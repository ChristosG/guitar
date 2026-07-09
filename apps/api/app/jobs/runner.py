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

That last `except Exception` is deliberately broad — it's the job layer's
swallow-and-record boundary (the same role `app.brain.ingest.ingest_source`
plays for its own status lifecycle), not the specific-exception-only style
the synchronous endpoints this mirrors use. A background task has no HTTP
caller to surface an unhandled error to: if anything escapes uncaught here,
the job is silently stranded in "running" forever with nothing left to ever
flip it to "failed". The real exception is logged (`log.exception`) for
diagnosis; only a generic message is stored on the row — and the
failure-recording itself is wrapped in a best-effort guard (see the nested
try/except below), so a DB that has gone unreachable at exactly that moment
can't re-strand the job by making the recovery throw.
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
            # Session on a flush-time error), and any partial writes it did
            # commit-less-ly flush must be discarded rather than swept into
            # this failure-record's commit — so rollback first, then re-fetch
            # `job` (rollback expires everything in the identity map) before
            # recording failure. Mirrors ingest_source's identical recovery.
            try:
                db.rollback()
                job = db.get(GenerationJob, job_id)
                if job is None:
                    # Row vanished between the initial load and here (e.g. a
                    # concurrent delete) — nothing left to record the failure
                    # on; same reasoning as the initial-load guard above.
                    log.warning(
                        "run_curriculum_job: job_id=%s gone during failure recovery", job_id)
                    return
                job.status = "failed"
                job.error_kind = "internal"
                job.error = "Curriculum generation failed unexpectedly. Try again."
                db.commit()
            except Exception:
                # Best-effort recovery, mirroring ingest_source's own final
                # guard: this recovery block itself talks to the DB
                # (rollback/get/commit) — if THAT also fails (e.g. the
                # connection that just errored is now unusable), it must not
                # propagate either, or an uncaught exception here would strand
                # the job in "running" forever, the exact outcome this whole
                # broad catch exists to prevent. Swallow + log: the row may be
                # left at "running" in this rare double-failure case — Task 3's
                # startup sweep is the backstop for that residual strand — but
                # that is strictly better than crashing the background task.
                log.warning(
                    "run_curriculum_job: failed to record failure status for job_id=%s",
                    job_id,
                    exc_info=True,
                )
        else:
            job.status = "succeeded"
            job.result_root_id = root_id
            db.commit()
    finally:
        db.close()
