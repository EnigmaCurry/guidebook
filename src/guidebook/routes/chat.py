import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from guidebook.chat import (
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
)

logger = logging.getLogger("guidebook.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])


class SendMessage(BaseModel):
    text: str


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
