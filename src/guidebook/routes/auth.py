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
AUTH_COOKIE_MAX_AGE = 365 * 24 * 3600  # 1 year
LOGIN_LINK_TTL_DEFAULT = 300  # 5 minutes

# Set at startup by main.py
REQUIRE_AUTH = False


def _env_require_auth() -> bool:
    return os.environ.get("GUIDEBOOK_REQUIRE_AUTH", "").lower() in (
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
    if REQUIRE_AUTH or _env_require_auth():
        return True
    val = await _get_setting(gdb, "auth_enabled")
    return val == "true"


async def _get_slots(gdb: AsyncSession) -> int:
    val = await _get_setting(gdb, "auth_slots")
    if val is not None:
        try:
            return int(val)
        except ValueError:
            pass
    return 1


async def _get_login_ttl(gdb: AsyncSession) -> int:
    val = await _get_setting(gdb, "login_link_ttl")
    if val is not None:
        try:
            return max(30, int(val))
        except ValueError:
            pass
    return LOGIN_LINK_TTL_DEFAULT


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
    configured = await _get_setting(gdb, "auth_configured")
    if configured != "true" and not REQUIRE_AUTH:
        return True  # Not yet configured, allow access (unless forced)
    token_str = _get_current_token(request)
    if not token_str:
        return False
    token = await _validate_token(gdb, token_str)
    if not token:
        return False
    # Update last_seen
    token.last_seen_at = time.time()
    await gdb.commit()
    return True


class AuthStatusResponse(BaseModel):
    enabled: bool
    required: bool  # forced by env/CLI
    configured: bool  # user has made their choice
    authenticated: bool
    env_require_auth: bool
    slots: int
    session_count: int
    login_link_ttl: int


@router.get("/status")
async def auth_status(
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    raw_enabled = await _get_setting(gdb, "auth_enabled")
    enabled = await _is_auth_enabled(gdb)
    configured = (await _get_setting(gdb, "auth_configured")) == "true"
    logger.info("auth/status: raw auth_enabled=%r enabled=%r configured=%r", raw_enabled, enabled, configured)
    token_str = _get_current_token(request)
    authenticated = False
    if token_str:
        tok = await _validate_token(gdb, token_str)
        authenticated = tok is not None
    if not enabled:
        authenticated = True  # no auth needed
    count = await _token_count(gdb)
    slots = await _get_slots(gdb)
    login_ttl = await _get_login_ttl(gdb)
    return AuthStatusResponse(
        enabled=enabled,
        required=REQUIRE_AUTH or _env_require_auth(),
        configured=configured,
        authenticated=authenticated,
        env_require_auth=_env_require_auth(),
        slots=slots,
        session_count=count,
        login_link_ttl=login_ttl,
    )


class SessionResponse(BaseModel):
    id: int
    label: str
    created_at: float
    last_seen_at: float | None
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
                is_current=tok.token == current_token,
                is_transfer=tok.is_transfer == 1,
            )
        )
    return sessions


class LockResponse(BaseModel):
    status: str
    token: str | None = None


@router.post("/lock")
async def lock_session(
    request: Request,
    response: Response,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Lock the server to the current browser session."""
    # Check if already has a valid token
    current_token = _get_current_token(request)
    if current_token:
        existing = await _validate_token(gdb, current_token)
        if existing:
            # Already locked — just mark as configured
            await _set_setting(gdb, "auth_enabled", "true")
            await _set_setting(gdb, "auth_configured", "true")
            await gdb.commit()
            return LockResponse(status="already_locked")

    token_str = secrets.token_urlsafe(48)
    now = time.time()
    gdb.add(
        AuthToken(
            token=token_str,
            label="Initial session",
            created_at=now,
            last_seen_at=now,
            is_transfer=0,
        )
    )
    await _set_setting(gdb, "auth_enabled", "true")
    await _set_setting(gdb, "auth_configured", "true")
    await gdb.commit()

    response.set_cookie(
        AUTH_COOKIE_NAME,
        token_str,
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    logger.info("Session locked to browser")
    return LockResponse(status="locked", token=token_str)


@router.post("/skip")
async def skip_auth(
    gdb: AsyncSession = Depends(get_global_session),
):
    """Skip auth — acknowledge the warning."""
    if REQUIRE_AUTH or _env_require_auth():
        raise HTTPException(
            400,
            "Cannot disable auth: GUIDEBOOK_REQUIRE_AUTH is set",
        )
    await _set_setting(gdb, "auth_enabled", "false")
    await _set_setting(gdb, "auth_configured", "true")
    await gdb.commit()
    logger.info("Authentication skipped by user")
    return {"status": "skipped"}


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
    slots = await _get_slots(gdb)
    count = await _token_count(gdb)
    if slots > 0 and count >= slots:
        raise HTTPException(
            400,
            f"All {slots} session slot(s) are in use. Remove a session first.",
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

    # Check if unused login link has expired
    login_ttl = await _get_login_ttl(gdb)
    if tok.last_seen_at is None and (time.time() - tok.created_at) > login_ttl:
        await gdb.delete(tok)
        await gdb.commit()
        raise HTTPException(401, "Login link has expired")

    if tok.is_transfer:
        # Transfer token: find the original session that created us and revoke it
        # The transfer token itself becomes the new permanent session
        tok.is_transfer = 0
        tok.label = "Transferred session"
        tok.last_seen_at = time.time()

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
        tok.last_seen_at = time.time()
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
        max_age=AUTH_COOKIE_MAX_AGE,
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


class AuthSettingsUpdate(BaseModel):
    auth_enabled: bool | None = None
    auth_slots: int | None = None
    login_link_ttl: int | None = None


@router.put("/settings")
async def update_auth_settings(
    data: AuthSettingsUpdate,
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Update auth settings."""
    if data.auth_enabled is not None:
        forced = REQUIRE_AUTH or _env_require_auth()
        if forced and not data.auth_enabled:
            raise HTTPException(
                400,
                "Cannot disable auth: GUIDEBOOK_REQUIRE_AUTH is set",
            )
        await _set_setting(
            gdb, "auth_enabled", "true" if data.auth_enabled else "false"
        )

        # If enabling auth and no token exists, create one for current browser
        if data.auth_enabled:
            count = await _token_count(gdb)
            if count == 0:
                token_str = secrets.token_urlsafe(48)
                now = time.time()
                gdb.add(
                    AuthToken(
                        token=token_str,
                        label="Initial session",
                        created_at=now,
                        last_seen_at=now,
                        is_transfer=0,
                    )
                )
                # We'll need to set the cookie in the response
                # But since this is a settings update, the frontend will handle it
                await _set_setting(gdb, "auth_configured", "true")

        logger.info("Auth enabled: %s", data.auth_enabled)

    if data.auth_slots is not None:
        if data.auth_slots < 0:
            raise HTTPException(400, "Slots must be >= 0")
        await _set_setting(gdb, "auth_slots", str(data.auth_slots))
        logger.info("Auth slots: %d", data.auth_slots)

    if data.login_link_ttl is not None:
        if data.login_link_ttl < 30:
            raise HTTPException(400, "TTL must be >= 30 seconds")
        await _set_setting(gdb, "login_link_ttl", str(data.login_link_ttl))
        logger.info("Login link TTL: %d", data.login_link_ttl)

    await gdb.commit()
    return {"status": "ok"}


@router.post("/enable-and-lock")
async def enable_and_lock(
    request: Request,
    response: Response,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Enable auth and lock to current browser in one step (used from settings)."""
    current_token = _get_current_token(request)

    # If already has a valid token, just enable
    if current_token:
        existing = await _validate_token(gdb, current_token)
        if existing:
            await _set_setting(gdb, "auth_enabled", "true")
            await _set_setting(gdb, "auth_configured", "true")
            await gdb.commit()
            v1 = await _get_setting(gdb, "auth_enabled")
            v2 = await _get_setting(gdb, "auth_configured")
            # Also verify via raw SQL to bypass ORM caching
            from sqlalchemy import text
            raw = (await gdb.execute(text("SELECT key, value FROM settings WHERE key IN ('auth_enabled', 'auth_configured')"))).fetchall()
            logger.info(
                "enable-and-lock already_locked: orm auth_enabled=%r auth_configured=%r raw=%r",
                v1, v2, raw,
            )
            return {"status": "already_locked"}

    token_str = secrets.token_urlsafe(48)
    now = time.time()
    gdb.add(
        AuthToken(
            token=token_str,
            label="Initial session",
            created_at=now,
            last_seen_at=now,
            is_transfer=0,
        )
    )
    await _set_setting(gdb, "auth_enabled", "true")
    await _set_setting(gdb, "auth_configured", "true")
    await gdb.commit()

    response.set_cookie(
        AUTH_COOKIE_NAME,
        token_str,
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    logger.info("Auth enabled and session locked from settings")
    return {"status": "locked"}
