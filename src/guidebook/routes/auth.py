import logging
import os
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from guidebook.db import AuthToken, GlobalSetting, get_global_session
from guidebook.routes.notifications import create_notification
from guidebook.sse import broadcast

logger = logging.getLogger("guidebook")

router = APIRouter(prefix="/api/auth", tags=["auth"])

AUTH_COOKIE_NAME = "guidebook_token"
AUTH_COOKIE_MAX_AGE_DEFAULT = 10 * 365 * 24 * 3600  # 10 years
LOGIN_LINK_TTL = 300  # 5 minutes (hardcoded)

# Set at startup by main.py
DISABLE_AUTH = False
AUTH_SLOTS: int = 1  # --auth-slots (default 1)
AUTH_TTL: int = AUTH_COOKIE_MAX_AGE_DEFAULT  # --auth-ttl (default 10 years)
ALLOW_TRANSFER: bool = False  # --allow-transfer


def _env_disable_auth() -> bool:
    return os.environ.get("GUIDEBOOK_DISABLE_AUTH", "").lower() in (
        "1",
        "true",
        "yes",
    )


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


async def _is_auth_enabled(gdb: AsyncSession) -> bool:
    if DISABLE_AUTH or _env_disable_auth():
        return False
    return True


async def _token_count(gdb: AsyncSession) -> int:
    result = await gdb.execute(
        select(func.count()).select_from(AuthToken).where(AuthToken.is_transfer == 0)
    )
    return result.scalar() or 0


def _get_current_token(request: Request) -> str | None:
    return request.cookies.get(AUTH_COOKIE_NAME)


async def _validate_token(gdb: AsyncSession, token: str) -> AuthToken | None:
    if not token:
        return None
    result = await gdb.execute(select(AuthToken).where(AuthToken.token == token))
    return result.scalar_one_or_none()


async def check_auth(request: Request, gdb: AsyncSession) -> bool:
    """Check if the current request is authenticated. Returns True if OK."""
    if not await _is_auth_enabled(gdb):
        return True
    token_str = _get_current_token(request)
    if not token_str:
        return False
    token = await _validate_token(gdb, token_str)
    if not token:
        return False
    # Update last_seen and IP
    token.last_seen_at = time.time()
    token.last_ip = request.client.host if request.client else None
    await gdb.commit()
    return True


class AuthStatusResponse(BaseModel):
    enabled: bool
    authenticated: bool
    slots: int
    session_count: int
    allow_transfer: bool


@router.get("/status")
async def auth_status(
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    enabled = await _is_auth_enabled(gdb)
    token_str = _get_current_token(request)
    authenticated = False
    if token_str:
        tok = await _validate_token(gdb, token_str)
        authenticated = tok is not None
    if not enabled:
        authenticated = True  # no auth needed
    count = await _token_count(gdb)
    return AuthStatusResponse(
        enabled=enabled,
        authenticated=authenticated,
        slots=AUTH_SLOTS,
        session_count=count,
        allow_transfer=ALLOW_TRANSFER,
    )


class SessionResponse(BaseModel):
    id: int
    label: str
    created_at: float
    last_seen_at: float | None
    expires_at: float | None
    last_ip: str | None
    is_current: bool
    is_transfer: bool


@router.get("/sessions")
async def list_sessions(
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    current_token = _get_current_token(request)
    result = await gdb.execute(select(AuthToken).order_by(AuthToken.created_at))
    sessions = []
    for tok in result.scalars().all():
        sessions.append(
            SessionResponse(
                id=tok.id,
                label=tok.label,
                created_at=tok.created_at,
                last_seen_at=tok.last_seen_at,
                expires_at=tok.expires_at,
                last_ip=tok.last_ip,
                is_current=tok.token == current_token,
                is_transfer=tok.is_transfer == 1,
            )
        )
    return sessions


class GenerateTokenResponse(BaseModel):
    token: str
    login_url: str


@router.post("/generate-token")
async def generate_token(
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Generate a login token for a new session."""
    enabled = await _is_auth_enabled(gdb)
    if not enabled:
        raise HTTPException(400, "Authentication is not enabled")

    # Check current user is authenticated
    current_token = _get_current_token(request)
    if not current_token or not await _validate_token(gdb, current_token):
        raise HTTPException(401, "Not authenticated")

    # Check slot availability
    count = await _token_count(gdb)
    if AUTH_SLOTS > 0 and count >= AUTH_SLOTS:
        raise HTTPException(
            400,
            f"All {AUTH_SLOTS} session slot(s) are in use. Remove a session first.",
        )

    token_str = secrets.token_urlsafe(48)
    gdb.add(
        AuthToken(
            token=token_str,
            label="Invited session",
            created_at=time.time(),
            last_seen_at=None,
            is_transfer=0,
        )
    )
    await gdb.commit()

    base_url = str(request.base_url).rstrip("/")
    login_url = f"{base_url}/?auth_token={token_str}"
    logger.info("Generated new login token")
    return GenerateTokenResponse(token=token_str, login_url=login_url)


@router.post("/transfer")
async def transfer_session(
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Generate a transfer token — logs out current session when new one logs in."""
    if not ALLOW_TRANSFER:
        raise HTTPException(400, "Session transfer is disabled. Use --allow-transfer at startup to enable it.")
    enabled = await _is_auth_enabled(gdb)
    if not enabled:
        raise HTTPException(400, "Authentication is not enabled")

    current_token_str = _get_current_token(request)
    if not current_token_str:
        raise HTTPException(401, "Not authenticated")
    current_tok = await _validate_token(gdb, current_token_str)
    if not current_tok:
        raise HTTPException(401, "Not authenticated")

    token_str = secrets.token_urlsafe(48)
    gdb.add(
        AuthToken(
            token=token_str,
            label="Transfer session",
            created_at=time.time(),
            last_seen_at=None,
            is_transfer=1,
        )
    )
    await gdb.commit()

    base_url = str(request.base_url).rstrip("/")
    login_url = f"{base_url}/?auth_token={token_str}"
    logger.info("Generated transfer token")
    return GenerateTokenResponse(token=token_str, login_url=login_url)


@router.post("/check-token")
async def check_token(
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Check if a login token is still valid (without consuming it)."""
    body = await request.json()
    token_str = body.get("token", "")
    if not token_str:
        return {"valid": False}
    tok = await _validate_token(gdb, token_str)
    if not tok:
        return {"valid": False}
    if tok.last_seen_at is not None and not tok.is_transfer:
        return {"valid": False}
    if tok.last_seen_at is None and (time.time() - tok.created_at) > LOGIN_LINK_TTL:
        return {"valid": False}
    return {"valid": True}


class LoginResponse(BaseModel):
    status: str


@router.post("/login")
async def login_with_token(
    request: Request,
    response: Response,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Login with a token (from URL parameter)."""
    body = await request.json()
    token_str = body.get("token", "")
    if not token_str:
        raise HTTPException(400, "Token required")

    tok = await _validate_token(gdb, token_str)
    if not tok:
        raise HTTPException(401, "Invalid or expired token")

    # Login links are one-time use — reject if already consumed
    if tok.last_seen_at is not None and not tok.is_transfer:
        raise HTTPException(401, "This login link has already been used")

    # Check if unused login link has expired
    if tok.last_seen_at is None and (time.time() - tok.created_at) > LOGIN_LINK_TTL:
        await gdb.delete(tok)
        await gdb.commit()
        raise HTTPException(401, "Login link has expired")

    now = time.time()
    tok.expires_at = now + AUTH_TTL

    if tok.is_transfer:
        # Transfer token: find the original session that created us and revoke it
        # The transfer token itself becomes the new permanent session
        tok.is_transfer = 0
        tok.label = "Transferred session"
        tok.last_seen_at = now

        # Delete all other non-transfer tokens (the old session)
        result = await gdb.execute(
            select(AuthToken).where(AuthToken.id != tok.id, AuthToken.is_transfer == 0)
        )
        for old_tok in result.scalars().all():
            await gdb.delete(old_tok)

        await gdb.commit()
        broadcast("auth-revoked", {})
        logger.info("Session transferred to new browser")
    else:
        # Regular login token — just activate it
        tok.last_seen_at = now
        tok.label = "Logged in session"
        await gdb.commit()
        try:
            await create_notification("New session logged in", "A new browser session was authenticated via login link.")
        except Exception:
            logger.warning("Failed to create login notification")
        logger.info("New session logged in via token")

    response.set_cookie(
        AUTH_COOKIE_NAME,
        token_str,
        max_age=AUTH_TTL,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return LoginResponse(status="ok")


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: int,
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Delete (logout) a session. Cannot delete current session."""
    current_token = _get_current_token(request)

    result = await gdb.execute(select(AuthToken).where(AuthToken.id == session_id))
    tok = result.scalar_one_or_none()
    if not tok:
        raise HTTPException(404, "Session not found")

    if tok.token == current_token:
        raise HTTPException(400, "Cannot delete your own session")

    await gdb.delete(tok)
    await gdb.commit()
    broadcast("auth-revoked", {})
    logger.info("Deleted session %d", session_id)
    return {"status": "deleted"}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Log out the current session."""
    token_str = _get_current_token(request)
    if token_str:
        tok = await _validate_token(gdb, token_str)
        if tok:
            await gdb.delete(tok)
            await gdb.commit()
            logger.info("Session logged out")
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return {"status": "logged_out"}




