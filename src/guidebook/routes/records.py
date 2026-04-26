import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_serializer, field_validator
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from guidebook.db import Attachment, Record, get_session
from guidebook.sse import _get_shutdown_event

logger = logging.getLogger("guidebook")

router = APIRouter(prefix="/api/records", tags=["records"])

_subscribers: list[asyncio.Queue[str]] = []


def _broadcast_records_changed() -> None:
    msg = f"event: records-changed\ndata: {json.dumps({})}\n\n"
    for q in list(_subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


async def _sse_generator(queue: asyncio.Queue[str], request: Request):
    shutdown_evt = _get_shutdown_event()
    try:
        while not shutdown_evt.is_set():
            if await request.is_disconnected():
                return
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=5)
                yield msg
            except asyncio.TimeoutError:
                yield "event: keepalive\ndata: {}\n\n"
            if shutdown_evt.is_set():
                while not queue.empty():
                    yield queue.get_nowait()
                return
    except (asyncio.CancelledError, GeneratorExit):
        return


@router.get("/stream")
async def records_stream(request: Request):
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
    _subscribers.append(queue)

    async def cleanup_generator():
        try:
            async for msg in _sse_generator(queue, request):
                yield msg
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            if queue in _subscribers:
                _subscribers.remove(queue)

    return StreamingResponse(
        cleanup_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class RecordCreate(BaseModel):
    title: str
    content: str | None = None
    tags: str | None = None
    timestamp: datetime | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title must not be empty")
        return v

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v


class RecordUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    tags: str | None = None
    timestamp: datetime | None = None

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v


class RecordResponse(BaseModel):
    id: int
    uuid: str | None
    title: str
    content: str | None
    tags: str | None
    timestamp: datetime
    updated_at: datetime | None = None

    @field_serializer("timestamp")
    def serialize_timestamp(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")

    @field_serializer("updated_at")
    def serialize_updated_at(self, v: datetime | None) -> str | None:
        if v is None:
            return None
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")

    model_config = {"from_attributes": True}


@router.get("/", response_model=list[RecordResponse])
async def list_records(
    q: str | None = Query(None, description="Search query"),
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Record)
    if q and len(q) >= 2:
        pattern = f"%{q}%"
        stmt = (
            stmt.outerjoin(Attachment, Attachment.record_id == Record.id)
            .where(
                or_(
                    Record.title.ilike(pattern),
                    Record.content.ilike(pattern),
                    Record.tags.ilike(pattern),
                    Record.uuid.ilike(pattern),
                    Attachment.filename.ilike(pattern),
                    Attachment.content_type.ilike(pattern),
                )
            )
            .distinct()
        )
    stmt = stmt.order_by(Record.timestamp.desc()).offset(offset).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


@router.post("/", response_model=RecordResponse, status_code=201)
async def create_record(
    data: RecordCreate, session: AsyncSession = Depends(get_session)
):
    fields = data.model_dump(exclude_unset=True)
    fields["updated_at"] = fields.get("timestamp") or datetime.now(timezone.utc)
    record = Record(**fields)
    session.add(record)
    await session.commit()
    await session.refresh(record)
    _broadcast_records_changed()
    return record


@router.get("/{record_id}", response_model=RecordResponse)
async def get_record(record_id: int, session: AsyncSession = Depends(get_session)):
    record = await session.get(Record, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


@router.put("/{record_id}", response_model=RecordResponse)
async def update_record(
    record_id: int,
    data: RecordUpdate,
    session: AsyncSession = Depends(get_session),
):
    record = await session.get(Record, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(record, key, value)
    record.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(record)
    _broadcast_records_changed()
    return record


@router.delete("/all")
async def delete_all_records(session: AsyncSession = Depends(get_session)):
    from guidebook.routes.attachments import cleanup_record_attachments

    records = (await session.execute(select(Record))).scalars().all()
    for r in records:
        cleanup_record_attachments(r)
    await session.execute(delete(Attachment))
    result = await session.execute(delete(Record))
    await session.commit()
    logger.info("Deleted all records: %d removed", result.rowcount)
    _broadcast_records_changed()
    return {"deleted": result.rowcount}


@router.delete("/{record_id}", status_code=204)
async def delete_record(record_id: int, session: AsyncSession = Depends(get_session)):
    from guidebook.routes.attachments import (
        cleanup_record_attachments,
        delete_attachments_for_record,
    )

    record = await session.get(Record, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    cleanup_record_attachments(record)
    await delete_attachments_for_record(record_id, session)
    await session.delete(record)
    await session.commit()
    _broadcast_records_changed()
