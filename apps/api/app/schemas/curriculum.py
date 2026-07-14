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
    """The non-interview `POST /curricula/generate` body.

    `domain` IS GONE (Plan 13, Stage 6). Chris asked what it did; the honest answer
    was one line in one prompt and a retrieval filter that, after 5d77bd0, could no
    longer exclude anything. `brief` is what it was standing in for — the tutor
    describing, in his own words, what the course is for — and unlike `domain` it
    reaches every lesson-draft prompt, not just the outline.
    """
    title: str
    language: str
    profile: dict = Field(default_factory=dict)
    brief: str | None = None
    # The real shape. `target_minutes_total` stays for the chat agent's tool, which
    # only knows a total — `curriculum.generate.shape_from_request` derives from it.
    weeks: int | None = Field(default=None, gt=0)
    sessions_per_week: int = Field(default=1, gt=0)
    minutes_per_session: int | None = Field(default=None, gt=0)
    target_minutes_total: int | None = Field(default=None, gt=0)
    source_ids: list[UUID] | None = None
    student_id: UUID | None = None
    # "library_only" | "general_knowledge" | "web". `allow_general` is the legacy
    # boolean spelling of the same choice and is translated, not honoured twice.
    gap_policy: str | None = None
    allow_general: bool = True


class CurriculumListItem(BaseModel):
    """`GET /curricula` row shape — template roots only."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    language: str
    target_profile: dict | None = None
    created_at: datetime


class BlockUpdate(BaseModel):
    """All fields optional — PATCH semantics. An omitted field leaves the
    stored value untouched; an explicit `null` is ALSO a no-op (review fix —
    `routers.curriculum.update_block` applies updates via `model_dump(
    exclude_unset=True, exclude_none=True)`), not a clear, even for a
    nullable column (`body`/`est_minutes`): `Block.title` is NOT NULL, so
    treating `null` as "clear this field" uniformly across every field would
    crash on `title` specifically; dropping nulls uniformly instead avoids a
    per-column special case. `title` is further rejected with a 422 if given
    as an empty string (satisfies NOT NULL but is still a useless title).
    `kind` is intentionally unconstrained (any string) — `Block.kind` is
    documented on the model itself as "soft, relabelable".
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


class ArtifactBrief(BaseModel):
    """An artifact, embedded in the tree it is attached to."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    title: str
    spec: dict


class BlockTreeOut(BaseModel):
    """Recursive tree shape returned by `routers.curriculum.block_to_tree`.

    `meta` IS SERIALIZED, AND UNTIL STAGE 6 IT WAS NOT. `block_to_tree` returned
    nine fields and provenance was not one of them — so the citation chips, the
    grounding tiers and the gap badges the spec promised were WRITTEN TO THE
    DATABASE AND THEN THROWN AWAY at the API boundary. The board could not have
    rendered them if it wanted to; there was nothing in the response to render.

    `artifacts` is embedded for the same reason it is embedded rather than
    fetched: every segment leaf used to fire its own `GET /artifacts?block_id=`,
    ~120 of them in parallel on a single board render. That stampede IS the "Could
    not load attached artifacts" error the tutor kept seeing — not a bug in the
    artifacts endpoint, just too many of it at once. Now the whole tree's artifacts
    come back from ONE `WHERE block_id IN (...)`.
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
    meta: dict | None = None
    artifacts: list[ArtifactBrief] = []
    children: list["BlockTreeOut"] = []


BlockTreeOut.model_rebuild()


class ModuleCreate(BaseModel):
    title: str = Field(min_length=1)
    objective: str = ""
    tier: str = "general_knowledge"
    after: UUID | None = None


class LessonCreate(BaseModel):
    title: str = Field(min_length=1)
    objective: str = ""
    after: UUID | None = None


class ReorderRequest(BaseModel):
    direction: str   # "up" | "down"


class RefineRequest(BaseModel):
    """The Extend-with-chat instruction, in the tutor's own words."""
    instruction: str = Field(min_length=1)


class DraftProgressOut(BaseModel):
    """What the board polls every 2 seconds. A GROUP BY over the lesson blocks —
    never a counter on the job row (see `curriculum.draft.draft_progress`)."""
    root_id: UUID
    total: int
    queued: int
    drafting: int
    ready: int
    failed: int
    done: bool
