"""NATS Chat manager.

Provides lobby presence/chat, challenge-response peer verification,
trusted contacts, and deterministic private room derivation.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from collections import deque

logger = logging.getLogger("guidebook.chat")

# --- Module state ---
_chat_task: asyncio.Task | None = None
_lobby_sub = None
_verify_sub = None
_dm_subs: dict[str, object] = {}  # room_id -> NATS subscription

_peers: dict[str, dict] = {}  # fingerprint -> {cn, last_seen, cert_pem}
_trusted: dict[str, dict] = {}  # fingerprint -> {cn, cert_pem, verified_at, mutual}
_pending_challenges: dict[str, dict] = {}  # fingerprint -> {nonce, timestamp}
_pending_incoming: dict[str, dict] = {}  # fingerprint -> {from_cn, from_fingerprint, nonce}

_chat_buffers: dict[str, deque] = {}  # room_id -> deque of messages
_buffer_sizes: dict[str, int] = {}  # room_id -> current buffer size in bytes
BUFFER_MAX_BYTES = 100_000  # 100KB per room

_lobby_enabled = False
_own_cn: str | None = None
_own_fingerprint: str | None = None
_own_cert_pem: str | None = None


def _broadcast_chat_event(event_type: str, data: dict):
    from guidebook.sse import broadcast

    broadcast(event_type, data)


def _append_message(room_id: str, msg: dict):
    if room_id not in _chat_buffers:
        _chat_buffers[room_id] = deque()
        _buffer_sizes[room_id] = 0
    buf = _chat_buffers[room_id]
    msg_bytes = len(json.dumps(msg).encode())
    buf.append(msg)
    _buffer_sizes[room_id] += msg_bytes
    # Evict oldest messages if over limit
    while _buffer_sizes[room_id] > BUFFER_MAX_BYTES and buf:
        old = buf.popleft()
        _buffer_sizes[room_id] -= len(json.dumps(old).encode())


def _derive_room_id(fp_a: str, fp_b: str) -> str:
    """Derive a deterministic private room subject from two fingerprints."""
    parts = sorted([fp_a, fp_b])
    h = hashlib.sha256((parts[0] + parts[1]).encode()).hexdigest()
    return h


def get_peers() -> list[dict]:
    now = time.time()
    return [
        {"cn": p["cn"], "fingerprint": fp, "last_seen": p["last_seen"]}
        for fp, p in _peers.items()
        if now - p["last_seen"] < 60 and fp != _own_fingerprint
    ]


def get_trusted() -> list[dict]:
    return [
        {
            "cn": t["cn"],
            "fingerprint": fp,
            "verified_at": t["verified_at"],
            "mutual": t["mutual"],
        }
        for fp, t in _trusted.items()
    ]


def get_pending_incoming() -> list[dict]:
    return [
        {"cn": v["from_cn"], "fingerprint": v["from_fingerprint"], "nonce": v["nonce"]}
        for v in _pending_incoming.values()
    ]


def get_rooms() -> list[dict]:
    rooms = []
    if _lobby_enabled:
        rooms.append(
            {
                "id": "lobby",
                "type": "lobby",
                "name": "Public Lobby",
                "subject": "guidebook.chat.lobby",
            }
        )
    for fp, t in _trusted.items():
        if t["mutual"]:
            room_id = _derive_room_id(_own_fingerprint, fp)
            rooms.append(
                {
                    "id": room_id,
                    "type": "dm",
                    "name": t["cn"],
                    "fingerprint": fp,
                    "subject": f"guidebook.chat.dm.{room_id}",
                }
            )
    return rooms


def get_messages(room_id: str, limit: int = 100) -> list[dict]:
    buf = _chat_buffers.get(room_id, deque())
    msgs = list(buf)
    if limit and len(msgs) > limit:
        msgs = msgs[-limit:]
    return msgs


async def _on_lobby_message(msg):
    try:
        data = json.loads(msg.data.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    msg_type = data.get("type")
    fp = data.get("fingerprint")
    cn = data.get("cn", "unknown")

    if msg_type == "presence":
        if fp and fp != _own_fingerprint:
            is_new = fp not in _peers
            _peers[fp] = {
                "cn": cn,
                "last_seen": time.time(),
                "cert_pem": data.get("cert_pem"),
            }
            if is_new:
                _broadcast_chat_event("chat-peers", {"peers": get_peers()})

    elif msg_type == "message":
        chat_msg = {
            "room": "lobby",
            "cn": cn,
            "fingerprint": fp,
            "text": data.get("text", ""),
            "ts": data.get("ts", time.time()),
        }
        _append_message("lobby", chat_msg)
        _broadcast_chat_event("chat-message", chat_msg)


async def _on_verify_message(msg):
    try:
        data = json.loads(msg.data.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    msg_type = data.get("type")
    from_fp = data.get("from_fingerprint")
    from_cn = data.get("from_cn", "unknown")

    if msg_type == "challenge":
        nonce = data.get("nonce")
        reciprocal = data.get("reciprocal", False)

        if reciprocal and from_fp in _trusted:
            # Auto-respond to reciprocal challenges from already-accepted peers
            await _sign_and_respond(from_fp, nonce)
            return

        # Store as pending incoming request for UI
        _pending_incoming[from_fp] = {
            "from_cn": from_cn,
            "from_fingerprint": from_fp,
            "nonce": nonce,
        }
        _broadcast_chat_event(
            "chat-verify-request",
            {"cn": from_cn, "fingerprint": from_fp},
        )
        logger.info("Verification request from %s (%s)", from_cn, from_fp[:16])

    elif msg_type == "response":
        nonce = data.get("nonce")
        signature = data.get("signature")
        cert_pem = data.get("cert_pem")

        if from_fp not in _pending_challenges:
            return
        expected_nonce = _pending_challenges[from_fp]["nonce"]
        if nonce != expected_nonce:
            logger.warning("Nonce mismatch in verification response from %s", from_cn)
            return

        # Verify the signature
        if not cert_pem or not signature:
            logger.warning("Missing cert or signature in response from %s", from_cn)
            return

        if not _verify_signature(cert_pem, nonce, signature):
            logger.warning("Invalid signature in verification response from %s", from_cn)
            return

        # Verify fingerprint matches the cert they provided
        from guidebook.nats_client import compute_fingerprint

        actual_fp = compute_fingerprint(cert_pem)
        if actual_fp != from_fp:
            logger.warning(
                "Fingerprint mismatch: advertised %s, cert has %s",
                from_fp[:16],
                (actual_fp or "none")[:16],
            )
            return

        # Trust established!
        del _pending_challenges[from_fp]
        already_trusted = from_fp in _trusted
        _trusted[from_fp] = {
            "cn": from_cn,
            "cert_pem": cert_pem,
            "verified_at": time.time(),
            "mutual": True,
        }
        logger.info("Verified peer %s (%s)", from_cn, from_fp[:16])

        if not already_trusted:
            # Send reciprocal challenge so they can verify us too
            await _send_challenge(from_fp, reciprocal=True)

        # Subscribe to private room
        room_id = _derive_room_id(_own_fingerprint, from_fp)
        await _subscribe_dm(room_id)

        _broadcast_chat_event(
            "chat-verify-complete",
            {"cn": from_cn, "fingerprint": from_fp, "room_id": room_id},
        )
        _broadcast_chat_event("chat-rooms", {"rooms": get_rooms()})


def _verify_signature(cert_pem: str, nonce: str, signature_b64: str) -> bool:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
        from cryptography.x509 import load_pem_x509_certificate

        cert = load_pem_x509_certificate(cert_pem.encode())
        public_key = cert.public_key()
        sig_bytes = base64.b64decode(signature_b64)
        nonce_bytes = nonce.encode()

        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(sig_bytes, nonce_bytes, padding.PKCS1v15(), hashes.SHA256())
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(sig_bytes, nonce_bytes, ec.ECDSA(hashes.SHA256()))
        else:
            logger.warning("Unsupported key type: %s", type(public_key))
            return False
        return True
    except Exception as e:
        logger.warning("Signature verification failed: %s", e)
        return False


def _sign_nonce(key_pem: str, nonce: str) -> str | None:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

        private_key = load_pem_private_key(key_pem.encode(), password=None)
        nonce_bytes = nonce.encode()

        if isinstance(private_key, rsa.RSAPrivateKey):
            sig = private_key.sign(nonce_bytes, padding.PKCS1v15(), hashes.SHA256())
        elif isinstance(private_key, ec.EllipticCurvePrivateKey):
            sig = private_key.sign(nonce_bytes, ec.ECDSA(hashes.SHA256()))
        else:
            logger.warning("Unsupported key type for signing: %s", type(private_key))
            return None
        return base64.b64encode(sig).decode()
    except Exception as e:
        logger.warning("Failed to sign nonce: %s", e)
        return None


async def _sign_and_respond(peer_fingerprint: str, nonce: str):
    from guidebook.nats_client import get_client, get_own_cert_pem, get_own_key_pem

    nc = get_client()
    if not nc or not nc.is_connected:
        return

    key_pem = await get_own_key_pem()
    cert_pem = await get_own_cert_pem()
    if not key_pem or not cert_pem:
        return

    signature = _sign_nonce(key_pem, nonce)
    if not signature:
        return

    response = {
        "type": "response",
        "from_cn": _own_cn,
        "from_fingerprint": _own_fingerprint,
        "nonce": nonce,
        "signature": signature,
        "cert_pem": cert_pem,
    }
    # Sanitize fingerprint for NATS subject (remove colons)
    subject_fp = peer_fingerprint.replace(":", "")
    await nc.publish(
        f"guidebook.chat.verify.{subject_fp}",
        json.dumps(response).encode(),
    )
    logger.info("Sent verification response to %s", peer_fingerprint[:16])


async def _send_challenge(peer_fingerprint: str, reciprocal: bool = False):
    from guidebook.nats_client import get_client

    nc = get_client()
    if not nc or not nc.is_connected:
        return

    nonce = os.urandom(32).hex()
    _pending_challenges[peer_fingerprint] = {"nonce": nonce, "timestamp": time.time()}

    challenge = {
        "type": "challenge",
        "from_cn": _own_cn,
        "from_fingerprint": _own_fingerprint,
        "nonce": nonce,
    }
    if reciprocal:
        challenge["reciprocal"] = True

    subject_fp = peer_fingerprint.replace(":", "")
    await nc.publish(
        f"guidebook.chat.verify.{subject_fp}",
        json.dumps(challenge).encode(),
    )
    logger.info(
        "Sent %schallenge to %s",
        "reciprocal " if reciprocal else "",
        peer_fingerprint[:16],
    )


async def initiate_verification(peer_fingerprint: str):
    await _send_challenge(peer_fingerprint)


async def accept_verification(peer_fingerprint: str):
    incoming = _pending_incoming.pop(peer_fingerprint, None)
    if not incoming:
        return

    # Sign the nonce and respond
    await _sign_and_respond(peer_fingerprint, incoming["nonce"])

    # Mark as trusted (not yet mutual until they send reciprocal)
    peer_info = _peers.get(peer_fingerprint, {})
    _trusted[peer_fingerprint] = {
        "cn": incoming["from_cn"],
        "cert_pem": peer_info.get("cert_pem"),
        "verified_at": time.time(),
        "mutual": False,
    }
    logger.info(
        "Accepted verification from %s (%s)",
        incoming["from_cn"],
        peer_fingerprint[:16],
    )


async def reject_verification(peer_fingerprint: str):
    _pending_incoming.pop(peer_fingerprint, None)
    logger.info("Rejected verification from %s", peer_fingerprint[:16])


async def send_lobby_message(text: str):
    from guidebook.nats_client import get_client

    nc = get_client()
    if not nc or not nc.is_connected or not _lobby_enabled:
        return

    msg = {
        "type": "message",
        "cn": _own_cn,
        "fingerprint": _own_fingerprint,
        "text": text,
        "ts": time.time(),
    }
    await nc.publish("guidebook.chat.lobby", json.dumps(msg).encode())


async def send_dm_message(room_id: str, text: str):
    from guidebook.nats_client import get_client

    nc = get_client()
    if not nc or not nc.is_connected:
        return

    msg = {
        "type": "message",
        "cn": _own_cn,
        "fingerprint": _own_fingerprint,
        "text": text,
        "ts": time.time(),
    }
    await nc.publish(f"guidebook.chat.dm.{room_id}", json.dumps(msg).encode())
    # Also store locally since we won't receive our own message
    chat_msg = {
        "room": room_id,
        "cn": _own_cn,
        "fingerprint": _own_fingerprint,
        "text": text,
        "ts": msg["ts"],
    }
    _append_message(room_id, chat_msg)
    _broadcast_chat_event("chat-message", chat_msg)


async def _on_dm_message(room_id: str):
    async def handler(msg):
        try:
            data = json.loads(msg.data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        # Skip our own messages
        if data.get("fingerprint") == _own_fingerprint:
            return
        chat_msg = {
            "room": room_id,
            "cn": data.get("cn", "unknown"),
            "fingerprint": data.get("fingerprint"),
            "text": data.get("text", ""),
            "ts": data.get("ts", time.time()),
        }
        _append_message(room_id, chat_msg)
        _broadcast_chat_event("chat-message", chat_msg)

    return handler


async def _subscribe_dm(room_id: str):
    from guidebook.nats_client import get_client

    nc = get_client()
    if not nc or not nc.is_connected:
        return
    if room_id in _dm_subs:
        return
    handler = await _on_dm_message(room_id)
    sub = await nc.subscribe(f"guidebook.chat.dm.{room_id}", cb=handler)
    _dm_subs[room_id] = sub
    logger.info("Subscribed to DM room %s", room_id[:16])


async def _presence_loop():
    """Periodically publish presence and prune stale peers."""
    from guidebook.nats_client import get_client

    while True:
        try:
            nc = get_client()
            if nc and nc.is_connected and _lobby_enabled and _own_fingerprint:
                presence = {
                    "type": "presence",
                    "cn": _own_cn,
                    "fingerprint": _own_fingerprint,
                    "cert_pem": _own_cert_pem,
                    "timestamp": time.time(),
                }
                await nc.publish(
                    "guidebook.chat.lobby", json.dumps(presence).encode()
                )

            # Prune stale peers
            now = time.time()
            stale = [fp for fp, p in _peers.items() if now - p["last_seen"] > 60]
            if stale:
                for fp in stale:
                    del _peers[fp]
                _broadcast_chat_event("chat-peers", {"peers": get_peers()})

        except Exception as e:
            logger.warning("Presence loop error: %s", e)

        await asyncio.sleep(30)


async def join_lobby():
    global _lobby_sub, _lobby_enabled
    from guidebook.nats_client import get_client

    nc = get_client()
    if not nc or not nc.is_connected:
        return
    if _lobby_sub is not None:
        return

    _lobby_enabled = True
    _lobby_sub = await nc.subscribe("guidebook.chat.lobby", cb=_on_lobby_message)
    logger.info("Joined public lobby")

    # Publish initial presence
    if _own_fingerprint:
        presence = {
            "type": "presence",
            "cn": _own_cn,
            "fingerprint": _own_fingerprint,
            "cert_pem": _own_cert_pem,
            "timestamp": time.time(),
        }
        await nc.publish("guidebook.chat.lobby", json.dumps(presence).encode())


async def leave_lobby():
    global _lobby_sub, _lobby_enabled
    _lobby_enabled = False
    if _lobby_sub is not None:
        try:
            await _lobby_sub.unsubscribe()
        except Exception:
            pass
        _lobby_sub = None
    _peers.clear()
    _chat_buffers.pop("lobby", None)
    _buffer_sizes.pop("lobby", None)
    _broadcast_chat_event("chat-peers", {"peers": []})
    logger.info("Left public lobby")


async def start_chat():
    global _chat_task, _verify_sub, _own_cn, _own_fingerprint, _own_cert_pem
    from guidebook.nats_client import (
        get_client,
        get_own_cert_pem,
        get_own_cn,
        get_own_fingerprint,
    )

    nc = get_client()
    if not nc or not nc.is_connected:
        logger.warning("Cannot start chat: NATS not connected")
        return

    _own_cn = await get_own_cn()
    _own_fingerprint = await get_own_fingerprint()
    _own_cert_pem = await get_own_cert_pem()

    if not _own_fingerprint:
        logger.warning("Cannot start chat: no client certificate configured")
        return

    # Subscribe to verification subject
    subject_fp = _own_fingerprint.replace(":", "")
    _verify_sub = await nc.subscribe(
        f"guidebook.chat.verify.{subject_fp}", cb=_on_verify_message
    )
    logger.info("Chat started (CN=%s)", _own_cn)

    # Check if lobby should be joined
    from sqlalchemy import select

    from guidebook.db import GlobalSetting, global_async_session

    async with global_async_session() as gdb:
        row = (
            await gdb.execute(
                select(GlobalSetting).where(GlobalSetting.key == "nats_lobby_enabled")
            )
        ).scalar_one_or_none()
        lobby_enabled = row and row.value == "true"

    if lobby_enabled:
        await join_lobby()

    # Start presence loop
    if _chat_task is None:
        _chat_task = asyncio.create_task(_presence_loop())


async def stop_chat():
    global _chat_task, _verify_sub, _lobby_sub, _lobby_enabled
    global _own_cn, _own_fingerprint, _own_cert_pem

    if _chat_task is not None:
        _chat_task.cancel()
        try:
            await _chat_task
        except asyncio.CancelledError:
            pass
        _chat_task = None

    if _verify_sub is not None:
        try:
            await _verify_sub.unsubscribe()
        except Exception:
            pass
        _verify_sub = None

    await leave_lobby()

    # Unsubscribe from all DM rooms
    for room_id, sub in list(_dm_subs.items()):
        try:
            await sub.unsubscribe()
        except Exception:
            pass
    _dm_subs.clear()

    _peers.clear()
    _trusted.clear()
    _pending_challenges.clear()
    _pending_incoming.clear()
    _chat_buffers.clear()
    _buffer_sizes.clear()
    _lobby_enabled = False
    _own_cn = None
    _own_fingerprint = None
    _own_cert_pem = None
    logger.info("Chat stopped")
