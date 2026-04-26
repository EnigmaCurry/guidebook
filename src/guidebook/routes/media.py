from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, field_serializer
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from guidebook.db import Attachment, Record, get_session

router = APIRouter(prefix="/api/media", tags=["media"])

MEDIA_TYPES = ("image/%", "video/%", "audio/%")


class MediaItem(BaseModel):
    id: int
    record_id: int
    record_title: str
    filename: str
    content_type: str
    size: int
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")

    model_config = {"from_attributes": True}


@router.get("/", response_model=list[MediaItem])
async def list_media(
    q: str | None = Query(None, description="Search query"),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(
            Attachment.id,
            Attachment.record_id,
            Record.title.label("record_title"),
            Attachment.filename,
            Attachment.content_type,
            Attachment.size,
            Attachment.created_at,
        )
        .join(Record, Attachment.record_id == Record.id)
        .where(
            or_(
                Attachment.content_type.like("image/%"),
                Attachment.content_type.like("video/%"),
                Attachment.content_type.like("audio/%"),
            )
        )
    )
    if q and len(q) >= 2:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                Record.title.ilike(pattern),
                Record.content.ilike(pattern),
                Record.tags.ilike(pattern),
            )
        )
    stmt = stmt.order_by(Attachment.created_at.desc())
    result = await session.execute(stmt)
    return [row._asdict() for row in result.all()]
