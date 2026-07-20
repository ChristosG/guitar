"""The revise job — two modes on ONE `kind="curriculum_revise"` row.

  * no "plan" in params  -> run the PLANNER (`revise.plan_revision`), store the
                            validated plan on `job.progress["plan"]`. Read-only:
                            the tree is never touched, the poller reads the plan.
  * "plan" in params     -> APPLY it (`revise.apply_revision`, one transaction),
                            then CHAIN the ordinary draft fan-out so the new/
                            changed lessons fill in against the warm library cache.

This is the SAME "two job rows" shape as `jobs/module_generate.py`: this row
answers "did my revision plan/apply happen?" and goes `succeeded` the moment the
tree is right; the chained `curriculum_draft` job has its own row and owns the
per-lesson partial-success/Resume story. A failure in the chain does NOT
un-succeed this row — the revised lessons sit `queued` for the ordinary Resume.

Reused by BOTH callers with no new logic:
  * `POST /curricula/{root}/revise` (`routers/curriculum.py`) — `mode` drives
    whether params carry a "plan".
  * the chat `apply_curriculum_revision` tool via `resolve_approval`
    (`routers/chat.py`), which enqueues `params={root_id, plan}` — plan present,
    so this runner applies (Global Constraint #8: reuse `GenerationJob`, no new
    table; `params["plan"]` present is the whole apply-vs-plan switch).
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.curriculum.corpus import CurriculumContextError
from app.curriculum.revise import ReviseError, apply_revision, plan_revision
from app.curriculum.segment_generate import drain_queued_segments
from app.db import SessionLocal
from app.jobs.curriculum_draft import run_curriculum_draft_job
from app.llm.errors import LLMError, LLMNotConfigured
from app.models.block import Block
from app.models.generation_job import GenerationJob

log = logging.getLogger(__name__)


def _has_queued_lessons(db, root_id: uuid.UUID) -> bool:
    """True when at least one LESSON under this course is `draft_status ==
    "queued"` after apply — the conditional-chaining gate. A segment-only or
    blueprint-only revision plan must not spawn a no-op `curriculum_draft` job
    (Spec A: "Blueprint-only/segment-only plans no longer spawn no-op draft
    jobs")."""
    module = aliased(Block)
    rows = db.execute(
        select(Block.meta)
        .join(module, Block.parent_id == module.id)
        .where(module.parent_id == root_id, module.kind == "module", Block.kind == "lesson")
    ).all()
    return any((meta or {}).get("draft_status") == "queued" for (meta,) in rows)


def run_curriculum_revise_job(job_id: uuid.UUID) -> None:
    """Plan (read-only) or apply (one tx + chain the draft fan-out) a revision."""
    db = SessionLocal()
    root_id: uuid.UUID | None = None
    do_chain = False
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_curriculum_revise_job: job %s not found", job_id)
            return

        job.status = "running"
        job.progress = {"phase": "planning"}
        db.commit()

        root_id = uuid.UUID(str(job.params["root_id"]))
        plan = job.params.get("plan")
        if plan is None:
            # PLAN MODE — read-only. Store the validated plan the poller reads.
            result = plan_revision(db, root_id, instruction=job.params["instruction"])
            job.status = "succeeded"
            job.result_root_id = root_id
            job.progress = {"phase": "done", "plan": result}
            db.commit()
        else:
            # APPLY MODE — one transaction (inside apply_revision, already
            # committed by the time we get here), then GENERATE any segments it
            # queued, then chain the draft fan-out.
            out = apply_revision(db, root_id, plan)

            # CONDITIONAL CHAINING (Spec A): only a plan that actually queued a
            # LESSON needs the draft fan-out. A segment-only or blueprint-only
            # plan has nothing for `curriculum_draft` to do, and used to chain
            # one anyway — a job row that immediately finds zero queued lessons
            # and succeeds having done nothing.
            do_chain = _has_queued_lessons(db, root_id)

            # GENERATE the segments apply_revision only created/marked queued —
            # `drain_queued_segments` (segment_generate.py), shared with the
            # resume endpoint's own drain (routers/curriculum.py). FAILURE IS
            # PER-SEGMENT: a bad segment must not fail this row or block the
            # rest, same principle as curriculum_draft's per-lesson isolation.
            # The apply already committed (the tree is right), so this row
            # stays `succeeded` regardless of how many segments fail — same
            # reasoning as the chain below staying non-fatal to this row.
            job.progress = {"phase": "generating", **out,
                            "segments_total": 0, "segments_done": 0, "segments_failed": 0}
            db.commit()

            def _report(total: int, done: int, failed: int) -> None:
                job.progress = {**(job.progress or {}), "segments_total": total,
                                "segments_done": done, "segments_failed": failed}
                db.commit()

            drain_queued_segments(db, root_id, on_progress=_report)

            job.status = "succeeded"
            job.result_root_id = root_id
            job.progress = {**(job.progress or {}), "phase": "drafting"}
            db.commit()
    except ReviseError as e:
        _fail(db, job_id, "internal", str(e))
        return
    except CurriculumContextError as e:
        # Too large to read whole AND a selected book is not compiled into the
        # canon — an actionable refusal, not "our bug"; nothing was changed.
        _fail(db, job_id, "upstream", str(e))
        return
    except LLMNotConfigured:
        _fail(db, job_id, "auth",
              "No API key is configured. Open Settings, add your key, then try again.")
        return
    except LLMError as e:
        if e.kind == "rate_limit":
            _fail(db, job_id, "rate_limit",
                  "The model is rate-limited right now. Wait a minute and try again.")
        else:
            _fail(db, job_id, "upstream",
                  str(e) or "The revision could not be planned. Try again.")
        return
    except Exception:
        log.exception("run_curriculum_revise_job: job %s failed", job_id)
        _fail(db, job_id, "internal", "The revision could not be completed. Try again.")
        return
    finally:
        db.close()

    # THE CHAIN. Same thread, immediately after apply — exactly like
    # module_generate. A revise CHANGES lessons, so the chained draft is
    # RETRIEVAL-GROUNDED (`grounding="retrieval"`): each new/changed lesson is
    # drafted from per-lesson `ground_topic` plus the model's own knowledge, never
    # the whole-library gate that refused here, and never a `CurriculumContextError`.
    #
    # The apply already committed (the tree is right), so this row stays `succeeded`
    # regardless of the chain — but we record the chained draft's id on its progress
    # so the revise chat can poll it and SURFACE a drafting failure, instead of
    # leaving the new lessons silently "queued".
    if do_chain:
        draft_id: uuid.UUID | None = None
        db = SessionLocal()
        try:
            draft = GenerationJob(
                kind="curriculum_draft", status="pending",
                params={"root_id": str(root_id), "grounding": "retrieval"},
            )
            db.add(draft)
            db.commit()
            draft_id = draft.id
            revise_job = db.get(GenerationJob, job_id)
            if revise_job is not None:
                # Whole-dict reassignment — `progress` is plain sa.JSON, no MutableDict.
                revise_job.progress = {**(revise_job.progress or {}),
                                       "draft_job_id": str(draft_id)}
                db.commit()
        except Exception:
            log.exception("run_curriculum_revise_job: could not enqueue the draft "
                          "chain for %s — the revised lessons stay queued for Resume", root_id)
            return
        finally:
            db.close()

        run_curriculum_draft_job(draft_id)


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
