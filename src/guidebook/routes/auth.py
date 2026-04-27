import logging
import os
import secrets
import time

import jwt
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
AUTH_TTL_DEFAULT = 30 * 24 * 3600  # 30 days
AUTH_RENEW_COOLDOWN_DEFAULT = 24 * 3600  # 24 hours
LOGIN_LINK_TTL = 300  # 5 minutes (hardcoded)

# Set at startup by main.py
DISABLE_AUTH = False
AUTH_SLOTS: int = 1  # --auth-slots (default 1)
AUTH_TTL: int = AUTH_TTL_DEFAULT  # --auth-ttl (default 30d)
AUTH_RENEW_COOLDOWN: int = AUTH_RENEW_COOLDOWN_DEFAULT  # --auth-renew-cooldown
ALLOW_TRANSFER: bool = False  # --allow-transfer
PROXY_MODE: bool = False  # --proxy
TLS_ENABLED: bool = True  # True unless --no-tls
MTLS_MODE: str = "disabled"  # "disabled", "optional", "required"


def parse_duration(value: str) -> int:
    """Parse a duration string like '30d', '24h', '60m', or bare seconds."""
    value = value.strip()
    if not value:
        raise ValueError("Empty duration string")
    suffixes = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    if value[-1].lower() in suffixes:
        return int(value[:-1]) * suffixes[value[-1].lower()]
    return int(value)


# JWT signing secret — loaded at startup by main.py
JWT_SECRET: str = ""


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


def _create_jwt(session_id: int, nonce: str) -> str:
    return jwt.encode(
        {"sid": session_id, "iat": int(time.time()), "nonce": nonce},
        JWT_SECRET,
        algorithm="HS256",
    )


def _decode_jwt(token_str: str) -> dict | None:
    try:
        return jwt.decode(token_str, JWT_SECRET, algorithms=["HS256"])
    except (jwt.InvalidTokenError, Exception):
        return None


def _get_jwt_claims(request: Request) -> dict | None:
    cookie = request.cookies.get(AUTH_COOKIE_NAME)
    if not cookie:
        return None
    return _decode_jwt(cookie)


def _get_current_session_id(request: Request) -> int | None:
    claims = _get_jwt_claims(request)
    if not claims or "sid" not in claims:
        return None
    return claims["sid"]


async def _validate_token_by_raw(gdb: AsyncSession, token: str) -> AuthToken | None:
    """Validate a raw login/transfer token (from URL, not JWT)."""
    if not token:
        return None
    result = await gdb.execute(select(AuthToken).where(AuthToken.token == token))
    return result.scalar_one_or_none()


async def _validate_session(
    gdb: AsyncSession, session_id: int, nonce: str | None = None
) -> AuthToken | None:
    """Validate a session by ID (from JWT). Checks nonce if present."""
    if not session_id:
        return None
    result = await gdb.execute(select(AuthToken).where(AuthToken.id == session_id))
    token = result.scalar_one_or_none()
    if not token:
        return None
    # If the DB row has a nonce, the JWT must match it
    if token.jwt_nonce and token.jwt_nonce != nonce:
        return None
    return token


async def check_auth(request: Request, gdb: AsyncSession) -> bool:
    """Check if the current request is authenticated. Returns True if OK."""
    if not await _is_auth_enabled(gdb):
        return True
    claims = _get_jwt_claims(request)
    if not claims or "sid" not in claims:
        return False
    token = await _validate_session(gdb, claims["sid"], claims.get("nonce"))
    if not token:
        return False
    if token.expires_at and time.time() > token.expires_at:
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
    tls_enabled: bool
    proxy_mode: bool
    mtls_mode: str


@router.get("/status")
async def auth_status(
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    enabled = await _is_auth_enabled(gdb)
    session_id = _get_current_session_id(request)
    authenticated = False
    if session_id:
        tok = await _validate_session(gdb, session_id)
        authenticated = tok is not None
    if not enabled:
        authenticated = True  # no auth needed
    count = await _token_count(gdb)
    mtls_mode = await _get_setting(gdb, "mtls_mode") or "disabled"
    return AuthStatusResponse(
        enabled=enabled,
        authenticated=authenticated,
        slots=AUTH_SLOTS,
        session_count=count,
        allow_transfer=ALLOW_TRANSFER,
        tls_enabled=TLS_ENABLED,
        proxy_mode=PROXY_MODE,
        mtls_mode=mtls_mode,
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
    user_agent: str | None


@router.get("/sessions")
async def list_sessions(
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    current_session_id = _get_current_session_id(request)
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
                is_current=tok.id == current_session_id,
                is_transfer=tok.is_transfer == 1,
                user_agent=tok.user_agent,
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
    claims = _get_jwt_claims(request)
    if not claims or "sid" not in claims:
        raise HTTPException(401, "Not authenticated")
    if not await _validate_session(gdb, claims["sid"], claims.get("nonce")):
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
        raise HTTPException(
            400,
            "Session transfer is disabled. Use --allow-transfer at startup to enable it.",
        )
    enabled = await _is_auth_enabled(gdb)
    if not enabled:
        raise HTTPException(400, "Authentication is not enabled")

    claims = _get_jwt_claims(request)
    if not claims or "sid" not in claims:
        raise HTTPException(401, "Not authenticated")
    current_tok = await _validate_session(gdb, claims["sid"], claims.get("nonce"))
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
        raise HTTPException(401, "Invalid token")
    tok = await _validate_token_by_raw(gdb, token_str)
    if not tok:
        raise HTTPException(401, "Invalid token")
    if tok.last_seen_at is not None and not tok.is_transfer:
        raise HTTPException(401, "Token already used")
    if tok.last_seen_at is None and (time.time() - tok.created_at) > LOGIN_LINK_TTL:
        raise HTTPException(401, "Token expired")
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

    tok = await _validate_token_by_raw(gdb, token_str)
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

    tok.user_agent = request.headers.get("user-agent", "")

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
            await create_notification(
                "New session logged in",
                "A new browser session was authenticated via login link.",
            )
        except Exception:
            logger.warning("Failed to create login notification")
        logger.info("New session logged in via token")

    # Replace the raw login token with a new random value so the
    # original link token can never be reused even if leaked
    tok.token = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(16)
    tok.jwt_nonce = nonce
    await gdb.commit()

    # Issue a JWT containing the session ID — this is what goes in the cookie
    jwt_token = _create_jwt(tok.id, nonce)
    _set_auth_cookie(response, request, jwt_token)
    return LoginResponse(status="ok")


async def server_side_check_token(gdb: AsyncSession, token_str: str) -> str | None:
    """Check if a login token is valid. Returns None if valid, or an error message."""
    if not token_str:
        return "Invalid token"
    tok = await _validate_token_by_raw(gdb, token_str)
    if not tok:
        return "Invalid or expired token"
    if tok.last_seen_at is not None and not tok.is_transfer:
        return "This login link has already been used"
    if tok.last_seen_at is None and (time.time() - tok.created_at) > LOGIN_LINK_TTL:
        await gdb.delete(tok)
        await gdb.commit()
        return "Login link has expired"
    return None


async def server_side_login(
    gdb: AsyncSession, request: Request, response: Response, token_str: str
) -> str | None:
    """Complete a token login server-side. Returns None on success, or error message."""
    tok = await _validate_token_by_raw(gdb, token_str)
    if not tok:
        return "Invalid or expired token"
    if tok.last_seen_at is not None and not tok.is_transfer:
        return "This login link has already been used"
    if tok.last_seen_at is None and (time.time() - tok.created_at) > LOGIN_LINK_TTL:
        await gdb.delete(tok)
        await gdb.commit()
        return "Login link has expired"

    now = time.time()
    tok.expires_at = now + AUTH_TTL
    tok.user_agent = request.headers.get("user-agent", "")

    if tok.is_transfer:
        tok.is_transfer = 0
        tok.label = "Transferred session"
        tok.last_seen_at = now
        result = await gdb.execute(
            select(AuthToken).where(AuthToken.id != tok.id, AuthToken.is_transfer == 0)
        )
        for old_tok in result.scalars().all():
            await gdb.delete(old_tok)
        await gdb.commit()
        broadcast("auth-revoked", {})
        logger.info("Session transferred to new browser")
    else:
        tok.last_seen_at = now
        tok.label = "Logged in session"
        await gdb.commit()
        try:
            await create_notification(
                "New session logged in",
                "A new browser session was authenticated via login link.",
            )
        except Exception:
            logger.warning("Failed to create login notification")
        logger.info("New session logged in via token")

    tok.token = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(16)
    tok.jwt_nonce = nonce
    await gdb.commit()

    jwt_token = _create_jwt(tok.id, nonce)
    _set_auth_cookie(response, request, jwt_token)
    return None


def _is_secure(request: Request) -> bool:
    if PROXY_MODE:
        return request.url.scheme == "https"
    return TLS_ENABLED


def _set_auth_cookie(response: Response, request: Request, jwt_token: str) -> None:
    response.set_cookie(
        AUTH_COOKIE_NAME,
        jwt_token,
        max_age=AUTH_TTL,
        httponly=True,
        samesite="lax",
        secure=_is_secure(request),
        path="/",
    )


@router.post("/renew")
async def renew_session(
    request: Request,
    response: Response,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Renew the current session cookie if eligible."""
    claims = _get_jwt_claims(request)
    if not claims or "sid" not in claims:
        raise HTTPException(401, "Not authenticated")

    token = await _validate_session(gdb, claims["sid"], claims.get("nonce"))
    if not token:
        raise HTTPException(401, "Invalid session")

    if token.expires_at and time.time() > token.expires_at:
        raise HTTPException(401, "Session expired")

    # Check cooldown — JWT must be at least AUTH_RENEW_COOLDOWN old
    iat = claims.get("iat", 0)
    now = time.time()
    if now - iat < AUTH_RENEW_COOLDOWN:
        eligible_at = iat + AUTH_RENEW_COOLDOWN
        raise HTTPException(
            400,
            f"Too early to renew (eligible at {int(eligible_at)})",
        )

    # Rotate nonce — invalidates the old JWT immediately
    nonce = secrets.token_urlsafe(16)
    token.jwt_nonce = nonce
    token.expires_at = now + AUTH_TTL
    await gdb.commit()

    jwt_token = _create_jwt(token.id, nonce)
    _set_auth_cookie(response, request, jwt_token)
    logger.info("Session %d renewed", token.id)
    return {"status": "renewed"}


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: int,
    request: Request,
    gdb: AsyncSession = Depends(get_global_session),
):
    """Delete (logout) a session. Cannot delete current session."""
    current_sid = _get_current_session_id(request)

    result = await gdb.execute(select(AuthToken).where(AuthToken.id == session_id))
    tok = result.scalar_one_or_none()
    if not tok:
        raise HTTPException(404, "Session not found")

    if tok.id == current_sid:
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
    claims = _get_jwt_claims(request)
    if claims and "sid" in claims:
        tok = await _validate_session(gdb, claims["sid"], claims.get("nonce"))
        if tok:
            await gdb.delete(tok)
            await gdb.commit()
            logger.info("Session logged out")
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return {"status": "logged_out"}
