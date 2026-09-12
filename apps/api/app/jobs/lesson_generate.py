"""The add-lesson-from-a-brief job: plan ONE lesson (one LLM call), persist it
`queued`, then chain the ordinary draft — narrowed to that one lesson.

TWO JOB ROWS, exactly as `jobs/module_generate.py` explains: this row answers
"did my lesson get planned?" and goes `succeeded` the moment the lesson exists
with the tutor's brief on it; the chained `curriculum_draft` row owns the
drafting and its own partial-success semantics.

THE CHAIN CARRIES `lesson_ids`, AND THAT IS THE WHOLE DIFFERENCE FROM ADD-MODULE.
The tutor added one lesson; a draft job over the whole root would re-draft every
`queued` lesson in the course — including ones he deliberately left queued. The
filter is `curriculum_draft`'s own (`params["lesson_ids"]`).

AND ITS `job.error` IS NOT COPIED HERE. A `curriculum_draft` that fails
elsewhere in the root reports "N lessons could not be drafted" on ITS row; the
web judges this lesson by its own `draft_status`, so borrowing that string would
turn someone else's failure into a red banner over a lesson that came out fine.
"""
from __future__ import annotations

import logging
import uuid

from app.curriculum.corpus import CurriculumContextError
from app.curriculum.extend import ExtendError, generate_lesson
from app.db import SessionLocal
from app.jobs.curriculum_draft import run_curriculum_draft_job
from app.llm.errors import LLMError, LLMNotConfigured
from app.models.block import Block
from app.models.generation_job import GenerationJob

log = logging.getLogger(__name__)


def run_lesson_generate_job(job_id: uuid.UUID) -> None:
    """Plan + persist the lesson (this job), then chain the single-lesson draft."""
    db = SessionLocal()
    lesson_id: uuid.UUID | None = None
    root_id: uuid.UUID | None = None
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_lesson_generate_job: job %s not found", job_id)
            return

        job.status = "running"
        job.progress = {"phase": "planning"}
        db.commit()

        module_id = uuid.UUID(str(job.params["module_id"]))
        brief = job.params.get("brief") or ""
        title = job.params.get("title") or None
        raw_after = job.params.get("after")
        after = uuid.UUID(str(raw_after)) if raw_after else None

        lesson = generate_lesson(db, module_id, brief=brief, title=title, after=after)
        lesson_id = lesson.id

        # The course, for `result_root_id` — the board the tutor is looking at is
        # the COURSE's, and that is what the job list links back to.
        module = db.get(Block, lesson.parent_id)
        root_id = module.parent_id if module is not None else None

        job.status = "succeeded"
        job.result_root_id = root_id
        job.progress = {"phase": "drafting", "lesson_id": str(lesson_id)}
        db.commit()
    except ExtendError as e:
        _fail(db, job_id, "internal", str(e))
        return
    except CurriculumContextError as e:
        # Too large to read whole AND a selected book is not compiled into the
        # canon yet — an actionable refusal, not "our bug". Nothing was added.
        _fail(db, job_id, "upstream", str(e))
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
                  str(e) or "The lesson could not be planned. Try again.")
        return
    except Exception:
        log.exception("run_lesson_generate_job: job %s failed", job_id)
        _fail(db, job_id, "internal", "The lesson could not be planned. Try again.")
        return
    finally:
        db.close()

    # THE CHAIN. Same thread, immediately after — the planning call above wrote
    # the cache entry and this draft reads it while it is still warm. A failure
    # here does not un-succeed the lesson: it sits `queued` on the board with
    # the ordinary Resume path.
    db = SessionLocal()
    try:
        # NO `grounding` KEY HERE, ON ANY PROVIDER — the `curriculum_draft` job's
        # own router (library / canon / retrieval) decides, exactly as it does for
        # every other draft. Task 4.3 briefly forced `grounding="retrieval"` on
        # `claude_cli` to dodge the bridge's cap; measured live on 2026-09-12 that
        # was worse on both counts. The retrieval-grounded lesson came out 2,348
        # words against 3,798 for the same brief read against the whole library,
        # and it had lost the woods, the profile, the dead spots and the nut — the
        # very things the tutor's brief asked for. And it was not faster: 636s of
        # draft plus a 369s citation-repair re-draft, 17 minutes, against 617s in
        # ONE call for the full-library draft that morning. What the CLI needed was
        # a longer cap and no repair re-draft (both in this task), not thinner
        # material to read.
        draft_job = GenerationJob(
            kind="curriculum_draft", status="pending",
            params={"root_id": str(root_id), "lesson_ids": [str(lesson_id)]},
        )
        db.add(draft_job)
        db.commit()
        draft_job_id = draft_job.id
        job = db.get(GenerationJob, job_id)
        if job is not None:
            # Whole-dict reassignment — a mutated JSONB dict never reaches the DB.
            job.progress = {**(job.progress or {}), "draft_job_id": str(draft_job_id)}
            db.commit()
    except Exception:
        log.exception("run_lesson_generate_job: could not enqueue the draft chain "
                      "for lesson %s — it stays queued for Resume", lesson_id)
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
