from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, field_serializer
from sqlalchemy import and_, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from guidebook.db import Attachment, Record, get_session

router = APIRouter(prefix="/api/media", tags=["media"])

MEDIA_TYPES = ("image/%", "video/%", "audio/%")

TYPE_FILTERS = {
    "image": [Attachment.content_type.like("image/%")],
    "video": [Attachment.content_type.like("video/%")],
    "audio": [Attachment.content_type.like("audio/%")],
    "document": [
        not_(Attachment.content_type.like("image/%")),
        not_(Attachment.content_type.like("video/%")),
        not_(Attachment.content_type.like("audio/%")),
    ],
}


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
    type: str | None = Query(None, description="Filter: image, video, audio, document"),
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
    )
    if type and type in TYPE_FILTERS:
        stmt = stmt.where(and_(*TYPE_FILTERS[type]))
    else:
        # "all" — no content_type restriction, show every attachment
        pass
    if q and len(q) >= 2:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                Record.title.ilike(pattern),
                Record.content.ilike(pattern),
                Record.tags.ilike(pattern),
                Attachment.filename.ilike(pattern),
                Attachment.content_type.ilike(pattern),
            )
        )
    stmt = stmt.order_by(Attachment.created_at.desc())
    result = await session.execute(stmt)
    return [row._asdict() for row in result.all()]
