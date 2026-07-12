import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class SelectionIn(BaseModel):
    source_id: uuid.UUID
    page_no: int = Field(ge=1)
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("selection text must not be blank")
        return v.strip()


class SelectionOut(BaseModel):
    selection_id: uuid.UUID
    source_id: uuid.UUID
    source_title: str
    page_no: int
    text: str


class LessonListItem(BaseModel):
    """`GET /lessons` row shape. `provenance` is lifted out of the lesson
    root Block's `target_profile` JSON column (`{"provenance": {"source_id":
    ..., "page_no": ...}}`, set by `app.lessons.draft.draft_lesson_from_
    selection`, B3) so the UI can show "from <book>, p.21" without also
    having to know where provenance is stored — same reasoning as
    `CurriculumListItem` (`app/schemas/curriculum.py`) exposing `target_
    profile` directly, just narrowed to the one key list callers actually
    want.
    """
    id: uuid.UUID
    title: str
    created_at: datetime
    provenance: dict | None = None


class SplitSessionRequest(BaseModel):
    session_minutes: int = Field(gt=0)


class MergeSessionsRequest(BaseModel):
    session_ids: list[uuid.UUID] = Field(min_length=2)


class AddSessionRequest(BaseModel):
    title: str = Field(min_length=1)
    est_minutes: int | None = Field(default=None, gt=0)
    after: uuid.UUID | None = None
