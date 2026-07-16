"""The add-module job: plan one module (one LLM call), persist it queued, then
chain the ORDINARY draft fan-out over the curriculum so the new lessons get
written immediately — against the cache entry the planning call just wrote.

TWO JOB ROWS, ON PURPOSE. This job's row answers the tutor's actual question —
"did my module get planned?" — and goes `succeeded` the moment the module and its
queued lessons exist. The chained `curriculum_draft` job has its own row and its
own partial-success semantics (`jobs/curriculum_draft.py`): a lesson that fails
to draft is a row with a Retry button on the board, not a failed add-module. The
recovery story is the SAME one the rest of the app already has — sweep flips
interrupted lessons back to `queued` at boot, and Resume finishes them.
"""
from __future__ import annotations

import logging
import uuid

from app.curriculum.extend import ExtendError, generate_module
from app.db import SessionLocal
from app.jobs.curriculum_draft import run_curriculum_draft_job
from app.llm.errors import LLMError, LLMNotConfigured
from app.models.generation_job import GenerationJob

log = logging.getLogger(__name__)


def run_module_generate_job(job_id: uuid.UUID) -> None:
    """Plan + persist the module (this job), then chain the draft fan-out."""
    db = SessionLocal()
    module_id: uuid.UUID | None = None
    root_id: uuid.UUID | None = None
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_module_generate_job: job %s not found", job_id)
            return

        job.status = "running"
        job.progress = {"phase": "planning"}
        db.commit()

        root_id = uuid.UUID(str(job.params["root_id"]))
        topic = job.params.get("topic") or None

        module = generate_module(db, root_id, topic=topic)
        module_id = module.id

        job.status = "succeeded"
        job.result_root_id = root_id
        job.progress = {"phase": "drafting", "module_id": str(module_id)}
        db.commit()
    except ExtendError as e:
        _fail(db, job_id, "internal", str(e))
        return
    except LLMNotConfigured:
        _fail(db, job_id, "auth",
              "No API key is configured. Open Settings, add your key, then try again.")
        return
    except LLMError as e:
        if e.kind == "rate_limit":
            _fail(db, job_id, "rate_limit",
                  "The model is rate-limited right now. Wait a minute and try again — "
                  "nothing was added to the course.")
        else:
            _fail(db, job_id, "upstream",
                  str(e) or "The module could not be planned. Try again.")
        return
    except Exception:
        log.exception("run_module_generate_job: job %s failed", job_id)
        _fail(db, job_id, "internal", "The module could not be planned. Try again.")
        return
    finally:
        db.close()

    # THE CHAIN. Same thread, immediately after — the planning call above wrote
    # the 90K-token cache entry, and these drafts read it at 0.1x while it is
    # still warm (5-minute TTL). A failure here does not un-succeed the module
    # job: the lessons sit `queued` on the board with the ordinary Resume path.
    db = SessionLocal()
    try:
        draft_job = GenerationJob(
            kind="curriculum_draft", status="pending",
            params={"root_id": str(root_id)},
        )
        db.add(draft_job)
        db.commit()
        draft_job_id = draft_job.id
    except Exception:
        log.exception("run_module_generate_job: could not enqueue the draft chain "
                      "for module %s — its lessons stay queued for Resume", module_id)
        return
    finally:
        db.close()

    run_curriculum_draft_job(draft_job_id)


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
