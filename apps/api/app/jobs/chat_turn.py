"""One chat turn, as a `GenerationJob`.

The revise drawer's turn calls `propose_curriculum_revision` INLINE (a
`role="plan"` guided-JSON call over the course, 20s-6min depending on the
provider). On the webapp that request sits behind nginx and Cloudflare, which
cut it long before the model finishes — the answer was persisted, billed, and
never seen (2026-07-21, 2026-09-11). Here the SAME turn (`routers/chat.py::
run_turn_core`) runs off the request path; the browser polls the job and then
re-hydrates history + pending approval exactly as a page reload would.

The turn's outcome is stored on `progress["turn"]` (the `ChatTurnOut` dict) so a
poller can branch on it without a second history read.
"""
from __future__ import annotations

import logging
import uuid

from app.db import SessionLocal
from app.llm.errors import LLMError, LLMNotConfigured
from app.models.chat import ChatSession
from app.models.generation_job import GenerationJob

log = logging.getLogger(__name__)


def run_chat_turn_job(job_id: uuid.UUID) -> None:
    # Imported here, not at module level: `routers/chat.py` imports THIS module
    # at import time (for `background_tasks.add_task`), so a top-level import
    # back into the router would be circular.
    from app.routers.chat import run_turn_core

    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_chat_turn_job: job %s not found", job_id)
            return
        job.status = "running"
        job.progress = {"phase": "answering"}
        db.commit()

        session = db.get(ChatSession, uuid.UUID(str(job.params["session_id"])))
        if session is None:
            _fail(db, job_id, "internal", "That conversation no longer exists.")
            return
        turn = run_turn_core(db, session, str(job.params.get("content") or ""))
        job = db.get(GenerationJob, job_id)
        job.status = "succeeded"
        job.progress = {"phase": "done", "turn": turn.model_dump(mode="json")}
        db.commit()
    except LLMNotConfigured:
        _fail(db, job_id, "auth", "No API key is configured. Open Settings, add your key, then try again.")
    except LLMError as e:
        _fail(db, job_id, e.kind, str(e) or e.kind)
    except Exception:
        log.exception("run_chat_turn_job: job %s failed", job_id)
        _fail(db, job_id, "internal", "The answer could not be written. Try again.")
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
