"""`/students` routes: CRUD for the Student roster, plus per-student Progress
upsert, LessonLog creation, and the student-detail aggregate (Plan 6 Task 3).

Auth: every route here sits behind the `gt_session` password gate
(`app/auth/middleware.py`), which is a whole-API ASGI middleware rather than a
per-router dependency — so there is nothing to declare in this file. One tutor,
one password; there is still no authorization model, because there is nobody to
authorize against anybody else.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.curriculum.progress import create_lesson_log, upsert_progress
from app.db import get_db
from app.models.block import Block
from app.models.curriculum import Assignment, LessonLog, Progress
from app.models.student import Student
from app.schemas.students import (
    AssignmentSummary,
    LessonLogIn,
    LessonLogOut,
    ProgressIn,
    ProgressOut,
    StudentCreate,
    StudentDetailOut,
    StudentOut,
    StudentUpdate,
)

router = APIRouter(tags=["students"])


def _to_student_out(student: Student) -> StudentOut:
    return StudentOut.model_validate(student, from_attributes=True)


def _to_progress_out(progress: Progress) -> ProgressOut:
    return ProgressOut.model_validate(progress, from_attributes=True)


def _to_lesson_log_out(log: LessonLog) -> LessonLogOut:
    return LessonLogOut.model_validate(log, from_attributes=True)


def _get_student_or_404(db: Session, student_id: UUID) -> Student:
    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="student not found")
    return student


def _get_block_or_404(db: Session, block_id: UUID) -> Block:
    """Same lookup `routers/curriculum.py`'s own `_get_block_or_404` is —
    duplicated rather than imported, same precedent that module's
    `segment_block_endpoint` already documents for a near-identical
    situation ("small deliberate duplication over reaching into another
    router module's `_` helper").
    """
    block = db.get(Block, block_id)
    if block is None:
        raise HTTPException(status_code=404, detail="block not found")
    return block


@router.post("/students", response_model=StudentOut)
def create_student(payload: StudentCreate, db: Session = Depends(get_db)) -> StudentOut:
    student = Student(
        name=payload.name,
        birthdate=payload.birthdate,
        level=payload.level,
        instrument=payload.instrument,
        preferred_language=payload.preferred_language,
    )
    db.add(student)
    db.commit()  # Postgres RETURNING populates id/created_at/updated_at eagerly — no refresh needed
    return _to_student_out(student)


@router.get("/students", response_model=list[StudentOut])
def list_students(db: Session = Depends(get_db)) -> list[StudentOut]:
    students = db.scalars(select(Student).order_by(Student.created_at.desc())).all()
    return [_to_student_out(s) for s in students]


@router.get("/students/{student_id}", response_model=StudentOut)
def get_student(student_id: UUID, db: Session = Depends(get_db)) -> StudentOut:
    return _to_student_out(_get_student_or_404(db, student_id))


@router.patch("/students/{student_id}", response_model=StudentOut)
def update_student(
    student_id: UUID, payload: StudentUpdate, db: Session = Depends(get_db)
) -> StudentOut:
    student = _get_student_or_404(db, student_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(student, field, value)
    db.commit()
    return _to_student_out(student)


@router.delete("/students/{student_id}", status_code=204, response_model=None)
def delete_student(student_id: UUID, db: Session = Depends(get_db)) -> None:
    student = _get_student_or_404(db, student_id)
    db.delete(student)
    db.commit()


@router.post("/students/{student_id}/progress", response_model=ProgressOut)
def upsert_student_progress(
    student_id: UUID, payload: ProgressIn, db: Session = Depends(get_db)
) -> ProgressOut:
    """UPSERT the student's Progress row for `payload.block_id` — see
    `app.curriculum.progress.upsert_progress`'s own docstring for the
    upsert/overwrite semantics. Both existence checks happen here, BEFORE
    calling the service, same reasoning as `routers/curriculum.py`'s
    `segment_block_endpoint` ("404 before doing any... work") — an unchecked
    bad id would otherwise reach the service's `Progress(...)` insert and
    fail as a raw FK IntegrityError/500 instead of a clean 404.
    """
    _get_student_or_404(db, student_id)
    _get_block_or_404(db, payload.block_id)
    progress = upsert_progress(
        db, student_id=student_id, block_id=payload.block_id,
        status=payload.status, notes=payload.notes,
    )
    return _to_progress_out(progress)


@router.post("/students/{student_id}/lessons", response_model=LessonLogOut)
def create_student_lesson_log(
    student_id: UUID, payload: LessonLogIn, db: Session = Depends(get_db)
) -> LessonLogOut:
    """Create a LessonLog for this student. Same pre-check-before-service
    reasoning as `upsert_student_progress` above.
    """
    _get_student_or_404(db, student_id)
    _get_block_or_404(db, payload.session_block_id)
    log = create_lesson_log(
        db, student_id=student_id, session_block_id=payload.session_block_id,
        date=payload.date, taught=payload.taught, notes=payload.notes, homework=payload.homework,
    )
    return _to_lesson_log_out(log)


@router.get("/students/{student_id}/detail", response_model=StudentDetailOut)
def get_student_detail(student_id: UUID, db: Session = Depends(get_db)) -> StudentDetailOut:
    """One aggregate round trip for a student-detail cockpit page: the
    student row, their assigned curriculum roots (+ titles), their per-block
    Progress, and their most recent LessonLogs (newest first, capped at 20).

    `assignments`' `title` is looked up per-row from `Block` — `Assignment.
    curriculum_block_id` FKs to `block.id` with `ondelete="CASCADE"`, so an
    Assignment can never actually outlive the Block it references (deleting
    the block cascades to the Assignment row too); `block` being `None`
    here is therefore unreachable in practice, but guarded anyway rather
    than trusting that invariant blindly across a module boundary — same
    "guarded anyway" precedent `routers/curriculum.py`'s
    `segment_block_endpoint` documents for its own currently-unreachable
    branch.

    "Newest first" for `recent_lessons` orders by `created_at` (every row
    has one; `LessonLog.date` is optional and, if used instead, would sort
    a still-undated log ahead of everything via Postgres's NULLS-FIRST-on-
    DESC default) — the same field every other "list" route in this
    codebase already orders newest-first by (`list_students`,
    `list_curricula`).
    """
    student = _get_student_or_404(db, student_id)

    assignments = db.scalars(
        select(Assignment)
        .where(Assignment.student_id == student_id)
        .order_by(Assignment.created_at.desc())
    ).all()
    assignment_summaries: list[AssignmentSummary] = []
    for a in assignments:
        block = db.get(Block, a.curriculum_block_id)
        assignment_summaries.append(
            AssignmentSummary(
                assignment_id=a.id,
                curriculum_block_id=a.curriculum_block_id,
                title=block.title if block is not None else "(deleted)",
            )
        )

    progress_rows = db.scalars(
        select(Progress).where(Progress.student_id == student_id).order_by(Progress.created_at.desc())
    ).all()

    recent_lessons = db.scalars(
        select(LessonLog)
        .where(LessonLog.student_id == student_id)
        .order_by(LessonLog.created_at.desc())
        .limit(20)
    ).all()

    return StudentDetailOut(
        student=_to_student_out(student),
        assignments=assignment_summaries,
        progress=[_to_progress_out(p) for p in progress_rows],
        recent_lessons=[_to_lesson_log_out(log) for log in recent_lessons],
    )
