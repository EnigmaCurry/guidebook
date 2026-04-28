import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from guidebook.db import (
    INSTANCE_DEFAULTABLE_KEYS,
    INSTANCE_ONLY_KEYS,
    InstanceSetting,
    get_instance_session,
)

logger = logging.getLogger("guidebook")

router = APIRouter(prefix="/api/instance-settings", tags=["instance-settings"])

ALLOWED_KEYS = INSTANCE_DEFAULTABLE_KEYS | INSTANCE_ONLY_KEYS
HIDDEN_KEYS: set[str] = {"nats_ca_cert", "nats_client_cert", "nats_client_key"}


class SettingValue(BaseModel):
    value: str


class SettingResponse(BaseModel):
    key: str
    value: str | None

    model_config = {"from_attributes": True}


def _redact(setting: InstanceSetting) -> SettingResponse:
    if setting.key in HIDDEN_KEYS:
        return SettingResponse(key=setting.key, value="***" if setting.value else None)
    return SettingResponse.model_validate(setting)


@router.get("/", response_model=list[SettingResponse])
async def list_instance_settings(gdb: AsyncSession = Depends(get_instance_session)):
    result = await gdb.execute(select(InstanceSetting))
    return [_redact(s) for s in result.scalars().all() if s is not None]


@router.get("/{key}", response_model=SettingResponse)
async def get_instance_setting(key: str, gdb: AsyncSession = Depends(get_instance_session)):
    result = await gdb.execute(select(InstanceSetting).where(InstanceSetting.key == key))
    setting = result.scalar_one_or_none()
    if not setting:
        return SettingResponse(key=key, value=None)
    return _redact(setting)


@router.put("/{key}", response_model=SettingResponse)
async def upsert_instance_setting(
    key: str,
    data: SettingValue,
    gdb: AsyncSession = Depends(get_instance_session),
):
    if key not in ALLOWED_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Key '{key}' is not a valid global setting",
        )
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = sqlite_insert(InstanceSetting).values(key=key, value=data.value)
    stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": data.value})
    await gdb.execute(stmt)
    await gdb.commit()
    log_value = "***" if key in HIDDEN_KEYS else data.value
    logger.info("Global setting changed: %s = %s", key, log_value)

    if key == "disable_shutdown":
        import guidebook.main as _main
        from guidebook.sse import stop_auto_shutdown

        if data.value == "true":
            _main.NO_SHUTDOWN = True
            await stop_auto_shutdown()
        else:
            _main.NO_SHUTDOWN = False

    if key == "auto_shutdown_on_disconnect":
        from guidebook.main import NO_SHUTDOWN
        from guidebook.sse import start_auto_shutdown, stop_auto_shutdown

        if not NO_SHUTDOWN and data.value == "true":
            await start_auto_shutdown()
        else:
            await stop_auto_shutdown()

    if key == "nats_chat_enabled":
        from guidebook.chat import start_chat, stop_chat
        from guidebook.sse import broadcast

        if data.value == "true":
            await start_chat()
        else:
            await stop_chat()
        broadcast("chat-enabled", {"enabled": data.value == "true"})

    if key == "nats_lobby_enabled":
        from guidebook.chat import join_lobby, leave_lobby

        if data.value == "true":
            await join_lobby()
        else:
            await leave_lobby()

    redacted_value = "***" if key in HIDDEN_KEYS and data.value else data.value
    return SettingResponse(key=key, value=redacted_value)
