"""Pydantic request/response models for the Students API (`routers/students.py`).

Kept separate from the SQLAlchemy `app.models.student.Student` model per this
codebase's established split (mirrors `schemas/knowledge.py` vs
`app.models.knowledge`) — this module is only the HTTP boundary's shape; it
holds no DB logic.

`goals` IS HERE NOW, and it used to be deliberately absent. This module's earlier
docstring said so out loud: the plan asked for it, `Student` had no column to put
it in, and accepting a field we would silently drop is worse than not offering it
("the client would believe it was saved"). Plan 13, Stage 6 added the column —
because the brief the tutor writes here (`app.students.context.build_student_brief`)
is what reaches the LESSON DRAFT prompt, which the student had never touched.
"""
import datetime as dt
from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class StudentCreate(BaseModel):
    name: str
    birthdate: date | None = None
    level: str | None = None
    instrument: str | None = None
    preferred_language: str = "el"  # mirrors Student.preferred_language's own default
    # What he actually wants ("play Wonderwall at his sister's wedding"), in the
    # tutor's own words — free text, deliberately not an enum.
    goals: str | None = None


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
    goals: str | None = None


class StudentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    birthdate: date | None
    level: str | None
    instrument: str | None
    preferred_language: str
    goals: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime


# --- Progress / LessonLog / student-detail (Plan 6 Task 3) ------------------
#
# Kept here (not in `schemas/curriculum.py`, even though the backing
# `Progress`/`LessonLog` ORM models live in `app.models.curriculum`) because
# every route that uses them is added to `routers/students.py`, not
# `routers/curriculum.py` — this module's own docstring already states its
# organizing principle is "the Students API (`routers/students.py`)"; a
# schema living wherever its OWN router lives is the more load-bearing
# precedent to keep than "schema lives next to its ORM model's module",
# since `routers/curriculum.py` already shows a model/schema split is normal
# in this codebase (`BlockTreeOut` isn't a 1:1 mirror of `Block` either).


class ProgressIn(BaseModel):
    """`POST /students/{id}/progress` request body. Not a PATCH-style partial
    update (no `exclude_unset` dance) — see `app.curriculum.progress.
    upsert_progress`'s own docstring for why `notes` is a wholesale
    overwrite, not a merge.
    """
    block_id: UUID
    status: str   # soft, relabelable — mirrors Progress.status's own comment and BlockUpdate.kind's precedent for not hard-coding an enum
    notes: str | None = None


class ProgressOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    student_id: UUID
    block_id: UUID
    status: str
    notes: str | None
    created_at: datetime
    updated_at: datetime


class LessonLogIn(BaseModel):
    """`POST /students/{id}/lessons` request body — always creates a new
    row (a taught session is its own event; there is no upsert here, unlike
    `ProgressIn`).

    The `date` field is typed `dt.date` (the `import datetime as dt` module
    alias above), NOT the bare `date` imported at the top of this file —
    that bare name works fine for `birthdate` (a differently-named field)
    but self-shadows for a field literally named `date`: Python evaluates a
    class-body annotated assignment's VALUE and stores it under the field's
    own name BEFORE evaluating the annotation expression, so `date: date |
    None = None` binds `date` (the name) to `None` first, then tries to
    evaluate `None | None` for the annotation itself — a real `TypeError`
    at class-definition time (verified empirically while building this
    schema). `dt.date` sidesteps it entirely: the shadowed name is `date`,
    not `dt`.
    """
    session_block_id: UUID
    date: dt.date | None = None
    taught: bool = False
    notes: str | None = None
    homework: str | None = None


class LessonLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    student_id: UUID
    session_block_id: UUID
    date: dt.date | None
    taught: bool
    notes: str | None
    homework: str | None
    created_at: datetime
    updated_at: datetime


class AssignmentSummary(BaseModel):
    """One row of `StudentDetailOut.assignments` — `curriculum_block_id` is
    the TEMPLATE block's id (`Assignment`'s own docstring: "A template
    curriculum Block handed to a specific Student"), and `title` is that
    same template block's title, looked up from `Block` — NOT the student's
    own deep-cloned instance (`routers/curriculum.py`'s `assign_curriculum`
    never records the clone's id anywhere, only the Assignment audit row
    pointing at the template).
    """
    assignment_id: UUID
    curriculum_block_id: UUID
    title: str


class StudentDetailOut(BaseModel):
    """`GET /students/{id}/detail`'s aggregate payload — everything a
    student-detail cockpit page needs in one round trip: the student row,
    their assigned curriculum roots (+ titles), their per-block Progress,
    and their most recent LessonLogs.
    """
    student: StudentOut
    assignments: list[AssignmentSummary]
    progress: list[ProgressOut]
    recent_lessons: list[LessonLogOut]
