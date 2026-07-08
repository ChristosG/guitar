"""Pydantic request/response models for the Students API (`routers/students.py`).

Kept separate from the SQLAlchemy `app.models.student.Student` model per this
codebase's established split (mirrors `schemas/knowledge.py` vs
`app.models.knowledge`) — this module is only the HTTP boundary's shape; it
holds no DB logic.

NOTE: the Curriculum plan's task brief lists an optional `goals` field on
`POST /students`, but the current `Student` model (Plan 3 Task 1, out of
scope for this task) has no backing column for it — `status`, `level`,
`instrument`, `birthdate`, `preferred_language`, `name` are the whole row.
Rather than accept `goals` and silently drop it (deceptive: the client would
believe it was saved), it's omitted here entirely; see this task's report.
"""
from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class StudentCreate(BaseModel):
    name: str
    birthdate: date | None = None
    level: str | None = None
    instrument: str | None = None
    preferred_language: str = "el"  # mirrors Student.preferred_language's own default


class StudentUpdate(BaseModel):
    """All fields optional — PATCH semantics. The router applies only the
    fields the client actually sent (`model_dump(exclude_unset=True)`), so
    an omitted field leaves the stored value untouched, while an explicit
    `null` clears a nullable field (e.g. `level`).
    """
    name: str | None = None
    birthdate: date | None = None
    level: str | None = None
    instrument: str | None = None
    preferred_language: str | None = None


class StudentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    birthdate: date | None
    level: str | None
    instrument: str | None
    preferred_language: str
    status: str
    created_at: datetime
    updated_at: datetime
