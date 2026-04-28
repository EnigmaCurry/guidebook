"""NATS connection management API routes."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from guidebook.db import GlobalSetting, get_global_session
from guidebook.nats_client import (
    compute_fingerprint,
    extract_cn,
    get_status,
    restart_nats,
)

router = APIRouter(prefix="/api/nats", tags=["nats"])


@router.get("/status")
async def nats_status():
    return get_status()


@router.get("/certs")
async def nats_certs(gdb: AsyncSession = Depends(get_global_session)):
    ca_pem = (
        await gdb.execute(
            select(GlobalSetting).where(GlobalSetting.key == "nats_ca_cert")
        )
    ).scalar_one_or_none()
    client_pem = (
        await gdb.execute(
            select(GlobalSetting).where(GlobalSetting.key == "nats_client_cert")
        )
    ).scalar_one_or_none()

    ca_fingerprint = None
    client_fingerprint = None
    cn = None

    if ca_pem and ca_pem.value:
        ca_fingerprint = compute_fingerprint(ca_pem.value)
    if client_pem and client_pem.value:
        client_fingerprint = compute_fingerprint(client_pem.value)
        cn = extract_cn(client_pem.value)

    has_key = (
        await gdb.execute(
            select(GlobalSetting).where(GlobalSetting.key == "nats_client_key")
        )
    ).scalar_one_or_none()

    return {
        "ca_fingerprint": ca_fingerprint,
        "client_fingerprint": client_fingerprint,
        "cn": cn,
        "has_key": bool(has_key and has_key.value),
    }


@router.post("/restart")
async def nats_restart():
    await restart_nats()
    return {"ok": True}
