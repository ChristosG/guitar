"""Progress + LessonLog services: framework-free `(db, ...) -> Model`
mutations, extracted the same way `app.curriculum.assign.
clone_content_subtree` is — so the identical logic is shared by BOTH
`routers/students.py`'s `POST /students/{id}/progress` + `POST
/students/{id}/lessons` HTTP routes AND a future chat-agent `log_progress`
tool (this plan's later task), instead of drifting between two copies.

Pure `(db, ...) -> Model` logic with zero FastAPI/HTTP coupling (no
`HTTPException`, no `Depends`) — student/block existence is the CALLER's
responsibility, same convention `clone_content_subtree` documents for
itself: the router already 404s on an unknown student/block BEFORE calling
in here (required, not just nice-to-have — `Progress.block_id`/`LessonLog.
session_block_id` are `ForeignKey(..., ondelete="CASCADE")` columns, so an
unchecked bad id would surface as a raw IntegrityError/500 at commit time
instead of a clean 404), so these functions assume valid foreign keys.

Unlike `clone_content_subtree` (flush-only, caller commits), both functions
below DO commit internally — each is one single, self-contained row write
with nothing else to batch into the same transaction at either of this
task's two call sites, mirroring `segment_block`'s own "commits inside the
service" precedent for that same reason.
"""
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.curriculum import LessonLog, Progress


def upsert_progress(
    db: Session, *, student_id: UUID, block_id: UUID, status: str, notes: str | None = None,
) -> Progress:
    """Insert-or-update the single Progress row for (student_id, block_id).

    A student has at most one Progress row per block — that's the whole
    point of "upsert" here (mirrors the brief's own worked example: "create
    then update same block = one row, new status"). Looked up with a plain
    SELECT + Python branch rather than a Postgres `ON CONFLICT` upsert:
    there's no unique constraint on (student_id, block_id) to conflict
    against (none was added for this task — this PoC has a single tutor
    writing interactively, not a high-concurrency path where a race between
    the SELECT and the INSERT could double-insert; see this task's report),
    and a plain select-then-branch is simple, correct, and adequate here.

    `status`/`notes` are OVERWRITTEN wholesale with whatever this call is
    given, not merged in PATCH-fashion — every call to this fn fully states
    the row's new status/notes, so an omitted `notes` (defaults to `None`)
    clears any previous note rather than leaving it untouched. This mirrors
    the route's own POST semantics (a fresh "log the current state" call,
    not a partial update) and keeps one simple rule instead of threading
    `exclude_unset` plumbing from the HTTP payload down into this
    framework-free fn.
    """
    existing = db.scalars(
        select(Progress).where(Progress.student_id == student_id, Progress.block_id == block_id)
    ).first()
    if existing is not None:
        existing.status = status
        existing.notes = notes
        db.commit()
        return existing

    progress = Progress(student_id=student_id, block_id=block_id, status=status, notes=notes)
    db.add(progress)
    db.commit()
    return progress


def create_lesson_log(
    db: Session, *, student_id: UUID, session_block_id: UUID, date: date | None = None,
    taught: bool = False, notes: str | None = None, homework: str | None = None,
) -> LessonLog:
    """Create one LessonLog row. Always an insert — a taught session is its
    own event, never merged into a prior one (unlike `upsert_progress`).
    """
    log = LessonLog(
        student_id=student_id, session_block_id=session_block_id, date=date,
        taught=taught, notes=notes, homework=homework,
    )
    db.add(log)
    db.commit()
    return log
