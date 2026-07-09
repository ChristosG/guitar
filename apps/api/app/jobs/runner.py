"""Background runner for async curriculum generation (Plan 8 Task 2).

`generate_curriculum` (`app.curriculum.generate`) is a blocking guided-JSON
LLM call measured at 49-179s/call (`routers/curriculum.py`'s
`generate_curriculum_endpoint` docstring) — too slow for a synchronous
request/response cycle. `run_curriculum_job` is what a scheduler (Task 3:
`POST /curricula/generate` enqueuing a `GenerationJob` row and scheduling
this via `BackgroundTasks`) actually calls to do that work off the request
path, polled back via `GET /jobs/{id}` (`routers/jobs.py`).

Own session, deliberately: this function opens its OWN `SessionLocal()`
rather than accepting a caller-owned `db`, because it runs detached from the
request that scheduled it — by the time this executes, the request's own
`get_db()`-provided session may already be closed. `expire_on_commit=False`
(`app.db`) means the `job` object stays readable across this function's own
multiple commits without needing repeated re-fetches.

Error classification mirrors `generate_curriculum_endpoint`'s exact mapping
(same exception types, same user-facing copy) so a polled `GenerationJob.
error`/`error_kind` reads the same as the synchronous endpoint's old 502/504
detail would have:
    `GuidedJSONError`                                  -> "upstream"
    `(openai.APIConnectionError, httpx.TransportError)` -> "timeout"
    anything else                                       -> "internal"

That last `except Exception` is deliberately broad — the ONE place in this
codebase that catches wholesale (every other handler here, and in the
endpoints this mirrors, catches specific exception types only, per this
codebase's no-auth-PoC posture of not blanket-catching `Exception`). A
background task has no HTTP caller to surface an unhandled error to: if
anything escapes uncaught here, the job is silently stranded in "running"
forever with nothing left to ever flip it to "failed". The real exception is
logged (`log.exception`) for diagnosis; only a generic message is stored on
the row, mirroring `app.brain.ingest.ingest_source`'s swallow-and-record
pattern for the same status-lifecycle-must-never-hang reason.
"""
import logging
import uuid

import httpx
import openai

from app.curriculum.generate import generate_curriculum
from app.db import SessionLocal
from app.llm.errors import GuidedJSONError
from app.models.generation_job import GenerationJob

log = logging.getLogger(__name__)


def run_curriculum_job(job_id: uuid.UUID) -> None:
    """Load `GenerationJob(job_id)`, run curriculum generation on it, record
    the outcome — all on a session this function opens and closes itself.

    Missing job: logged and returned (not raised) — defensive only. The
    enqueue endpoint (Task 3) always creates the row before scheduling this
    runner, so this should never happen in practice; there's no job row to
    record an error against either way.
    """
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_curriculum_job: job_id=%s not found, skipping", job_id)
            return

        job.status = "running"
        db.commit()

        try:
            root_id = generate_curriculum(db, **job.params)
        except GuidedJSONError:
            job.status = "failed"
            job.error_kind = "upstream"
            job.error = (
                "Curriculum generation failed (model returned invalid/truncated output). Try again."
            )
            db.commit()
        except (openai.APIConnectionError, httpx.TransportError):
            job.status = "failed"
            job.error_kind = "timeout"
            job.error = "Curriculum generation timed out. Try again."
            db.commit()
        except Exception:
            log.exception("run_curriculum_job: job_id=%s failed unexpectedly", job_id)
            # A failure inside generate_curriculum's own DB writes (e.g. mid
            # _persist_tree) can leave this Session's transaction needing a
            # rollback before it's usable again (SQLAlchemy invalidates the
            # Session on a flush-time error) — rollback, then re-fetch `job`
            # (rollback expires everything in the identity map) before
            # recording failure. Mirrors ingest_source's identical recovery
            # step, same reason.
            db.rollback()
            job = db.get(GenerationJob, job_id)
            job.status = "failed"
            job.error_kind = "internal"
            job.error = "Curriculum generation failed unexpectedly. Try again."
            db.commit()
        else:
            job.status = "succeeded"
            job.result_root_id = root_id
            db.commit()
    finally:
        db.close()
