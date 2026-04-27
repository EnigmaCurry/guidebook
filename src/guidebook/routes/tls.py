"""TLS certificate management endpoints and ACME auto-renewal."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from guidebook.db import GlobalSetting, get_global_session

logger = logging.getLogger("guidebook")

router = APIRouter(prefix="/api/tls", tags=["tls"])


# ---------------------------------------------------------------------------
# DB helpers (same pattern as mtls.py)
# ---------------------------------------------------------------------------


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


async def _delete_setting(gdb: AsyncSession, key: str) -> None:
    row = (
        await gdb.execute(select(GlobalSetting).where(GlobalSetting.key == key))
    ).scalar_one_or_none()
    if row:
        await gdb.delete(row)


# ---------------------------------------------------------------------------
# GET /api/tls/status
# ---------------------------------------------------------------------------


class TlsStatusResponse(BaseModel):
    tls_enabled: bool
    tls_mode: str  # "self-signed" or "acme"
    # Self-signed info
    ca_fingerprint: str | None = None
    server_cert_fingerprint: str | None = None
    # Common cert info
    cert_issuer: str | None = None
    cert_subject: str | None = None
    cert_not_before: str | None = None
    cert_not_after: str | None = None
    cert_san: list[str] | None = None
    next_renewal: str | None = None
    # ACME-DNS state
    acme_domain: str | None = None
    acme_endpoint: str | None = None
    acme_dns_server: str | None = None
    acme_dns_fulldomain: str | None = None
    acme_registered: bool = False
    acme_configured: bool = False


@router.get("/status")
async def tls_status(
    gdb: AsyncSession = Depends(get_global_session),
) -> TlsStatusResponse:
    from guidebook.routes.auth import TLS_ENABLED

    tls_mode = await _get_setting(gdb, "tls_mode") or "self-signed"
    resp = TlsStatusResponse(tls_enabled=TLS_ENABLED, tls_mode=tls_mode)

    if not TLS_ENABLED:
        return resp

    # Parse current server cert info
    cert_pem = await _get_setting(gdb, "tls_cert_pem")
    if cert_pem:
        from guidebook.acme import parse_cert_info

        info = parse_cert_info(cert_pem)
        resp.cert_issuer = info["issuer"]
        resp.cert_subject = info["subject"]
        resp.cert_not_before = info["not_before"]
        resp.cert_not_after = info["not_after"]
        resp.server_cert_fingerprint = info["fingerprint_sha256"]
        resp.cert_san = info["san"]

    # CA fingerprint (for self-signed mode display)
    ca_pem = await _get_setting(gdb, "ca_cert_pem")
    if ca_pem:
        from guidebook.acme import parse_cert_info

        ca_info = parse_cert_info(ca_pem)
        resp.ca_fingerprint = ca_info["fingerprint_sha256"]

    # ACME config state
    acme_domain = await _get_setting(gdb, "acme_domain")
    acme_endpoint = await _get_setting(gdb, "acme_endpoint")
    acme_dns_server = await _get_setting(gdb, "acme_dns_server")
    acme_dns_fulldomain = await _get_setting(gdb, "acme_dns_fulldomain")

    resp.acme_domain = acme_domain
    resp.acme_endpoint = acme_endpoint
    resp.acme_dns_server = acme_dns_server
    resp.acme_dns_fulldomain = acme_dns_fulldomain
    resp.acme_configured = bool(acme_domain and acme_endpoint and acme_dns_server)
    resp.acme_registered = bool(acme_dns_fulldomain)

    # Renewal info for ACME certs
    if tls_mode == "acme" and cert_pem:
        from guidebook.acme import next_renewal_time

        resp.next_renewal = next_renewal_time(cert_pem)

    return resp


# ---------------------------------------------------------------------------
# GET /api/tls/ca.pem — download CA certificate
# ---------------------------------------------------------------------------


@router.get("/ca.pem")
async def download_ca_pem(
    gdb: AsyncSession = Depends(get_global_session),
):
    from guidebook.routes.auth import TLS_ENABLED

    if not TLS_ENABLED:
        raise HTTPException(404, "TLS is disabled")
    ca_pem = await _get_setting(gdb, "ca_cert_pem")
    if not ca_pem:
        raise HTTPException(404, "No CA certificate available")
    return Response(
        content=ca_pem,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": "attachment; filename=guidebook-ca.pem"},
    )


# ---------------------------------------------------------------------------
# POST /api/tls/acme/configure
# ---------------------------------------------------------------------------


class AcmeConfigureRequest(BaseModel):
    domain: str
    endpoint: str  # "le-production", "le-staging", or custom URL
    acme_dns_server: str = "https://auth.acme-dns.io"


@router.post("/acme/configure")
async def acme_configure(
    body: AcmeConfigureRequest,
    gdb: AsyncSession = Depends(get_global_session),
):
    from guidebook.routes.auth import TLS_ENABLED

    if not TLS_ENABLED:
        raise HTTPException(400, "TLS is disabled")

    from guidebook.acme import LE_PRODUCTION, LE_STAGING

    endpoint_url = body.endpoint
    if endpoint_url == "le-production":
        endpoint_url = LE_PRODUCTION
    elif endpoint_url == "le-staging":
        endpoint_url = LE_STAGING

    domain = body.domain.strip().lower()
    if not domain:
        raise HTTPException(400, "Domain is required")

    await _set_setting(gdb, "acme_domain", domain)
    await _set_setting(gdb, "acme_endpoint", endpoint_url)
    await _set_setting(gdb, "acme_dns_server", body.acme_dns_server.rstrip("/"))
    await gdb.commit()

    logger.info("ACME configured: domain=%s endpoint=%s", domain, endpoint_url)
    return {"status": "configured", "domain": domain, "endpoint": endpoint_url}


# ---------------------------------------------------------------------------
# POST /api/tls/acme/register — register with acme-dns
# ---------------------------------------------------------------------------


@router.post("/acme/register")
async def acme_register(
    gdb: AsyncSession = Depends(get_global_session),
):
    from guidebook.routes.auth import TLS_ENABLED

    if not TLS_ENABLED:
        raise HTTPException(400, "TLS is disabled")

    acme_dns_server = await _get_setting(gdb, "acme_dns_server")
    if not acme_dns_server:
        raise HTTPException(
            400, "ACME not configured. Call /api/tls/acme/configure first."
        )

    domain = await _get_setting(gdb, "acme_domain")
    if not domain:
        raise HTTPException(400, "No domain configured")

    from guidebook.acme import acmedns_register

    reg = await acmedns_register(acme_dns_server)

    await _set_setting(gdb, "acme_dns_subdomain", reg["subdomain"])
    await _set_setting(gdb, "acme_dns_username", reg["username"])
    await _set_setting(gdb, "acme_dns_password", reg["password"])
    await _set_setting(gdb, "acme_dns_fulldomain", reg["fulldomain"])
    await gdb.commit()

    logger.info("acme-dns registered: fulldomain=%s", reg["fulldomain"])
    return {
        "status": "registered",
        "fulldomain": reg["fulldomain"],
        "cname_record": f"_acme-challenge.{domain}",
        "cname_target": f"{reg['fulldomain']}.",
    }


# ---------------------------------------------------------------------------
# POST /api/tls/acme/verify-cname — check DNS CNAME
# ---------------------------------------------------------------------------


@router.post("/acme/verify-cname")
async def acme_verify_cname(
    gdb: AsyncSession = Depends(get_global_session),
):
    from guidebook.routes.auth import TLS_ENABLED

    if not TLS_ENABLED:
        raise HTTPException(400, "TLS is disabled")

    domain = await _get_setting(gdb, "acme_domain")
    fulldomain = await _get_setting(gdb, "acme_dns_fulldomain")
    if not domain or not fulldomain:
        raise HTTPException(400, "ACME not fully configured or registered")

    # Use asyncio subprocess to do a DNS lookup
    import asyncio

    cname_host = f"_acme-challenge.{domain}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "dig",
            "+short",
            "CNAME",
            cname_host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        result = stdout.decode().strip()
        # dig returns the CNAME target with trailing dot
        expected = f"{fulldomain}."
        found = any(
            line.strip().rstrip(".") == fulldomain
            for line in result.splitlines()
            if line.strip()
        )
        return {
            "status": "ok" if found else "not_found",
            "cname_host": cname_host,
            "expected": expected,
            "found": result or None,
        }
    except FileNotFoundError:
        # dig not available, try Python socket
        import socket

        try:
            socket.getaddrinfo(
                f"_acme-challenge.{domain}", None, type=socket.SOCK_STREAM
            )
            # Can't reliably check CNAME via getaddrinfo, just report it resolved
            return {
                "status": "resolved",
                "cname_host": cname_host,
                "expected": f"{fulldomain}.",
                "found": "DNS resolved (dig unavailable for CNAME check)",
            }
        except socket.gaierror:
            return {
                "status": "not_found",
                "cname_host": cname_host,
                "expected": f"{fulldomain}.",
                "found": None,
            }
    except Exception as e:
        return {
            "status": "error",
            "cname_host": cname_host,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# POST /api/tls/acme/provision — full ACME flow
# ---------------------------------------------------------------------------

_provision_lock = asyncio.Lock()


@router.post("/acme/provision")
async def acme_provision(
    gdb: AsyncSession = Depends(get_global_session),
):
    from guidebook.routes.auth import TLS_ENABLED

    if not TLS_ENABLED:
        raise HTTPException(400, "TLS is disabled")

    if _provision_lock.locked():
        raise HTTPException(409, "Certificate provisioning already in progress")

    domain = await _get_setting(gdb, "acme_domain")
    endpoint = await _get_setting(gdb, "acme_endpoint")
    acme_dns_server = await _get_setting(gdb, "acme_dns_server")
    acme_dns_subdomain = await _get_setting(gdb, "acme_dns_subdomain")
    acme_dns_username = await _get_setting(gdb, "acme_dns_username")
    acme_dns_password = await _get_setting(gdb, "acme_dns_password")

    if not all([domain, endpoint, acme_dns_server, acme_dns_subdomain]):
        raise HTTPException(400, "ACME not fully configured and registered")

    # Ensure ACME account exists
    account_key_pem = await _get_setting(gdb, "acme_account_key_pem")
    account_url = await _get_setting(gdb, "acme_account_url")
    if not account_key_pem:
        from guidebook.acme import generate_account_key

        account_key_pem = generate_account_key()
        await _set_setting(gdb, "acme_account_key_pem", account_key_pem)
        await gdb.commit()

    if not account_url:
        from guidebook.acme import acme_register_account

        account_url, _ = await acme_register_account(endpoint, account_key_pem)
        await _set_setting(gdb, "acme_account_url", account_url)
        await gdb.commit()

    from guidebook.acme import acme_provision_cert
    from guidebook.sse import broadcast

    async with _provision_lock:

        def on_progress(stage, detail):
            broadcast("tls-acme-progress", {"stage": stage, "detail": detail})

        try:
            cert_pem, key_pem = await acme_provision_cert(
                domain=domain,
                endpoint=endpoint,
                account_key_pem=account_key_pem,
                account_url=account_url,
                acmedns_server=acme_dns_server,
                acmedns_subdomain=acme_dns_subdomain,
                acmedns_username=acme_dns_username,
                acmedns_password=acme_dns_password,
                progress_callback=on_progress,
            )
        except Exception as e:
            logger.exception("ACME provisioning failed")
            broadcast(
                "tls-acme-progress",
                {"stage": "error", "detail": str(e)},
            )
            raise HTTPException(500, f"ACME provisioning failed: {e}")

    # Store the cert as the active TLS cert
    await _set_setting(gdb, "tls_cert_pem", cert_pem)
    await _set_setting(gdb, "tls_key_pem", key_pem)
    await _set_setting(gdb, "tls_mode", "acme")
    await gdb.commit()

    broadcast("tls-cert-updated", {"restart_required": True})

    from guidebook.acme import parse_cert_info

    info = parse_cert_info(cert_pem)
    logger.info(
        "ACME cert provisioned for %s (issuer: %s, expires: %s)",
        domain,
        info["issuer"],
        info["not_after"],
    )
    return {
        "status": "provisioned",
        "restart_required": True,
        "cert": info,
    }


# ---------------------------------------------------------------------------
# POST /api/tls/acme/revert — switch back to self-signed
# ---------------------------------------------------------------------------


@router.post("/acme/revert")
async def acme_revert(
    gdb: AsyncSession = Depends(get_global_session),
):
    from guidebook.routes.auth import TLS_ENABLED

    if not TLS_ENABLED:
        raise HTTPException(400, "TLS is disabled")

    from guidebook.db import META_DB_PATH
    from guidebook.tls import ensure_tls_cert

    # Delete the ACME cert and regenerate self-signed
    await _delete_setting(gdb, "tls_cert_pem")
    await _delete_setting(gdb, "tls_key_pem")
    await _set_setting(gdb, "tls_mode", "self-signed")
    await gdb.commit()

    # Regenerate self-signed cert (synchronous, uses sqlite3 directly)
    cert_pem, _key_pem = ensure_tls_cert(str(META_DB_PATH))

    from guidebook.sse import broadcast

    broadcast("tls-cert-updated", {"restart_required": True})

    logger.info("Reverted to self-signed TLS certificate")
    return {"status": "reverted", "restart_required": True}


# ---------------------------------------------------------------------------
# Background ACME renewal task
# ---------------------------------------------------------------------------

_acme_renewal_task: asyncio.Task | None = None


async def _acme_renewal_loop(initial_delay: float = 300):
    """Periodically check if the ACME cert needs renewal.

    Waits *initial_delay* seconds (default 5 minutes) before the first check,
    then checks every hour.
    """
    await asyncio.sleep(initial_delay)
    while True:
        try:
            await _check_and_renew()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ACME renewal check failed")
        await asyncio.sleep(3600)


async def _check_and_renew():
    """Check if renewal is needed and run the ACME flow if so."""
    from guidebook.db import global_async_session

    async with global_async_session() as gdb:
        tls_mode = await _get_setting(gdb, "tls_mode")
        if tls_mode != "acme":
            return

        cert_pem = await _get_setting(gdb, "tls_cert_pem")
        if not cert_pem:
            return

        from guidebook.acme import check_needs_renewal

        if not check_needs_renewal(cert_pem):
            return

        logger.info("ACME certificate needs renewal, starting provisioning")

        domain = await _get_setting(gdb, "acme_domain")
        endpoint = await _get_setting(gdb, "acme_endpoint")
        acme_dns_server = await _get_setting(gdb, "acme_dns_server")
        acme_dns_subdomain = await _get_setting(gdb, "acme_dns_subdomain")
        acme_dns_username = await _get_setting(gdb, "acme_dns_username")
        acme_dns_password = await _get_setting(gdb, "acme_dns_password")
        account_key_pem = await _get_setting(gdb, "acme_account_key_pem")
        account_url = await _get_setting(gdb, "acme_account_url")

        if not all(
            [
                domain,
                endpoint,
                acme_dns_server,
                acme_dns_subdomain,
                account_key_pem,
                account_url,
            ]
        ):
            logger.warning("ACME renewal skipped: incomplete configuration")
            return

        from guidebook.acme import acme_provision_cert
        from guidebook.sse import broadcast

        async with _provision_lock:
            cert_pem, key_pem = await acme_provision_cert(
                domain=domain,
                endpoint=endpoint,
                account_key_pem=account_key_pem,
                account_url=account_url,
                acmedns_server=acme_dns_server,
                acmedns_subdomain=acme_dns_subdomain,
                acmedns_username=acme_dns_username,
                acmedns_password=acme_dns_password,
            )

        await _set_setting(gdb, "tls_cert_pem", cert_pem)
        await _set_setting(gdb, "tls_key_pem", key_pem)
        await gdb.commit()

        broadcast("tls-cert-updated", {"restart_required": True})
        logger.info("ACME certificate renewed successfully")


async def start_acme_renewal(initial_delay: float = 300):
    """Start the background ACME renewal task."""
    global _acme_renewal_task
    if _acme_renewal_task is not None:
        return
    _acme_renewal_task = asyncio.create_task(_acme_renewal_loop(initial_delay))


async def stop_acme_renewal():
    """Cancel the background ACME renewal task."""
    global _acme_renewal_task
    if _acme_renewal_task is not None:
        _acme_renewal_task.cancel()
        try:
            await _acme_renewal_task
        except asyncio.CancelledError:
            pass
        _acme_renewal_task = None
