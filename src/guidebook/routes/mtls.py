"""mTLS client certificate management endpoints."""

import logging
import secrets
import time
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from guidebook.db import ClientCert, GlobalSetting, get_global_session

logger = logging.getLogger("guidebook")

router = APIRouter(prefix="/api/auth/mtls", tags=["mtls"])

PENDING_DOWNLOAD_TTL = 600  # 10 minutes


@dataclass
class PendingDownload:
    p12_bytes: bytes
    password: str
    created_at: float
    expires_at: float


# In-memory only — .p12 bytes and passwords never touch disk or DB
_pending_downloads: dict[str, PendingDownload] = {}


def _cleanup_expired() -> None:
    """Remove expired pending downloads."""
    now = time.time()
    expired = [k for k, v in _pending_downloads.items() if v.expires_at < now]
    for k in expired:
        del _pending_downloads[k]


async def _get_setting(gdb: AsyncSession, key: str) -> str | None:
    row = (
        await gdb.execute(select(GlobalSetting).where(GlobalSetting.key == key))
    ).scalar_one_or_none()
    return row.value if row else None


async def _set_setting(gdb: AsyncSession, key: str, value: str) -> None:
    from sqlalchemy.dialects.sqlite import insert

    stmt = insert(GlobalSetting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value})
    await gdb.execute(stmt)


class MtlsStatusResponse(BaseModel):
    mode: str
    ca_initialized: bool
    tls_enabled: bool
    proxy_mode: bool
    certs: list[dict]


@router.get("/status")
async def mtls_status(
    gdb: AsyncSession = Depends(get_global_session),
):
    from guidebook.routes.auth import TLS_ENABLED, PROXY_MODE

    mode = await _get_setting(gdb, "mtls_mode") or "disabled"
    ca_cert = await _get_setting(gdb, "ca_cert_pem")

    result = await gdb.execute(select(ClientCert).order_by(ClientCert.issued_at.desc()))
    certs = [
        {
            "id": c.id,
            "serial_number": c.serial_number,
            "label": c.label,
            "issued_at": c.issued_at,
            "expires_at": c.expires_at,
            "revoked_at": c.revoked_at,
            "fingerprint_sha256": c.fingerprint_sha256,
        }
        for c in result.scalars().all()
    ]

    return MtlsStatusResponse(
        mode=mode,
        ca_initialized=bool(ca_cert),
        tls_enabled=TLS_ENABLED,
        proxy_mode=PROXY_MODE,
        certs=certs,
    )


class GenerateCertResponse(BaseModel):
    download_token: str
    password: str
    fingerprint: str
    label: str


@router.post("/generate-cert")
async def generate_cert(
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Generate a client certificate. Returns download token and password (shown once)."""
    from guidebook.routes.auth import TLS_ENABLED, PROXY_MODE

    if not TLS_ENABLED:
        raise HTTPException(400, "TLS is not enabled. mTLS requires TLS.")
    if PROXY_MODE:
        raise HTTPException(400, "mTLS is not available in proxy mode.")

    from guidebook.db import META_DB_PATH
    from guidebook.tls import ensure_ca_cert, generate_client_cert

    ca_cert_pem, ca_key_pem = ensure_ca_cert(str(META_DB_PATH))

    # Count active (non-revoked) certs
    result = await gdb.execute(
        select(ClientCert).where(ClientCert.revoked_at.is_(None))
    )
    active_count = len(result.scalars().all())
    label = f"client-{active_count + 1}"

    p12_bytes, password, serial_hex, fingerprint = generate_client_cert(
        ca_cert_pem, ca_key_pem, label
    )

    # Store cert metadata in DB (no private key material)
    now = time.time()
    from guidebook.tls import CERT_VALIDITY_DAYS

    gdb.add(
        ClientCert(
            serial_number=serial_hex,
            label=label,
            issued_at=now,
            expires_at=now + CERT_VALIDITY_DAYS * 86400,
            fingerprint_sha256=fingerprint,
        )
    )
    await gdb.commit()

    # Store .p12 in memory only (never touches DB or disk)
    _cleanup_expired()
    download_token = secrets.token_urlsafe(48)
    _pending_downloads[download_token] = PendingDownload(
        p12_bytes=p12_bytes,
        password=password,
        created_at=now,
        expires_at=now + PENDING_DOWNLOAD_TTL,
    )

    logger.info(
        "Generated client certificate: %s (fingerprint: %s...)", label, fingerprint[:16]
    )
    return GenerateCertResponse(
        download_token=download_token,
        password=password,
        fingerprint=fingerprint,
        label=label,
    )


@router.get("/download/{download_token}")
async def download_cert(download_token: str):
    """Single-use .p12 download. Returns 410 if already downloaded or expired."""
    _cleanup_expired()

    pending = _pending_downloads.pop(download_token, None)
    if not pending:
        raise HTTPException(410, "Download link expired or already used.")

    if time.time() > pending.expires_at:
        raise HTTPException(410, "Download link expired.")

    return Response(
        content=pending.p12_bytes,
        media_type="application/x-pkcs12",
        headers={
            "Content-Disposition": 'attachment; filename="guidebook-client.p12"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/certs")
async def list_certs(
    gdb: AsyncSession = Depends(get_global_session),
):
    """List all issued client certificates."""
    result = await gdb.execute(select(ClientCert).order_by(ClientCert.issued_at.desc()))
    return [
        {
            "id": c.id,
            "serial_number": c.serial_number,
            "label": c.label,
            "issued_at": c.issued_at,
            "expires_at": c.expires_at,
            "revoked_at": c.revoked_at,
            "fingerprint_sha256": c.fingerprint_sha256,
        }
        for c in result.scalars().all()
    ]


@router.delete("/certs/{cert_id}")
async def revoke_cert(
    cert_id: int,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Revoke a client certificate."""
    result = await gdb.execute(select(ClientCert).where(ClientCert.id == cert_id))
    cert = result.scalar_one_or_none()
    if not cert:
        raise HTTPException(404, "Certificate not found.")
    if cert.revoked_at:
        raise HTTPException(400, "Certificate is already revoked.")

    cert.revoked_at = time.time()
    await gdb.commit()
    logger.info(
        "Revoked client certificate: %s (serial: %s)", cert.label, cert.serial_number
    )
    return {"status": "revoked", "id": cert_id, "restart_required": True}


class ActivateRequest(BaseModel):
    mode: str


@router.post("/activate")
async def activate_mtls(
    body: ActivateRequest,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Set mTLS mode. Requires server restart to take effect."""
    from guidebook.routes.auth import TLS_ENABLED, PROXY_MODE

    if not TLS_ENABLED:
        raise HTTPException(400, "TLS is not enabled. mTLS requires TLS.")
    if PROXY_MODE:
        raise HTTPException(400, "mTLS is not available in proxy mode.")
    if body.mode not in ("disabled", "optional", "required"):
        raise HTTPException(400, f"Invalid mode: {body.mode}")

    if body.mode != "disabled":
        # Ensure CA exists
        from guidebook.db import META_DB_PATH
        from guidebook.tls import ensure_ca_cert

        ensure_ca_cert(str(META_DB_PATH))

    await _set_setting(gdb, "mtls_mode", body.mode)
    await gdb.commit()
    logger.info("mTLS mode set to: %s (restart required)", body.mode)
    return {"status": "ok", "mode": body.mode, "restart_required": True}
