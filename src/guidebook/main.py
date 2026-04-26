import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from packaging.version import Version
from sqlalchemy import delete, select

from sqlalchemy.ext.asyncio import AsyncSession

from guidebook.db import (
    DatabaseLockError,
    DatabaseTooNewError,
    GlobalCache,
    Setting,
    db_manager,
    get_global_session,
    get_session,
    init_db,
    global_async_session,
)
from guidebook.routes.auth import router as auth_router
import guidebook.routes.auth as _auth_module
from guidebook.routes.databases import router as databases_router
from guidebook.routes.records import router as records_router
from guidebook.routes.attachments import router as attachments_router
from guidebook.routes.notifications import router as notifications_router
from guidebook.sse import (
    router as sse_router,
    stop_auto_shutdown as stop_sse_auto_shutdown,
)
from guidebook.routes.settings import (
    router as settings_router,
    start_auto_backup,
    stop_auto_backup,
)
from guidebook.routes.query import router as query_router
from guidebook.routes.global_settings import router as global_settings_router
from guidebook.routes.update import router as update_router
from guidebook.routes.scratchpad import router as scratchpad_router
from guidebook.routes.media import router as media_router
from guidebook._build_info import BUILD_GITHUB_ACTIONS, BUILD_ORIGIN_REPO, GIT_SHA

logger = logging.getLogger("guidebook")


def _resource_path(relative: str) -> Path:
    """Resolve path to bundled resource (works in both dev and PyInstaller)."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).parent
    return base / relative


def _handle_shutdown_signal(sig, frame):
    import signal
    import threading
    import time

    from guidebook.sse import notify_shutdown

    notify_shutdown()
    # Restore default handlers so a second signal force-quits
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    # Delay the actual shutdown so the event loop can flush SSE to clients
    def deferred_shutdown():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=deferred_shutdown, daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global NO_SHUTDOWN
    import signal

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    origin = BUILD_ORIGIN_REPO or "local build"
    sha = f" {GIT_SHA}" if GIT_SHA else ""
    logger.info("Guidebook v%s (%s%s)", version("guidebook"), origin, sha)
    if GITHUB_REPO != "EnigmaCurry/guidebook":
        logger.warning("Custom update source: BUILD_ORIGIN_REPO=%s", GITHUB_REPO)
    from guidebook.routes.update import _cleanup_old_binaries

    _cleanup_old_binaries()
    try:
        await init_db()
    except DatabaseTooNewError as e:
        logger.error("%s", e)
        print(f"Error: {e}", file=sys.stderr)
        os.kill(os.getpid(), signal.SIGTERM)
        yield
        return
    # Clear update check cache so we always check once on startup
    async with global_async_session() as gdb:
        await gdb.execute(
            delete(GlobalCache).where(
                GlobalCache.namespace == UPDATE_CACHE_NS,
                GlobalCache.key == UPDATE_CACHE_KEY,
            )
        )
        await gdb.commit()
    if db_manager.is_open:
        await start_auto_backup()
    yield
    await stop_sse_auto_shutdown()
    await stop_auto_backup()
    await db_manager.close()
    await db_manager.close_global()


app = FastAPI(title="Guidebook", version=version("guidebook"), lifespan=lifespan)


# Paths that bypass auth checking
_AUTH_EXEMPT_PREFIXES = ("/api/auth/",)


def _rate_limit_429(retry_after: int) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests"},
        headers={"Retry-After": str(retry_after)},
    )


@app.middleware("http")
async def http_middleware(request: Request, call_next):
    from guidebook.ratelimit import auth_limiter

    path = request.url.path
    client_ip = request.client.host if request.client else "unknown"

    # Rate limit: auth endpoints (check prior failures)
    if path.startswith("/api/auth/"):
        allowed, retry_after = auth_limiter.check(client_ip)
        if not allowed:
            return _rate_limit_429(retry_after)

    # Auth check for API routes
    if path.startswith("/api/") and not any(
        path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES
    ):
        from guidebook.routes.auth import check_auth
        from guidebook.db import db_manager

        if db_manager._global_session_factory:
            async with db_manager._global_session_factory() as gdb:
                ok = await check_auth(request, gdb)
                if not ok:
                    from fastapi.responses import JSONResponse

                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Authentication required"},
                    )

    # Auth check for non-API routes (HTML pages and static assets)
    if not path.startswith("/api/") and "auth_token" not in request.url.query:
        from guidebook.routes.auth import check_auth
        from guidebook.db import db_manager

        if db_manager._global_session_factory:
            async with db_manager._global_session_factory() as gdb:
                ok = await check_auth(request, gdb)
                if not ok:
                    from fastapi.responses import HTMLResponse

                    return HTMLResponse(
                        status_code=401,
                        content="<html><head><style>"
                        "body{background:#111;color:#ccc;font-family:sans-serif;"
                        "display:flex;align-items:center;justify-content:center;"
                        "min-height:100vh;margin:0;font-size:.95rem}"
                        ".box{max-width:420px;text-align:center;line-height:1.6}"
                        "code{background:#222;padding:2px 6px;border-radius:3px;font-size:.85rem;color:#e6a700}"
                        ".dim{color:#777;font-size:.8rem;margin-top:1.2rem}"
                        "</style></head><body>"
                        '<div class="box">'
                        "<p>You need a login link from the owner to access this site.</p>"
                        '<p class="dim">If you are the owner and have lost access, restart the server with '
                        "<code style='white-space:nowrap'>--reset-auth</code> to clear all sessions and generate a new login link.</p>"
                        "</div></body></html>",
                    )

    response: Response = await call_next(request)

    # Record auth failures for rate limiting (successful logins don't count)
    if path.startswith("/api/auth/") and response.status_code == 401:
        auth_limiter.record(client_ip)

    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    else:
        response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'wasm-unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'"
    )
    if _auth_module.PROXY_MODE:
        if request.headers.get("x-forwarded-proto", "").lower() == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
    elif _auth_module.TLS_ENABLED:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    if response.status_code >= 400:
        logger.warning(
            '%s - "%s %s" %s',
            request.client.host,
            request.method,
            path,
            response.status_code,
        )
    return response


NO_SHUTDOWN = os.environ.get("GUIDEBOOK_NO_SHUTDOWN", "").lower() in (
    "1",
    "true",
    "yes",
)


@app.get("/api/version")
async def get_version():
    return {
        "version": version("guidebook"),
        "no_shutdown": NO_SHUTDOWN,
        "frozen": getattr(sys, "frozen", False),
    }


GITHUB_REPO = BUILD_ORIGIN_REPO or "EnigmaCurry/guidebook"
UPDATE_CACHE_NS = "update_check"
UPDATE_CACHE_KEY = "latest"
UPDATE_CACHE_TTL = 3600  # 1 hour


@app.get("/api/update-check")
async def check_for_update(
    gdb: AsyncSession = Depends(get_global_session),
    session: AsyncSession = Depends(get_session),
    bust: bool = False,
):
    current = version("guidebook")

    # Only check updates for official GitHub Actions builds
    if not BUILD_GITHUB_ACTIONS:
        return {"current": current, "latest": None, "update_available": False}

    # Check if update checking is disabled (skip when bust=True so settings page can force-check)
    if not bust:
        row = (
            await session.execute(
                select(Setting).where(Setting.key == "update_check_enabled")
            )
        ).scalar_one_or_none()
        if row and row.value == "false":
            return {"current": current, "latest": None, "update_available": False}

    # Bust cache if requested
    if bust:
        await gdb.execute(
            delete(GlobalCache).where(
                GlobalCache.namespace == UPDATE_CACHE_NS,
                GlobalCache.key == UPDATE_CACHE_KEY,
            )
        )
        await gdb.commit()

    # Check cache
    cached = (
        await gdb.execute(
            select(GlobalCache).where(
                GlobalCache.namespace == UPDATE_CACHE_NS,
                GlobalCache.key == UPDATE_CACHE_KEY,
                GlobalCache.expires_at > time.time(),
            )
        )
    ).scalar_one_or_none()

    fresh_fetch = False
    if cached and cached.value:
        data = json.loads(cached.value)
        latest = data["latest"]
        url = data["url"]
        checked_at = data.get("checked_at", time.time())
    else:
        fresh_fetch = True
        # Fetch from GitHub
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                    headers={"Accept": "application/vnd.github+json"},
                    timeout=10,
                )
                resp.raise_for_status()
                release = resp.json()
                latest = release["tag_name"].lstrip("v")
                url = release["html_url"]
                logger.info(
                    "Update check (%s): current=%s, latest=%s",
                    GITHUB_REPO,
                    current,
                    latest,
                )
        except Exception:
            logger.info("Update check failed: could not reach GitHub")
            return {"current": current, "latest": None, "update_available": False}

        checked_at = time.time()

        # Store in cache
        await gdb.execute(
            delete(GlobalCache).where(
                GlobalCache.namespace == UPDATE_CACHE_NS,
                GlobalCache.key == UPDATE_CACHE_KEY,
            )
        )
        gdb.add(
            GlobalCache(
                namespace=UPDATE_CACHE_NS,
                key=UPDATE_CACHE_KEY,
                value=json.dumps(
                    {"latest": latest, "url": url, "checked_at": checked_at}
                ),
                expires_at=time.time() + UPDATE_CACHE_TTL,
            )
        )
        await gdb.commit()

    dev_suffixes = ("-dev", "-alpha", "-beta", "-rc")
    is_dev = any(s in current for s in dev_suffixes)
    is_exact = current == latest

    try:
        update_available = not is_dev and Version(latest) > Version(current)
    except Exception:
        update_available = not is_dev and latest != current

    # Check if this version was skipped by the user
    skipped = False
    if update_available:
        row = (
            await session.execute(
                select(Setting).where(Setting.key == "update_skip_version")
            )
        ).scalar_one_or_none()
        if row and row.value == latest:
            skipped = True

    result = {
        "current": current,
        "latest": latest,
        "update_available": update_available,
        "update_skipped": skipped,
        "is_dev": is_dev,
        "is_exact": is_exact,
        "url": url if update_available else None,
        "checked_at": checked_at,
        "next_check_at": checked_at + UPDATE_CACHE_TTL,
    }

    if fresh_fetch:
        from guidebook.sse import broadcast

        broadcast("update-check", result)

    return result


@app.post("/api/update-check/skip")
async def skip_update(
    gdb: AsyncSession = Depends(get_global_session),
    session: AsyncSession = Depends(get_session),
):
    """Skip the currently available update version."""
    cached = (
        await gdb.execute(
            select(GlobalCache).where(
                GlobalCache.namespace == UPDATE_CACHE_NS,
                GlobalCache.key == UPDATE_CACHE_KEY,
            )
        )
    ).scalar_one_or_none()
    if not cached or not cached.value:
        return {"status": "no_update"}
    data = json.loads(cached.value)
    latest = data.get("latest")
    if not latest:
        return {"status": "no_update"}

    row = (
        await session.execute(
            select(Setting).where(Setting.key == "update_skip_version")
        )
    ).scalar_one_or_none()
    if row:
        row.value = latest
    else:
        session.add(Setting(key="update_skip_version", value=latest))
    await session.commit()
    logger.info("Skipped update to v%s", latest)
    return {"status": "skipped", "version": latest}


app.include_router(auth_router)
app.include_router(databases_router)
app.include_router(records_router)
app.include_router(attachments_router)
app.include_router(settings_router)
app.include_router(global_settings_router)
app.include_router(notifications_router)
app.include_router(query_router)
app.include_router(update_router)
app.include_router(scratchpad_router)
app.include_router(media_router)
app.include_router(sse_router)

static_dir = _resource_path("static")
if static_dir.is_dir():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


def _detect_browser_name() -> str:
    import subprocess
    import webbrowser

    name = webbrowser.get().name
    if name == "xdg-open":
        try:
            result = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            desktop = result.stdout.strip()
            if desktop:
                return desktop.removesuffix(".desktop")
        except (OSError, subprocess.TimeoutExpired):
            pass
    return name


def _check_running_instance(
    host: str, port: int, no_browser: bool, pid: int | None = None
) -> bool:
    """Check if guidebook is already running at host:port.

    Returns True if we replaced the running instance (caller should continue startup).
    Calls sys.exit() if we should defer to the running instance.
    Returns False if nothing is running.
    """
    import json as _json
    import ssl as _ssl
    import urllib.request

    # Create an unverified SSL context for probing self-signed certs
    _noverify = _ssl.create_default_context()
    _noverify.check_hostname = False
    _noverify.verify_mode = _ssl.CERT_NONE

    # Quick probe — try HTTPS first, then HTTP
    url = None
    for _scheme in ("https", "http"):
        try:
            urllib.request.urlopen(
                f"{_scheme}://{host}:{port}/api/version",
                timeout=2,
                context=_noverify,
            )
            url = f"{_scheme}://{host}:{port}"
            break
        except Exception:
            continue
    if url is None:
        return False  # nothing running

    # Something is running — gather info
    running_version = None
    running_origin = None
    running_sha = None
    try:
        resp = urllib.request.urlopen(
            f"{url}/api/version", timeout=2, context=_noverify
        )
        running_version = _json.loads(resp.read()).get("version")
    except Exception:
        pass
    try:
        resp = urllib.request.urlopen(
            f"{url}/api/update/platform", timeout=2, context=_noverify
        )
        platform_info = _json.loads(resp.read())
        running_origin = platform_info.get("build_origin_repo")
        running_sha = platform_info.get("build_git_sha")
    except Exception:
        pass

    current = version("guidebook")
    my_origin = BUILD_ORIGIN_REPO or None
    my_sha = GIT_SHA or None
    same_lineage = (my_origin == running_origin) or (
        not my_origin and not running_origin
    )

    # Check if we should replace the running instance
    should_replace = False
    if same_lineage and running_version:
        if running_version != current:
            try:
                from packaging.version import Version

                should_replace = Version(current) > Version(running_version)
            except Exception:
                pass
        elif my_sha and running_sha and my_sha != running_sha:
            should_replace = True

    if should_replace and pid:
        import signal

        if running_version == current and my_sha:
            reason = f"v{current} ({running_sha} → {my_sha})"
        else:
            reason = f"v{running_version} → v{current}"
        print(f"Stopping Guidebook {reason} (PID {pid})...")
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
        except OSError:
            pass
        return True  # replaced — continue startup

    # Not replacing — either open browser or error out
    if not same_lineage:
        import platform as _platform

        running_desc = running_origin or "unknown"
        my_desc = my_origin or "unknown"
        pid_str = str(pid) if pid else "?"
        if _platform.system() == "Windows":
            kill_cmd = f"taskkill /PID {pid_str} /F"
        else:
            kill_cmd = f"kill {pid_str}"
        rv = running_version or "unknown"
        print(
            f"Error: Guidebook v{rv} is already running (PID {pid_str}) "
            f"from build origin {running_desc}.\n"
            f"This binary is v{current} from {my_desc}. "
            f"Stop the other instance first:\n"
            f"  {kill_cmd}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not no_browser:
        import webbrowser

        browser_name = _detect_browser_name()
        rv = running_version or current
        origin = running_origin or "local"
        print(
            f"Guidebook v{rv} ({origin}) is already running — opening {url} in {browser_name}"
        )
        webbrowser.open(url)
    else:
        print(f"Guidebook is already running on {url}")
    sys.exit(0)


def run() -> None:
    import argparse

    env_help = """
environment variables (overridden by command line options):
  GUIDEBOOK_DB                    Database name to open (default: guidebook)
  GUIDEBOOK_PICKER                Enable database picker mode (default: false)
  GUIDEBOOK_NO_BROWSER            Skip opening browser (default: false)
  GUIDEBOOK_NO_SHUTDOWN           Disable shutdown endpoint (default: false)
  GUIDEBOOK_HOST                  Bind address (default: 127.0.0.1)
  GUIDEBOOK_PORT                  Port (default: 4280)
  GUIDEBOOK_BROWSER_URL           Override browser URL
  GUIDEBOOK_DISABLE_AUTH          Disable authentication (default: false)
  GUIDEBOOK_AUTH_SLOTS            Max concurrent sessions (default: 1)
  GUIDEBOOK_AUTH_TTL              Session cookie TTL (e.g. 30d, 24h, 3600; default: 30d)
  GUIDEBOOK_AUTH_RENEW_COOLDOWN   Min time before cookie renewal (default: 24h)
  GUIDEBOOK_ALLOW_TRANSFER        Enable session transfer (default: false)
  GUIDEBOOK_NO_TLS                Disable TLS (default: false)
  GUIDEBOOK_PROXY                 Enable reverse proxy mode (default: false)
"""
    parser = argparse.ArgumentParser(
        description="Guidebook - Web Application Template",
        epilog=env_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"guidebook {version('guidebook')} ({BUILD_ORIGIN_REPO or 'local build'}{' ' + GIT_SHA if GIT_SHA else ''})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose/debug logging"
    )
    parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Database name to open (e.g. my-project, default: guidebook)",
    )
    parser.add_argument(
        "--pick",
        action="store_true",
        help="Enable database picker mode",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the browser automatically",
    )
    parser.add_argument(
        "--no-shutdown",
        action="store_true",
        help="Disable the shutdown endpoint and auto-shutdown",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=None,
        help="Port to listen on (default: auto-select starting from 4280)",
    )
    parser.add_argument(
        "--disable-auth",
        action="store_true",
        help="Disable authentication (allow unauthenticated access)",
    )
    parser.add_argument(
        "--reset-auth",
        action="store_true",
        help="Reset all auth sessions and generate a new login link",
    )
    parser.add_argument(
        "--auth-slots",
        type=int,
        default=None,
        help="Set maximum concurrent sessions (default: 1)",
    )
    parser.add_argument(
        "--auth-ttl",
        type=str,
        default=None,
        help="Session cookie TTL (e.g. 30d, 24h, 3600) (default: 30d)",
    )
    parser.add_argument(
        "--auth-renew-cooldown",
        type=str,
        default=None,
        help="Min time before cookie renewal (e.g. 24h, 1h) (default: 24h)",
    )
    parser.add_argument(
        "--allow-transfer",
        action="store_true",
        help="Enable session transfer (move session to another browser)",
    )
    parser.add_argument(
        "--no-tls",
        action="store_true",
        help="Disable TLS (serve plain HTTP instead of HTTPS)",
    )
    parser.add_argument(
        "--proxy",
        action="store_true",
        help="Enable reverse proxy mode (trust X-Forwarded-* headers)",
    )
    args = parser.parse_args()

    global NO_SHUTDOWN
    if args.disable_auth:
        _auth_module.DISABLE_AUTH = True
    if args.allow_transfer or os.environ.get(
        "GUIDEBOOK_ALLOW_TRANSFER", ""
    ).lower() in ("1", "true", "yes"):
        _auth_module.ALLOW_TRANSFER = True
    # Apply --auth-slots / GUIDEBOOK_AUTH_SLOTS
    if args.auth_slots is not None:
        _auth_module.AUTH_SLOTS = args.auth_slots
    else:
        val = os.environ.get("GUIDEBOOK_AUTH_SLOTS", "").strip()
        if val:
            try:
                _auth_module.AUTH_SLOTS = int(val)
            except ValueError:
                pass
    # Apply --auth-ttl / GUIDEBOOK_AUTH_TTL
    if args.auth_ttl is not None:
        try:
            _auth_module.AUTH_TTL = max(30, _auth_module.parse_duration(args.auth_ttl))
        except ValueError:
            print(f"Error: invalid --auth-ttl value: {args.auth_ttl}")
            sys.exit(1)
    else:
        val = os.environ.get("GUIDEBOOK_AUTH_TTL", "").strip()
        if val:
            try:
                _auth_module.AUTH_TTL = max(30, _auth_module.parse_duration(val))
            except ValueError:
                pass
    # Apply --auth-renew-cooldown / GUIDEBOOK_AUTH_RENEW_COOLDOWN
    if args.auth_renew_cooldown is not None:
        try:
            _auth_module.AUTH_RENEW_COOLDOWN = max(
                0, _auth_module.parse_duration(args.auth_renew_cooldown)
            )
        except ValueError:
            print(
                f"Error: invalid --auth-renew-cooldown value: {args.auth_renew_cooldown}"
            )
            sys.exit(1)
    else:
        val = os.environ.get("GUIDEBOOK_AUTH_RENEW_COOLDOWN", "").strip()
        if val:
            try:
                _auth_module.AUTH_RENEW_COOLDOWN = max(
                    0, _auth_module.parse_duration(val)
                )
            except ValueError:
                pass
    # Apply --no-tls / GUIDEBOOK_NO_TLS
    no_tls = args.no_tls or os.environ.get("GUIDEBOOK_NO_TLS", "").lower() in (
        "1",
        "true",
        "yes",
    )
    # Apply --proxy / GUIDEBOOK_PROXY
    proxy_mode = args.proxy or os.environ.get("GUIDEBOOK_PROXY", "").lower() in (
        "1",
        "true",
        "yes",
    )
    _auth_module.PROXY_MODE = proxy_mode
    _auth_module.TLS_ENABLED = not no_tls
    if args.name and args.name.startswith("__"):
        print(
            "Error: database name must not start with '__' (reserved for system databases)"
        )
        sys.exit(1)
    db_manager.configure(db_name=args.name, picker=args.pick)
    if args.no_shutdown:
        NO_SHUTDOWN = True

    # Ensure auth tables exist and handle --reset-auth
    if True:
        import secrets
        import sqlite3

        from guidebook.db import META_DB_PATH, _ensure_data_dir

        _ensure_data_dir(META_DB_PATH.parent)
        conn = sqlite3.connect(str(META_DB_PATH))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS auth_tokens "
            "(id INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL, label TEXT NOT NULL DEFAULT '', "
            "created_at REAL NOT NULL, last_seen_at REAL, expires_at REAL, last_ip TEXT, "
            "is_transfer INTEGER NOT NULL DEFAULT 0)"
        )
        for col in ("expires_at REAL", "last_ip TEXT", "jwt_nonce TEXT"):
            try:
                conn.execute(f"ALTER TABLE auth_tokens ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings "
            "(id INTEGER NOT NULL PRIMARY KEY, key VARCHAR NOT NULL UNIQUE, value VARCHAR)"
        )

        if args.reset_auth:
            conn.execute("DELETE FROM auth_tokens")
            # Regenerate JWT secret on auth reset to invalidate all JWTs
            jwt_secret = secrets.token_urlsafe(64)
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("auth_jwt_secret", jwt_secret),
            )
            token_str = secrets.token_urlsafe(48)
            now = time.time()
            conn.execute(
                "INSERT INTO auth_tokens (token, label, created_at, last_seen_at, expires_at, is_transfer) VALUES (?, ?, ?, ?, ?, ?)",
                (token_str, "Login link", now, None, None, 0),
            )
            conn.commit()
            os.environ["_GUIDEBOOK_RESET_AUTH_TOKEN"] = token_str
            print("Auth reset: all sessions cleared, new login token generated.")

        # Ensure JWT signing secret exists
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("auth_jwt_secret",)
        ).fetchone()
        if row:
            _auth_module.JWT_SECRET = row[0]
        else:
            jwt_secret = secrets.token_urlsafe(64)
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                ("auth_jwt_secret", jwt_secret),
            )
            conn.commit()
            _auth_module.JWT_SECRET = jwt_secret

        conn.close()

    import webbrowser

    if not db_manager.picker_mode:
        db_path = db_manager.default_db_path
        if db_path.exists() or not db_manager._db_override:
            try:
                db_manager.check_lock(db_path)
            except DatabaseLockError:
                no_browser = args.no_browser or os.environ.get(
                    "GUIDEBOOK_NO_BROWSER", ""
                ).lower() in ("1", "true", "yes")
                lock_info = db_manager.read_lock_info(db_path)
                if not lock_info or "host" not in lock_info:
                    lock_info = lock_info or {}
                    lock_info.setdefault(
                        "host", os.environ.get("GUIDEBOOK_HOST", "127.0.0.1")
                    )
                    lock_info.setdefault(
                        "port", int(os.environ.get("GUIDEBOOK_PORT", "4280"))
                    )
                _check_running_instance(
                    lock_info["host"],
                    lock_info["port"],
                    no_browser,
                    pid=lock_info.get("pid"),
                )

    log_level = "DEBUG" if args.verbose else "INFO"

    class ColorFormatter(logging.Formatter):
        COLORS = {
            logging.WARNING: "\033[33m",  # orange/yellow
            logging.ERROR: "\033[31m",  # red
            logging.CRITICAL: "\033[31;1m",  # bold red
        }
        RESET = "\033[0m"
        converter = time.gmtime

        def format(self, record):
            msg = super().format(record)
            color = self.COLORS.get(record.levelno)
            if color and sys.stderr.isatty():
                return f"{color}{msg}{self.RESET}"
            return msg

    _log_to_file = getattr(sys, "frozen", False) and (
        sys.platform == "win32"
        or (sys.platform == "darwin" and not sys.stderr.isatty())
    )
    if _log_to_file:
        from guidebook.db import DB_DIR, _ensure_data_dir

        _ensure_data_dir(DB_DIR)
        handler = logging.FileHandler(DB_DIR / "guidebook.log", encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s UTC %(levelname)s: %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(
            ColorFormatter(
                fmt="%(asctime)s UTC %(levelname)s: %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    logging.basicConfig(level=log_level, handlers=[handler])
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    host = os.environ.get("GUIDEBOOK_HOST", "")
    if not host:
        host = "127.0.0.1"
    port = args.port or int(os.environ.get("GUIDEBOOK_PORT", "4280"))

    scheme = "http" if no_tls else "https"

    # Check if guidebook is already running on this port
    no_browser = args.no_browser or os.environ.get(
        "GUIDEBOOK_NO_BROWSER", ""
    ).lower() in ("1", "true", "yes")
    _check_running_instance(host, port, no_browser)

    db_manager.set_listen_addr(host, port)

    import threading

    reset_token = os.environ.pop("_GUIDEBOOK_RESET_AUTH_TOKEN", "")
    if reset_token:
        login_url = f"{scheme}://{host}:{port}/?auth_token={reset_token}"
        print(f"Login URL: {login_url}")
    no_browser = args.no_browser or os.environ.get(
        "GUIDEBOOK_NO_BROWSER", ""
    ).lower() in ("1", "true", "yes")
    if not no_browser:
        default_url = f"{scheme}://{host}:{port}"
        if reset_token:
            default_url = f"{scheme}://{host}:{port}/?auth_token={reset_token}"
        env_browser_url = os.environ.get("GUIDEBOOK_BROWSER_URL", "").strip()

        def open_browser():
            import time

            time.sleep(1)
            url = env_browser_url or default_url
            browser_name = _detect_browser_name()
            logger.info("Opening %s in %s", url, browser_name)
            webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()

    ssl_kwargs = {}
    if not no_tls:
        from guidebook.db import META_DB_PATH
        from guidebook.tls import ensure_tls_cert, write_tls_temp_files

        cert_pem, key_pem = ensure_tls_cert(str(META_DB_PATH))
        certfile, keyfile = write_tls_temp_files(cert_pem, key_pem)
        ssl_kwargs = {"ssl_certfile": certfile, "ssl_keyfile": keyfile}
        logger.info("TLS enabled (self-signed certificate)")

    proxy_kwargs = {}
    if proxy_mode:
        proxy_kwargs = {"proxy_headers": True, "forwarded_allow_ips": "*"}
        logger.info("Reverse proxy mode enabled (trusting X-Forwarded-* headers)")

    uvicorn.run(
        app,
        host=host,
        port=port,
        access_log=False,
        log_config=None,
        **ssl_kwargs,
        **proxy_kwargs,
    )
