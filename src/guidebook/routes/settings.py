import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from guidebook.db import (
    INSTANCE_DEFAULTABLE_KEYS,
    InstanceSetting,
    Setting,
    _ensure_data_dir,
    db_manager,
    get_instance_session,
    get_session,
)

logger = logging.getLogger("guidebook")

THEME_BROADCAST_KEYS = {
    "theme",
    "theme_mode",
    "theme_contrast",
    "theme_brightness",
    "theme_hue",
    "theme_saturation",
    "theme_gradient",
    "theme_grain",
    "theme_glow",
    "theme_scanlines",
    "custom_theme_colors",
}

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingValue(BaseModel):
    value: str


class SettingResponse(BaseModel):
    key: str
    value: str | None
    source: str = "database"

    model_config = {"from_attributes": True}


HIDDEN_KEYS: set[str] = set()


def _redact(setting: Setting, source: str = "database") -> SettingResponse:
    if setting.key in HIDDEN_KEYS:
        return SettingResponse(
            key=setting.key,
            value="***" if setting.value else None,
            source=source,
        )
    return SettingResponse(key=setting.key, value=setting.value, source=source)


@router.get("/", response_model=list[SettingResponse])
async def list_settings(
    session: AsyncSession = Depends(get_session),
    gdb: AsyncSession = Depends(get_instance_session),
):
    result = await session.execute(select(Setting))
    db_settings = result.scalars().all()
    # Track which defaultable keys have a non-blank database value
    db_filled = {
        s.key
        for s in db_settings
        if s.key not in INSTANCE_DEFAULTABLE_KEYS or s.value
    }
    responses = [
        _redact(s, "database") for s in db_settings if s.key in db_filled
    ]

    # Fill in global defaults for defaultable keys that are missing or blank
    meta_result = await gdb.execute(
        select(InstanceSetting).where(InstanceSetting.key.in_(INSTANCE_DEFAULTABLE_KEYS))
    )
    for ms in meta_result.scalars().all():
        if ms.key not in db_filled and ms.value:
            responses.append(
                _redact(Setting(key=ms.key, value=ms.value), source="instance")
            )

    return responses


@router.get("/{key}", response_model=SettingResponse)
async def get_setting(
    key: str,
    session: AsyncSession = Depends(get_session),
    gdb: AsyncSession = Depends(get_instance_session),
):
    result = await session.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()
    if setting and setting.value:
        return _redact(setting, "database")
    # Fall back to global default if applicable
    if key in INSTANCE_DEFAULTABLE_KEYS:
        meta_result = await gdb.execute(
            select(InstanceSetting).where(InstanceSetting.key == key)
        )
        meta_setting = meta_result.scalar_one_or_none()
        if meta_setting and meta_setting.value:
            return _redact(
                Setting(key=meta_setting.key, value=meta_setting.value),
                source="instance",
            )
    if setting:
        return _redact(setting, "database")
    return SettingResponse(key=key, value=None)


@router.put("/{key}", response_model=SettingResponse)
async def upsert_setting(
    key: str, data: SettingValue, session: AsyncSession = Depends(get_session)
):
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = sqlite_insert(Setting).values(key=key, value=data.value)
    stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": data.value})
    await session.execute(stmt)
    await session.commit()
    log_value = "***" if key in HIDDEN_KEYS else data.value
    logger.info("Setting changed: %s = %s", key, log_value)

    if key in THEME_BROADCAST_KEYS:
        from guidebook.sse import broadcast

        broadcast("theme-changed", {})

    redacted_value = "***" if key in HIDDEN_KEYS and data.value else data.value
    return SettingResponse(key=key, value=redacted_value)


@router.post("/theme-preview")
async def theme_preview(data: SettingValue, key: str = ""):
    """Broadcast a live theme preview value via SSE without saving to DB."""
    from guidebook.sse import broadcast

    broadcast("theme-preview", {"key": key, "value": data.value})


@router.post("/backup")
async def backup_database():
    db_path = db_manager.db_path
    if not db_path or not db_path.exists():
        raise HTTPException(status_code=400, detail="No database is open")

    backup_dir = db_path.parent / "backups"
    try:
        _ensure_data_dir(backup_dir)
    except OSError as e:
        raise HTTPException(
            status_code=400, detail=f"Cannot create directory: {backup_dir}: {e}"
        ) from e

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%Sz")
    backup_name = f"{db_path.stem}_backup_{ts}{db_path.suffix}"
    backup_path = backup_dir / backup_name

    try:
        shutil.copy2(str(db_path), str(backup_path))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}") from e

    size = backup_path.stat().st_size
    logger.info("Manual backup: saved %s (%.1f KB)", backup_name, size / 1024)
    return {"path": str(backup_path), "size": size}


@router.get("/backup/db-info")
async def get_db_info():
    db_path = db_manager.db_path
    if not db_path or not db_path.exists():
        return {"path": None, "size": None, "directory": None}
    return {
        "path": str(db_path),
        "size": db_path.stat().st_size,
        "directory": str(db_path.parent / "backups"),
    }


@router.get("/backup/status")
async def backup_status(session: AsyncSession = Depends(get_session)):
    db_path = db_manager.db_path
    backup_dir = db_path.parent / "backups" if db_path else None

    async def _get(key: str, default: str = "") -> str:
        row = (
            await session.execute(select(Setting).where(Setting.key == key))
        ).scalar_one_or_none()
        return row.value if row and row.value else default

    enabled = (await _get("auto_backup_enabled", "true")).lower() == "true"
    hours = int(await _get("auto_backup_hours", "24"))
    max_backups = int(await _get("auto_backup_max", "10"))
    last_str = await _get("auto_backup_last", "")

    last_backup = None
    next_due = None
    if last_str:
        try:
            last_backup = datetime.fromisoformat(last_str)
            if enabled:
                next_due = last_backup + timedelta(hours=hours)
        except ValueError:
            pass
    elif enabled:
        next_due = datetime.now(timezone.utc)

    auto_count = 0
    manual_count = 0
    if backup_dir and backup_dir.is_dir():
        for f in backup_dir.glob("*.db"):
            if "_autobackup_" in f.name:
                auto_count += 1
            elif "_backup_" in f.name:
                manual_count += 1

    return {
        "auto_enabled": enabled,
        "interval_hours": hours,
        "max_backups": max_backups,
        "last_backup": last_backup.isoformat() if last_backup else None,
        "next_due": next_due.isoformat() if next_due else None,
        "auto_backup_count": auto_count,
        "manual_backup_count": manual_count,
    }


# --- Auto-backup background task ---

_auto_backup_task: asyncio.Task | None = None


async def _get_setting(key: str, default: str = "") -> str:
    async for session in get_session():
        row = (
            await session.execute(select(Setting).where(Setting.key == key))
        ).scalar_one_or_none()
        return row.value if row and row.value else default
    return default


async def _set_setting(key: str, value: str) -> None:
    from sqlalchemy.dialects.sqlite import insert

    async for session in get_session():
        stmt = insert(Setting).values(key=key, value=value)
        stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value})
        await session.execute(stmt)
        await session.commit()
        return


def _prune_auto_backups(backup_dir, stem: str, max_keep: int) -> int:
    pattern = f"{stem}_autobackup_*.db"
    files = sorted(backup_dir.glob(pattern), key=lambda f: f.name)
    to_delete = files[:-max_keep] if len(files) > max_keep else []
    for f in to_delete:
        f.unlink()
    if to_delete:
        logger.info(
            "Auto-backup: pruned %d old backups, keeping %d", len(to_delete), max_keep
        )
    return len(to_delete)


def _compute_next_due(last_str: str, hours: int) -> datetime:
    if last_str:
        try:
            return datetime.fromisoformat(last_str) + timedelta(hours=hours)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def _auto_backup_loop(initial_delay: float = 5):
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)  # brief delay on startup
    while True:
        try:
            enabled = (
                await _get_setting("auto_backup_enabled", "true")
            ).lower() == "true"
            if not enabled:
                await asyncio.sleep(60)
                continue

            hours = max(1, int(await _get_setting("auto_backup_hours", "24")))
            max_keep = max(
                1, min(100, int(await _get_setting("auto_backup_max", "10")))
            )
            last_str = await _get_setting("auto_backup_last", "")

            next_due = _compute_next_due(last_str, hours)
            now = datetime.now(timezone.utc)

            if now >= next_due:
                db_path = db_manager.db_path
                if db_path and db_path.exists():
                    backup_dir = db_path.parent / "backups"
                    _ensure_data_dir(backup_dir)
                    ts = now.strftime("%Y-%m-%d_%H%M%Sz")
                    name = f"{db_path.stem}_autobackup_{ts}{db_path.suffix}"
                    dest = backup_dir / name
                    shutil.copy2(str(db_path), str(dest))
                    size_kb = dest.stat().st_size / 1024
                    await _set_setting("auto_backup_last", now.isoformat())
                    next_after = now + timedelta(hours=hours)
                    logger.info(
                        "Auto-backup: saved %s (%.1f KB), next due at %s",
                        name,
                        size_kb,
                        next_after.strftime("%Y-%m-%d %H:%M:%Sz"),
                    )
                    _prune_auto_backups(backup_dir, db_path.stem, max_keep)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auto-backup error")
        await asyncio.sleep(60)


async def start_auto_backup(initial_delay: float = 5):
    global _auto_backup_task
    if _auto_backup_task is not None:
        return
    _auto_backup_task = asyncio.create_task(_auto_backup_loop(initial_delay))

    # Log initial status
    enabled = (await _get_setting("auto_backup_enabled", "true")).lower() == "true"
    if enabled:
        hours = int(await _get_setting("auto_backup_hours", "24"))
        last_str = await _get_setting("auto_backup_last", "")
        next_due = _compute_next_due(last_str, hours)
        logger.info(
            "Auto-backup enabled: next backup due at %s",
            next_due.strftime("%Y-%m-%d %H:%M:%Sz"),
        )
    else:
        logger.info("Auto-backup disabled")


async def stop_auto_backup():
    global _auto_backup_task
    if _auto_backup_task is not None:
        _auto_backup_task.cancel()
        try:
            await _auto_backup_task
        except asyncio.CancelledError:
            pass
        _auto_backup_task = None
