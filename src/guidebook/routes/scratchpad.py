"""In-memory scratchpad with pub/sub SSE delivery.

The scratchpad content lives only in server memory — it is never persisted
to disk, database, or logs.  Only clients currently viewing the scratchpad
page subscribe to its SSE stream.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from guidebook.db import AuthToken, ClientCert, get_global_session
from guidebook.sse import _get_shutdown_event

logger = logging.getLogger("guidebook")

router = APIRouter(prefix="/api/scratchpad", tags=["scratchpad"])

_content: str = ""
_subscribers: list[asyncio.Queue[str]] = []

# Track observer identity per queue: queue id -> {label, auth_type}
_observer_info: dict[int, dict] = {}


class ScratchpadUpdate(BaseModel):
    content: str


def _broadcast(content: str) -> None:
    msg = f"event: scratchpad\ndata: {json.dumps({'content': content})}\n\n"
    for q in list(_subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


def _broadcast_observers() -> None:
    """Send current observer list to all subscribers."""
    observers = list(_observer_info.values())
    msg = f"event: observers\ndata: {json.dumps({'count': len(observers), 'observers': observers})}\n\n"
    for q in list(_subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


def _identify_request(request: Request) -> dict:
    """Extract observer identity from the request."""
    # Try mTLS cert serial
    peer_cert_der = request.scope.get("mtls_peer_cert_der")
    if peer_cert_der:
        try:
            from cryptography.x509 import load_der_x509_certificate

            cert = load_der_x509_certificate(peer_cert_der)
            serial = format(cert.serial_number, "x")
            return {"serial": serial, "auth_type": "mtls"}
        except Exception:
            pass

    # Try session ID from JWT cookie
    try:
        from guidebook.routes.auth import _get_current_session_id

        session_id = _get_current_session_id(request)
        if session_id:
            return {"session_id": session_id, "auth_type": "cookie"}
    except Exception:
        pass

    return {"auth_type": "anonymous"}


async def _resolve_label(identity: dict, gdb: AsyncSession) -> str:
    """Resolve a human-readable label from observer identity."""
    if identity.get("serial"):
        row = (
            await gdb.execute(
                select(ClientCert).where(
                    ClientCert.serial_number == identity["serial"],
                    ClientCert.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row:
            return row.label
        return f"cert-{identity['serial'][:8]}"

    if identity.get("session_id"):
        row = (
            await gdb.execute(
                select(AuthToken).where(AuthToken.id == identity["session_id"])
            )
        ).scalar_one_or_none()
        if row:
            return row.label
        return f"session-{identity['session_id']}"

    return "anonymous"


@router.get("/")
async def get_scratchpad():
    return {"content": _content}


@router.post("/")
async def update_scratchpad(data: ScratchpadUpdate):
    global _content
    _content = data.content
    _broadcast(_content)
    return {"ok": True}


@router.get("/observers")
async def get_observers():
    """Return current observer list."""
    observers = list(_observer_info.values())
    return {"count": len(observers), "observers": observers}


async def _sse_generator(queue: asyncio.Queue[str], request: Request):
    shutdown_evt = _get_shutdown_event()
    try:
        while not shutdown_evt.is_set():
            if await request.is_disconnected():
                return
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=5)
                yield msg
            except asyncio.TimeoutError:
                yield "event: keepalive\ndata: {}\n\n"
            if shutdown_evt.is_set():
                while not queue.empty():
                    yield queue.get_nowait()
                return
    except (asyncio.CancelledError, GeneratorExit):
        return


@router.get("/stream")
async def scratchpad_stream(
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
    queue_id = id(queue)

    # Identify the observer and resolve their label
    identity = _identify_request(request)
    label = await _resolve_label(identity, gdb)
    observer = {"label": label, "auth_type": identity["auth_type"]}

    _subscribers.append(queue)
    _observer_info[queue_id] = observer
    _broadcast_observers()

    async def cleanup_generator():
        try:
            async for msg in _sse_generator(queue, request):
                yield msg
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            if queue in _subscribers:
                _subscribers.remove(queue)
            _observer_info.pop(queue_id, None)
            _broadcast_observers()

    return StreamingResponse(
        cleanup_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
