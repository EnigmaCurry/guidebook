"""In-memory scratchpad with pub/sub SSE delivery.

The scratchpad content lives only in server memory — it is never persisted
to disk, database, or logs.  Only clients currently viewing the scratchpad
page subscribe to its SSE stream.
"""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from guidebook.sse import _get_shutdown_event

router = APIRouter(prefix="/api/scratchpad", tags=["scratchpad"])

_content: str = ""
_subscribers: list[asyncio.Queue[str]] = []


class ScratchpadUpdate(BaseModel):
    content: str


def _broadcast(content: str) -> None:
    msg = f"event: scratchpad\ndata: {json.dumps({'content': content})}\n\n"
    for q in list(_subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


@router.get("/")
async def get_scratchpad():
    return {"content": _content}


@router.post("/")
async def update_scratchpad(data: ScratchpadUpdate):
    global _content
    _content = data.content
    _broadcast(_content)
    return {"ok": True}


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
async def scratchpad_stream(request: Request):
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
    _subscribers.append(queue)

    async def cleanup_generator():
        try:
            async for msg in _sse_generator(queue, request):
                yield msg
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            if queue in _subscribers:
                _subscribers.remove(queue)

    return StreamingResponse(
        cleanup_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
