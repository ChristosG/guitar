"""Pydantic request/response models for the Notes API (`routers/notes.py`).

Kept separate from the SQLAlchemy `app.models.note.Note` model per this
codebase's established split (mirrors `schemas/artifacts.py` vs `app.models.
artifact`, `schemas/students.py` vs `app.models.student`) — this module is
only the HTTP boundary's shape; it holds no DB logic.
"""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class NoteCreate(BaseModel):
    title: str = Field(max_length=300)
    body: str
    tags: list[str] = []
    student_id: UUID | None = None


class NoteUpdate(BaseModel):
    """All fields optional — PATCH semantics. `exclude_unset=True,
    exclude_none=True` in the router (mirrors `routers.curriculum.
    update_block`'s own reviewed fix for the identical shape): `Note.title`/
    `Note.body` are NOT NULL columns, so an explicit `null` is treated as a
    no-op for EVERY field here, uniformly, rather than only the NOT NULL
    ones — one rule for all of them, at the accepted cost that `student_id`
    (the one genuinely nullable field) also can't be cleared back to `null`
    via PATCH this way. See `routers/notes.py`'s `update_note` docstring.
    """
    title: str | None = Field(default=None, max_length=300)
    body: str | None = None
    tags: list[str] | None = None
    student_id: UUID | None = None


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    body: str
    tags: list[str]
    student_id: UUID | None
    promoted_to_knowledge: bool
    created_at: datetime
    updated_at: datetime


class NotePromoteOut(NoteOut):
    """`POST /notes/{id}/promote`'s response: the updated Note plus the id
    of the `KnowledgeSource` this call just created. `source_id` is NOT a
    column on `Note` (this task's model has no such field — see `app.
    models.note`); it's synthesized here, once, from `promote_note`'s return
    value, since a caller acting on a just-promoted note (e.g. "open it in
    the Brain") needs this id and `KnowledgeSource.title` has no uniqueness
    constraint to reliably look it back up by later.
    """
    source_id: UUID
