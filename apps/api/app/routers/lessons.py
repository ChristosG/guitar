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
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.jobs.runner import run_lesson_job
from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource
from app.schemas.jobs import JobAccepted
from app.schemas.lessons import SelectionIn

router = APIRouter(prefix="/lessons", tags=["lessons"])


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
