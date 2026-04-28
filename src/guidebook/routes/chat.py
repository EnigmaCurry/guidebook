import base64
import hashlib
import hmac
import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from guidebook.db import InstanceSetting, db_manager
from guidebook.chat import (
    WEBRTC_SIGNAL_TYPES,
    accept_verification,
    get_messages,
    get_peers,
    get_pending_incoming,
    get_rooms,
    get_trusted,
    initiate_verification,
    reject_verification,
    remove_trusted,
    send_dm_message,
    send_signal,
)

logger = logging.getLogger("guidebook.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])


class SendMessage(BaseModel):
    text: str


class SignalMessage(BaseModel):
    type: str
    sdp: str | None = None
    candidate: str | None = None
    sdpMid: str | None = None
    sdpMLineIndex: int | None = None
    ts: float | None = None


TURN_TTL_SECONDS = 14400  # 4 hours


@router.get("/ice-servers")
async def get_ice_servers():
    """Return ICE servers with ephemeral TURN credentials computed from shared secret."""
    if db_manager._instance_session_factory is None:
        return []
    async with db_manager._instance_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(InstanceSetting).where(
                        InstanceSetting.key.in_(["turn_server", "turn_secret"])
                    )
                )
            )
            .scalars()
            .all()
        )
    settings = {r.key: r.value for r in rows if r.value}
    server = settings.get("turn_server", "").strip()
    secret = settings.get("turn_secret", "").strip()
    if not server or not secret:
        return []

    expiry = int(time.time()) + TURN_TTL_SECONDS
    username = f"{expiry}:guidebook"
    password = base64.b64encode(
        hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    ).decode()

    return [
        {
            "urls": f"turn:{server}",
            "username": username,
            "credential": password,
        }
    ]


@router.get("/status")
async def chat_status():
    from guidebook.chat import _own_cn, _own_fingerprint, _lobby_enabled, _verify_sub

    return {
        "active": _verify_sub is not None,
        "cn": _own_cn,
        "fingerprint": _own_fingerprint,
        "lobby_joined": _lobby_enabled,
        "peer_count": len(get_peers()),
        "room_count": len(get_rooms()),
    }


@router.get("/peers")
async def list_peers():
    return get_peers()


@router.get("/rooms")
async def list_rooms():
    return get_rooms()


@router.get("/rooms/{room_id}/messages")
async def room_messages(room_id: str, limit: int = 100):
    return get_messages(room_id, limit)


@router.post("/rooms/{room_id}/send")
async def send_message(room_id: str, data: SendMessage):
    if not data.text.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    if room_id == "lobby":
        raise HTTPException(status_code=403, detail="Lobby is for peer discovery only")
    await send_dm_message(room_id, data.text)
    return {"ok": True}


@router.post("/rooms/{room_id}/signal")
async def send_signal_endpoint(room_id: str, data: SignalMessage):
    if data.type not in WEBRTC_SIGNAL_TYPES:
        raise HTTPException(status_code=400, detail="Invalid signal type")
    await send_signal(room_id, data.model_dump(exclude_none=True))
    return {"ok": True}


@router.get("/trusted")
async def list_trusted():
    return get_trusted()


@router.delete("/trusted/{fingerprint}")
async def delete_trusted(fingerprint: str):
    await remove_trusted(fingerprint)
    return {"ok": True}


@router.get("/verify/pending")
async def list_pending():
    return get_pending_incoming()


@router.post("/verify/{fingerprint}")
async def start_verification(fingerprint: str):
    await initiate_verification(fingerprint)
    return {"ok": True}


@router.post("/verify/{fingerprint}/accept")
async def accept_verify(fingerprint: str):
    await accept_verification(fingerprint)
    return {"ok": True}


@router.post("/verify/{fingerprint}/reject")
async def reject_verify(fingerprint: str):
    await reject_verification(fingerprint)
    return {"ok": True}
