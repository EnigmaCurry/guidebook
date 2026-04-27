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
    ca_fingerprint: str | None
    tls_enabled: bool
    proxy_mode: bool
    certs: list[dict]


def _get_peer_cert_serial(request: Request) -> str | None:
    """Extract the serial number (hex) of the client cert from the TLS connection."""
    peer_cert_der = request.scope.get("mtls_peer_cert_der")
    if not peer_cert_der:
        return None
    try:
        from cryptography.x509 import load_der_x509_certificate

        cert = load_der_x509_certificate(peer_cert_der)
        return format(cert.serial_number, "x")
    except Exception:
        return None


@router.get("/status")
async def mtls_status(
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    from guidebook.routes.auth import TLS_ENABLED, PROXY_MODE
    from guidebook.sse import connected_cert_serials

    mode = await _get_setting(gdb, "mtls_mode") or "disabled"
    ca_cert_pem = await _get_setting(gdb, "ca_cert_pem")
    current_serial = _get_peer_cert_serial(request)

    ca_fingerprint = None
    if ca_cert_pem:
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes

            ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode())
            ca_fingerprint = ca_cert.fingerprint(hashes.SHA256()).hex()
        except Exception:
            pass
    active_serials = connected_cert_serials()

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
            "is_current": current_serial is not None
            and c.serial_number == current_serial,
            "is_connected": c.serial_number in active_serials,
        }
        for c in result.scalars().all()
    ]

    return MtlsStatusResponse(
        mode=mode,
        ca_initialized=bool(ca_cert_pem),
        ca_fingerprint=ca_fingerprint,
        tls_enabled=TLS_ENABLED,
        proxy_mode=PROXY_MODE,
        certs=certs,
    )


@router.get("/ca.pem")
async def download_ca_cert(
    gdb: AsyncSession = Depends(get_global_session),
):
    """Download the CA public certificate in PEM format."""
    ca_cert_pem = await _get_setting(gdb, "ca_cert_pem")
    if not ca_cert_pem:
        raise HTTPException(404, "CA certificate not found.")
    return Response(
        content=ca_cert_pem,
        media_type="application/x-pem-file",
        headers={
            "Content-Disposition": 'attachment; filename="guidebook-ca.pem"',
            "Cache-Control": "no-store",
        },
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

    from guidebook.routes.auth import (
        AUTH_SLOTS,
        _get_current_session_id,
        _token_count,
    )

    from guidebook.db import META_DB_PATH
    from guidebook.tls import ensure_ca_cert, generate_client_cert

    ca_cert_pem, ca_key_pem = ensure_ca_cert(str(META_DB_PATH))

    # Count active (non-revoked) certs (exclude pending-upgrade certs from count)
    result = await gdb.execute(
        select(ClientCert).where(ClientCert.revoked_at.is_(None))
    )
    active_certs = result.scalars().all()
    active_cert_count = len(active_certs)

    # Check slot availability (sessions + certs share the same slots)
    session_count = await _token_count(gdb)
    total_used = session_count + active_cert_count
    pending_session_id = None

    if AUTH_SLOTS > 0 and total_used >= AUTH_SLOTS:
        # If the user has a cookie session, allow the upgrade — the session
        # will be revoked when the new cert is first used (state machine)
        current_sid = _get_current_session_id(request)
        if current_sid:
            pending_session_id = current_sid
        else:
            raise HTTPException(
                400,
                f"All {AUTH_SLOTS} slot(s) are in use ({session_count} session(s), {active_cert_count} cert(s)). Revoke a session or certificate first.",
            )

    label = f"client-{active_cert_count + 1}"

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
            pending_session_id=pending_session_id,
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

    await _set_setting(gdb, "mtls_mode", body.mode)
    await gdb.commit()
    logger.info("mTLS mode set to: %s (restart required)", body.mode)
    return {"status": "ok", "mode": body.mode, "restart_required": True}


@router.post("/logout")
async def mtls_logout(
    request: Request,
    response: Response,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Revoke the client certificate and clear the cookie session."""
    current_serial = _get_peer_cert_serial(request)
    if not current_serial:
        raise HTTPException(400, "No client certificate detected on this connection.")

    result = await gdb.execute(
        select(ClientCert).where(
            ClientCert.serial_number == current_serial,
            ClientCert.revoked_at.is_(None),
        )
    )
    cert = result.scalar_one_or_none()
    if not cert:
        raise HTTPException(404, "Current certificate not found or already revoked.")

    cert.revoked_at = time.time()

    # Also clear the cookie session so the user is fully logged out
    from guidebook.routes.auth import (
        AUTH_COOKIE_NAME,
        _get_current_session_id,
        _validate_session,
    )

    session_id = _get_current_session_id(request)
    if session_id:
        tok = await _validate_session(gdb, session_id)
        if tok:
            await gdb.delete(tok)

    await gdb.commit()
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    logger.info(
        "mTLS logout: revoked certificate %s and cleared session (serial: %s)",
        cert.label,
        cert.serial_number,
    )
    return {"status": "revoked", "restart_required": True}
