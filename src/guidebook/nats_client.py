"""NATS connection manager.

Maintains an optional background NATS connection with TLS/mTLS support.
Connection status is broadcast to the frontend via SSE.
"""

import asyncio
import logging
import os
import ssl
import tempfile

from cryptography import x509
from cryptography.x509.oid import NameOID

logger = logging.getLogger("guidebook.nats")

_nats_task: asyncio.Task | None = None
_nats_client = None
_nats_status: dict = {"state": "disabled", "detail": None, "cn": None}


def get_status() -> dict:
    return dict(_nats_status)


def get_client():
    """Return the active NATS client, or None if not connected."""
    return _nats_client


async def get_own_cert_pem() -> str | None:
    """Return the stored client certificate PEM."""
    settings = await _read_nats_settings()
    return settings.get("nats_client_cert") or None


async def get_own_key_pem() -> str | None:
    """Return the stored client private key PEM."""
    settings = await _read_nats_settings()
    return settings.get("nats_client_key") or None


async def get_own_cn() -> str | None:
    """Return the CN from the stored client certificate."""
    cert_pem = await get_own_cert_pem()
    return extract_cn(cert_pem) if cert_pem else None


async def get_own_fingerprint() -> str | None:
    """Return the SHA-256 fingerprint of the stored client certificate."""
    cert_pem = await get_own_cert_pem()
    return compute_fingerprint(cert_pem) if cert_pem else None


def compute_fingerprint(cert_pem: str) -> str | None:
    """Return the SHA-256 fingerprint of a PEM certificate."""
    try:
        from cryptography.hazmat.primitives import hashes

        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        digest = cert.fingerprint(hashes.SHA256())
        return ":".join(f"{b:02X}" for b in digest)
    except Exception:
        return None


def extract_cn(cert_pem: str) -> str | None:
    """Extract the Common Name from a PEM certificate."""
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return cn_attrs[0].value if cn_attrs else None
    except Exception:
        return None


async def _read_nats_settings() -> dict:
    from sqlalchemy import select

    from guidebook.db import InstanceSetting, instance_async_session

    keys = [
        "nats_enabled",
        "nats_endpoint",
        "nats_ca_cert",
        "nats_client_cert",
        "nats_client_key",
    ]
    result = {}
    async with instance_async_session() as gdb:
        for key in keys:
            row = (
                await gdb.execute(
                    select(InstanceSetting).where(InstanceSetting.key == key)
                )
            ).scalar_one_or_none()
            result[key] = row.value if row else None
    return result


def _build_ssl_context(
    ca_cert: str, client_cert: str | None, client_key: str | None
) -> tuple[ssl.SSLContext, list[str]]:
    temp_files: list[str] = []
    ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)

    ca_file = tempfile.NamedTemporaryFile(
        delete=False, suffix=".pem", prefix="guidebook_nats_ca_"
    )
    ca_file.write(ca_cert.encode())
    ca_file.close()
    temp_files.append(ca_file.name)
    ctx.load_verify_locations(cafile=ca_file.name)

    if client_cert and client_key:
        cert_file = tempfile.NamedTemporaryFile(
            delete=False, suffix=".pem", prefix="guidebook_nats_cert_"
        )
        cert_file.write(client_cert.encode())
        cert_file.close()
        temp_files.append(cert_file.name)
        os.chmod(cert_file.name, 0o600)

        key_file = tempfile.NamedTemporaryFile(
            delete=False, suffix=".pem", prefix="guidebook_nats_key_"
        )
        key_file.write(client_key.encode())
        key_file.close()
        temp_files.append(key_file.name)
        os.chmod(key_file.name, 0o600)

        ctx.load_cert_chain(certfile=cert_file.name, keyfile=key_file.name)

    return ctx, temp_files


def _cleanup_temp_files(paths: list[str]):
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def _set_status(state: str, detail: str | None = None, cn: str | None = None):
    global _nats_status
    prev_state = _nats_status.get("state")
    _nats_status = {"state": state, "detail": detail, "cn": cn}
    from guidebook.sse import broadcast

    broadcast("nats-status", _nats_status)

    # Start/stop chat on NATS connection state changes
    if state == "connected" and prev_state != "connected":
        asyncio.ensure_future(_on_nats_connected())
    elif state != "connected" and prev_state == "connected":
        asyncio.ensure_future(_on_nats_disconnected())


async def _on_nats_connected():
    """Called when NATS transitions to connected state."""
    from sqlalchemy import select

    from guidebook.db import InstanceSetting, instance_async_session

    try:
        async with instance_async_session() as db:
            row = (
                await db.execute(
                    select(InstanceSetting).where(
                        InstanceSetting.key == "nats_chat_enabled"
                    )
                )
            ).scalar_one_or_none()
            if row and row.value == "true":
                from guidebook.chat import start_chat

                await start_chat()
    except Exception as e:
        logger.warning("Failed to start chat on NATS connect: %s", e)


async def _on_nats_disconnected():
    """Called when NATS transitions away from connected state."""
    try:
        from guidebook.chat import stop_chat

        await stop_chat()
    except Exception as e:
        logger.warning("Failed to stop chat on NATS disconnect: %s", e)


async def _nats_connection_loop():
    import nats as nats_lib

    global _nats_client
    temp_files: list[str] = []
    try:
        while True:
            settings = await _read_nats_settings()
            if settings.get("nats_enabled") != "true":
                _set_status("disabled")
                await asyncio.sleep(10)
                continue

            endpoint = (settings.get("nats_endpoint") or "").strip()
            ca_cert = settings.get("nats_ca_cert") or ""
            client_cert = settings.get("nats_client_cert") or ""
            client_key = settings.get("nats_client_key") or ""

            if not endpoint:
                logger.warning("NATS enabled but no endpoint configured")
                _set_status("error", detail="No endpoint configured")
                await asyncio.sleep(10)
                continue

            cn = extract_cn(client_cert) if client_cert else None

            tls_ctx = None
            if ca_cert:
                try:
                    _cleanup_temp_files(temp_files)
                    tls_ctx, temp_files = _build_ssl_context(
                        ca_cert, client_cert, client_key
                    )
                except Exception as e:
                    _set_status("error", detail=f"TLS config error: {e}", cn=cn)
                    await asyncio.sleep(30)
                    continue

            _set_status("connecting", cn=cn)
            logger.info("Connecting to NATS %s (CN=%s)", endpoint, cn or "none")

            try:
                nc = await nats_lib.connect(
                    endpoint,
                    tls=tls_ctx,
                    reconnect_time_wait=5,
                    max_reconnect_attempts=-1,
                    disconnected_cb=_make_cb("disconnected", cn),
                    reconnected_cb=_make_cb("connected", cn),
                    error_cb=_make_error_cb(cn),
                )
                _nats_client = nc
                _set_status("connected", cn=cn)
                logger.info("Connected to NATS %s", endpoint)

                while nc.is_connected or nc.is_reconnecting:
                    await asyncio.sleep(5)
                    fresh = await _read_nats_settings()
                    if fresh.get("nats_enabled") != "true":
                        await nc.close()
                        _nats_client = None
                        _set_status("disabled")
                        break

                if nc.is_connected:
                    await nc.close()
                _nats_client = None

            except Exception as e:
                logger.warning("NATS connection failed (%s): %s", endpoint, e)
                _set_status("error", detail=str(e), cn=cn)
                _nats_client = None
                await asyncio.sleep(10)

    except asyncio.CancelledError:
        if _nats_client and _nats_client.is_connected:
            await _nats_client.close()
        _nats_client = None
        _cleanup_temp_files(temp_files)
        raise


def _make_cb(state: str, cn: str | None):
    async def cb():
        _set_status(state, cn=cn)

    return cb


def _make_error_cb(cn: str | None):
    async def cb(e):
        _set_status("error", detail=str(e), cn=cn)

    return cb


async def start_nats():
    global _nats_task
    if _nats_task is not None:
        return
    _nats_task = asyncio.create_task(_nats_connection_loop())
    logger.info("NATS connection manager started")


async def stop_nats():
    global _nats_task, _nats_client
    if _nats_task is not None:
        _nats_task.cancel()
        try:
            await _nats_task
        except asyncio.CancelledError:
            pass
        _nats_task = None
        _nats_client = None
        _set_status("disabled")


async def restart_nats():
    await stop_nats()
    await start_nats()
