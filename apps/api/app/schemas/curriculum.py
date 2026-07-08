"""Pydantic request/response models for the Curriculum API
(`routers/curriculum.py`): generation, tree retrieval, block CRUD,
segmentation, and per-student assignment.

Kept separate from the SQLAlchemy `app.models.block`/`app.models.curriculum`
models per this codebase's established split (mirrors `schemas/knowledge.py`
vs `app.models.knowledge`) — this module is only the HTTP boundary's shape.
"""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CurriculumGenerateRequest(BaseModel):
    title: str
    language: str
    profile: dict
    domain: str | None = None
    target_minutes_total: int | None = Field(default=None, gt=0)


class CurriculumListItem(BaseModel):
    """`GET /curricula` row shape — template roots only."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    language: str
    target_profile: dict | None = None
    created_at: datetime


class BlockUpdate(BaseModel):
    """All fields optional — PATCH semantics, same `exclude_unset` convention
    as `schemas.students.StudentUpdate`: an omitted field leaves the stored
    value untouched, an explicit `null` clears a nullable one (`body`/
    `est_minutes`). `kind` is intentionally unconstrained (any string) —
    `Block.kind` is documented on the model itself as "soft, relabelable".
    """
    title: str | None = None
    body: str | None = None
    est_minutes: int | None = None
    order: int | None = None
    kind: str | None = None


class SegmentRequest(BaseModel):
    session_minutes: int = Field(gt=0)
    cadence_per_week: int = Field(default=1, gt=0)  # mirrors segment_block's own default
    # Not in the brief's literal field list for this route, but `segment_
    # block` already supports scoping a delivery plan to one student (vs.
    # the template's own student_id=None plan) — exposed as an optional
    # passthrough so the route isn't artificially limited to unscoped calls.
    student_id: UUID | None = None


class AssignRequest(BaseModel):
    student_id: UUID


class BlockTreeOut(BaseModel):
    """Recursive tree shape returned by `routers.curriculum.block_to_tree`.
    `from_attributes=True` so it would also accept an ORM object directly,
    though every current call site passes the plain dict `block_to_tree`
    returns.
    """
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    title: str
    body: str | None = None
    est_minutes: int | None = None
    order: int
    language: str
    plane: str
    student_id: UUID | None = None
    children: list["BlockTreeOut"] = []


BlockTreeOut.model_rebuild()
