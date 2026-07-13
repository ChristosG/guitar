import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class SelectionIn(BaseModel):
    """The Reader's "author a lesson from this" payload (Plan 10 Task 1,
    extended Plan 12 Task 4 / G4 for a selection that spans pages).

    `page_from`/`page_to` are the current shape — a selection made in the
    continuous-scroll Reader can cross a page boundary, so the passage is a
    RANGE, not a single page. `page_no` is kept accepted for backward
    compatibility (older clients, existing tests, and anything still posting
    the single-page shape) and is resolved into an equivalent one-page range
    below rather than threaded through as a separate code path. Exactly one
    of "page_no" or "page_from"+"page_to" must be given.
    """
    source_id: uuid.UUID
    text: str = Field(min_length=1)
    page_no: int | None = Field(default=None, ge=1)
    page_from: int | None = Field(default=None, ge=1)
    page_to: int | None = Field(default=None, ge=1)

    @field_validator("text")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("selection text must not be blank")
        return v.strip()

    @model_validator(mode="after")
    def _resolve_page_range(self) -> "SelectionIn":
        if self.page_from is None and self.page_to is None:
            if self.page_no is None:
                raise ValueError("either page_no or page_from/page_to is required")
            self.page_from = self.page_no
            self.page_to = self.page_no
        elif self.page_from is None or self.page_to is None:
            raise ValueError("page_from and page_to must both be given together")
        elif self.page_to < self.page_from:
            raise ValueError("page_to must be >= page_from")
        # Normalize page_no to the range's start so any code still reading
        # `payload.page_no` directly sees a value consistent with the range.
        self.page_no = self.page_from
        return self


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
