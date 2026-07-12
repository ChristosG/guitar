"""The seam from the Library to lesson authoring (Plan 10 Task 1).

Captures a selection the tutor made while reading — with the page provenance
attached — and enqueues a `GenerationJob(kind="lesson")` to draft a real
`Block(kind="lesson")` tree grounded in that exact passage (`app.lessons.
draft.draft_lesson_from_selection`, run off the request path by
`app.jobs.runner.run_lesson_job`). Mirrors `routers/curriculum.py`'s
`generate_curriculum_endpoint` verbatim (B4): `draft_lesson_from_selection`
is a blocking guided-JSON LLM call, too slow for a synchronous
request/response cycle, so this creates the job row, commits it (BEFORE
scheduling the background task — the runner opens its OWN session and must
find the row), and returns 202 immediately. Poll `GET /jobs/{job_id}`
(`routers/jobs.py`) for the outcome; `result_root_id` is the drafted lesson's
root Block id.

`run_lesson_job` is imported at module level specifically so tests can
`monkeypatch.setattr("app.routers.lessons.run_lesson_job", ...)`: Starlette's
TestClient runs `BackgroundTasks` in-process, AFTER the response — an
unpatched test would trigger a real, multi-minute LLM call.
"""
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.jobs.runner import run_lesson_job
from app.lessons.edit import add_session, merge_sessions, split_session
from app.models.block import Block
from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource
from app.routers.curriculum import block_to_tree
from app.schemas.curriculum import BlockTreeOut
from app.schemas.jobs import JobAccepted
from app.schemas.lessons import (
    AddSessionRequest,
    LessonListItem,
    MergeSessionsRequest,
    SelectionIn,
    SplitSessionRequest,
)

router = APIRouter(prefix="/lessons", tags=["lessons"])


def _get_block_or_404(db: Session, block_id: UUID) -> Block:
    # Same lookup routers/curriculum.py's own `_get_block_or_404` (and
    # routers/artifacts.py's copy of it) perform — not imported from there,
    # small deliberate duplication over a cross-router import, mirroring
    # that module's own precedent.
    block = db.get(Block, block_id)
    if block is None:
        raise HTTPException(status_code=404, detail="block not found")
    return block


def _get_lesson_or_404(db: Session, lesson_id: UUID) -> Block:
    lesson = _get_block_or_404(db, lesson_id)
    if lesson.kind != "lesson":
        raise HTTPException(status_code=404, detail="lesson not found")
    return lesson


def _get_session_of_lesson_or_404(db: Session, lesson_id: UUID, session_id: UUID) -> Block:
    """A session Block that both exists AND is actually a child of
    `lesson_id` — scopes the nested `/lessons/{lesson_id}/sessions/{session_id}`
    URL so a session id from a DIFFERENT lesson 404s here rather than being
    silently accepted and split/edited under the wrong lesson.
    """
    session = _get_block_or_404(db, session_id)
    if session.kind != "session" or session.parent_id != lesson_id:
        raise HTTPException(status_code=404, detail="session not found in this lesson")
    return session


@router.post("/from-selection", response_model=JobAccepted, status_code=202)
def from_selection(
    payload: SelectionIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> JobAccepted:
    source = db.get(KnowledgeSource, payload.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    params = {
        "source_id": str(payload.source_id),
        "page_no": payload.page_no,
        "text": payload.text,
    }
    job = GenerationJob(kind="lesson", status="pending", params=params)
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(run_lesson_job, job.id)

    return JobAccepted(job_id=job.id, status=job.status)


@router.get("", response_model=list[LessonListItem])
def list_lessons(db: Session = Depends(get_db)) -> list[LessonListItem]:
    """Every drafted lesson (`Block(kind="lesson", plane="content")`),
    newest first, with its provenance (if any — B3, set by `draft_lesson_
    from_selection`) lifted out of `target_profile` so the UI can render
    "from <book>, p.21" without reaching into the JSON column itself.
    """
    lessons = db.scalars(
        select(Block)
        .where(Block.kind == "lesson", Block.plane == "content")
        .order_by(Block.created_at.desc())
    ).all()
    return [
        LessonListItem(
            id=lesson.id,
            title=lesson.title,
            created_at=lesson.created_at,
            provenance=(lesson.target_profile or {}).get("provenance"),
        )
        for lesson in lessons
    ]


@router.get("/{lesson_id}", response_model=BlockTreeOut)
def get_lesson(lesson_id: UUID, db: Session = Depends(get_db)) -> dict:
    # Reuses routers.curriculum's block_to_tree/BlockTreeOut verbatim (per
    # this task's brief) rather than a second tree serializer.
    return block_to_tree(_get_lesson_or_404(db, lesson_id))


@router.post("/{lesson_id}/sessions/{session_id}/split", response_model=BlockTreeOut)
def split_session_endpoint(
    lesson_id: UUID, session_id: UUID, payload: SplitSessionRequest, db: Session = Depends(get_db),
) -> dict:
    lesson = _get_lesson_or_404(db, lesson_id)
    _get_session_of_lesson_or_404(db, lesson_id, session_id)

    try:
        split_session(db, session_id, session_minutes=payload.session_minutes)
    except ValueError as e:
        # Mirrors routers.artifacts/notes's ValueError -> 422 precedent
        # (app.lessons.edit's own errors are all caller/input problems -
        # unknown/wrong-kind block, nothing to split - not server faults).
        raise HTTPException(status_code=422, detail=str(e)) from e

    # `lesson.children` has not been accessed yet on this request-scoped
    # session, so this is the FIRST load of that relationship - it lazily
    # queries fresh from the DB (post-commit), never returning a stale
    # pre-split collection despite `expire_on_commit=False` (app/db.py).
    return block_to_tree(lesson)


@router.post("/{lesson_id}/sessions/merge", response_model=BlockTreeOut)
def merge_sessions_endpoint(
    lesson_id: UUID, payload: MergeSessionsRequest, db: Session = Depends(get_db),
) -> dict:
    lesson = _get_lesson_or_404(db, lesson_id)
    for session_id in payload.session_ids:
        _get_session_of_lesson_or_404(db, lesson_id, session_id)

    try:
        merge_sessions(db, payload.session_ids)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    return block_to_tree(lesson)


@router.post("/{lesson_id}/sessions", response_model=BlockTreeOut)
def add_session_endpoint(
    lesson_id: UUID, payload: AddSessionRequest, db: Session = Depends(get_db),
) -> dict:
    lesson = _get_lesson_or_404(db, lesson_id)

    # Pre-check 'after' parameter membership at the route boundary, consistent
    # with split/merge routes, to return 404 on cross-lesson ids rather than 422.
    if payload.after is not None:
        _get_session_of_lesson_or_404(db, lesson_id, payload.after)

    try:
        add_session(
            db, lesson_id, title=payload.title, est_minutes=payload.est_minutes, after=payload.after,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    return block_to_tree(lesson)
