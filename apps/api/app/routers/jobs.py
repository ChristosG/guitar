"""`/jobs` routes: poll endpoint for async background-generation jobs
(Plan 8) — `GenerationJob` rows written by an enqueue endpoint (Task 3:
`POST /curricula/generate`) and by `app.jobs.runner.run_curriculum_job`
running off the request path.

Auth: every route here sits behind the `gt_session` password gate
(`app/auth/middleware.py`) — a whole-API ASGI middleware, not a per-router
dependency, so there is nothing to declare in this file. One tutor, one
password; there is still no authorization model, because there is nobody to
authorize against anybody else.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.generation_job import GenerationJob
from app.schemas.jobs import JobOut

router = APIRouter(tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: UUID, db: Session = Depends(get_db)) -> JobOut:
    job = db.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobOut.model_validate(job, from_attributes=True)
