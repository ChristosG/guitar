"""`/jobs` routes: poll endpoint for async background-generation jobs
(Plan 8) — `GenerationJob` rows written by an enqueue endpoint (Task 3:
`POST /curricula/generate`) and by `app.jobs.runner.run_curriculum_job`
running off the request path.

No-auth PoC posture, same as `routers/artifacts.py`/`routers/curriculum.py`
— no authentication/authorization here either; this deploys origin-locked
behind Cloudflare for a single user.
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
