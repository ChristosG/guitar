"""The lesson panel's two operations as ONE job kind, `mode` on params —
the `jobs/curriculum_revise.py` plan-vs-apply shape.

PLAN MODE holds NO transaction across the model call: `plan_lesson_change`
itself opens a read transaction while it builds the prompt (sections,
retrieval) and keeps it open through the `guided_json` call — same as
`revise.plan_revision` — but this row is polled by the board, and a job
wrapper is not the place to leave a read transaction hanging around after the
call returns just because nothing was written under it. So the wrapper
`db.rollback()`s the moment `plan_lesson_change` returns (there is nothing to
lose — the function is read-only) and re-`db.get`s the job row before writing
`progress`, rather than trusting the same possibly-expired object.

APPLY MODE needs no such rollback: `apply_lesson_change` manages its own
session discipline end to end — it commits the snapshot-and-claim, `db.close()`s
the connection BEFORE the model call (`jobs/curriculum_draft.py: _draft_one`'s
discipline), and commits again at the end. The job row is simply re-fetched
afterwards, on the same `db`, before writing `progress`.
"""
from __future__ import annotations

import logging
import uuid

from app.curriculum.lesson_ai import LessonAiError, apply_lesson_change, plan_lesson_change
from app.db import SessionLocal
from app.llm.errors import LLMError, LLMNotConfigured
from app.models.block import Block
from app.models.generation_job import GenerationJob

log = logging.getLogger(__name__)


def _root_of(db, lesson_id: uuid.UUID) -> uuid.UUID | None:
    """The course id — module's parent — for `job.result_root_id`."""
    lesson = db.get(Block, lesson_id)
    module = db.get(Block, lesson.parent_id) if lesson and lesson.parent_id else None
    return module.parent_id if module else None


def run_lesson_ai_job(job_id: uuid.UUID) -> None:
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_lesson_ai_job: job %s not found", job_id)
            return
        mode = job.params.get("mode") or "plan"
        lesson_id = uuid.UUID(str(job.params["lesson_id"]))
        job.status = "running"
        job.progress = {"phase": "planning" if mode == "plan" else "generating"}
        job.result_root_id = _root_of(db, lesson_id)
        db.commit()

        if mode == "plan":
            plan = plan_lesson_change(
                db, lesson_id,
                instruction=str(job.params.get("instruction") or ""),
                note=job.params.get("note"),
            )
            # Release the read transaction `plan_lesson_change` held across the
            # model call — read-only, so there is nothing to lose — before
            # re-fetching the row this wrapper writes to.
            db.rollback()
            job = db.get(GenerationJob, job_id)
            job.status = "succeeded"
            job.progress = {"phase": "done", "plan": plan}
            db.commit()
            return

        out = apply_lesson_change(
            db, lesson_id,
            instruction=str(job.params.get("instruction") or ""),
            note=job.params.get("note"),
            sections=list(job.params.get("sections") or []),
        )
        # `apply_lesson_change` already committed and closed its own connection
        # before/after the model call — just re-fetch the row on this session.
        job = db.get(GenerationJob, job_id)
        job.status = "succeeded"
        job.progress = {"phase": "done", **out}
        db.commit()
    except LessonAiError as e:
        _fail(db, job_id, "internal", str(e))
    except LLMNotConfigured:
        _fail(db, job_id, "auth", "No API key is configured. Open Settings, add your key, then try again.")
    except LLMError as e:
        _fail(db, job_id, e.kind, str(e) or e.kind)
    except Exception:
        log.exception("run_lesson_ai_job: job %s failed", job_id)
        _fail(db, job_id, "internal", "The lesson could not be processed. Try again.")
    finally:
        db.close()


def _fail(db, job_id: uuid.UUID, kind: str, message: str) -> None:
    try:
        db.rollback()
        job = db.get(GenerationJob, job_id)
        if job is None:
            return
        job.status = "failed"
        job.error_kind = kind
        job.error = message
        db.commit()
    except Exception:
        log.warning("could not record failure for job %s", job_id, exc_info=True)
