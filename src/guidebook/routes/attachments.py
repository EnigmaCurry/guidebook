import mimetypes
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_serializer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from guidebook.db import Attachment, Record, DB_DIR, db_manager, get_session
from guidebook.routes.records import _broadcast_records_changed

router = APIRouter(
    prefix="/api/records/{record_id}/attachments", tags=["attachments"]
)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def _attachments_dir(record_uuid: str) -> Path:
    db_name = db_manager.db_name or "guidebook"
    return DB_DIR / "attachments" / db_name / record_uuid


def _safe_filename(name: str, existing: set[str]) -> str:
    name = Path(name).name or "upload"
    if len(name) > 200:
        stem = Path(name).stem[:180]
        name = stem + Path(name).suffix
    if name not in existing:
        return name
    stem = Path(name).stem
    suffix = Path(name).suffix
    i = 1
    while f"{stem}_{i}{suffix}" in existing:
        i += 1
    return f"{stem}_{i}{suffix}"


class AttachmentResponse(BaseModel):
    id: int
    record_id: int
    filename: str
    content_type: str
    size: int
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")

    model_config = {"from_attributes": True}


async def _get_record(record_id: int, session: AsyncSession) -> Record:
    record = await session.get(Record, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


@router.get("/", response_model=list[AttachmentResponse])
async def list_attachments(
    record_id: int, session: AsyncSession = Depends(get_session)
):
    await _get_record(record_id, session)
    result = await session.execute(
        select(Attachment)
        .where(Attachment.record_id == record_id)
        .order_by(Attachment.created_at)
    )
    return result.scalars().all()


@router.post("/", response_model=list[AttachmentResponse], status_code=201)
async def upload_attachments(
    record_id: int,
    files: list[UploadFile],
    session: AsyncSession = Depends(get_session),
):
    record = await _get_record(record_id, session)
    att_dir = _attachments_dir(record.uuid)
    att_dir.mkdir(parents=True, exist_ok=True)

    existing = {p.name for p in att_dir.iterdir()} if att_dir.exists() else set()
    created = []

    for f in files:
        data = await f.read()
        if len(data) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413, detail=f"File too large: {f.filename}"
            )

        filename = _safe_filename(f.filename or "upload", existing)
        existing.add(filename)

        content_type = f.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        filepath = att_dir / filename
        filepath.write_bytes(data)

        att = Attachment(
            record_id=record_id,
            filename=filename,
            content_type=content_type,
            size=len(data),
        )
        session.add(att)
        created.append(att)

    await session.commit()
    for att in created:
        await session.refresh(att)
    _broadcast_records_changed()
    return created


@router.get("/{attachment_id}/download")
async def download_attachment(
    record_id: int,
    attachment_id: int,
    session: AsyncSession = Depends(get_session),
):
    record = await _get_record(record_id, session)
    att = await session.get(Attachment, attachment_id)
    if not att or att.record_id != record_id:
        raise HTTPException(status_code=404, detail="Attachment not found")

    filepath = _attachments_dir(record.uuid) / att.filename
    if not filepath.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(
        path=filepath,
        filename=att.filename,
        media_type=att.content_type,
    )


@router.delete("/{attachment_id}", status_code=204)
async def delete_attachment(
    record_id: int,
    attachment_id: int,
    session: AsyncSession = Depends(get_session),
):
    record = await _get_record(record_id, session)
    att = await session.get(Attachment, attachment_id)
    if not att or att.record_id != record_id:
        raise HTTPException(status_code=404, detail="Attachment not found")

    filepath = _attachments_dir(record.uuid) / att.filename
    if filepath.is_file():
        filepath.unlink()

    await session.delete(att)
    await session.commit()
    _broadcast_records_changed()


def cleanup_record_attachments(record: Record) -> None:
    """Remove attachment files from disk for a record."""
    if record.uuid:
        att_dir = _attachments_dir(record.uuid)
        if att_dir.is_dir():
            shutil.rmtree(att_dir)


async def delete_attachments_for_record(
    record_id: int, session: AsyncSession
) -> None:
    """Delete all attachment DB rows for a record."""
    await session.execute(
        delete(Attachment).where(Attachment.record_id == record_id)
    )
