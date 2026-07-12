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

from app.brain.ingest import IngestPayload, ingest_source
from app.brain.ocr import ocr_source
from app.curriculum.generate import generate_curriculum
from app.db import SessionLocal
from app.lessons.draft import draft_lesson_from_selection
from app.llm.errors import GuidedJSONError
from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource

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


def run_lesson_job(job_id: uuid.UUID) -> None:
    """Load `GenerationJob(job_id)`, draft a lesson from its `params`
    selection, record the outcome — all on a session this function opens and
    closes itself. Mirrors `run_curriculum_job` EXACTLY (same session
    ownership, same `status` lifecycle, same `error_kind` classification,
    same best-effort nested failure-recording guard): `draft_lesson_from_
    selection` is, like `generate_curriculum`, a blocking guided-JSON LLM
    call unsuitable for a synchronous request/response cycle (Plan 10 Task 1,
    B4 — "reuse the EXISTING pattern verbatim; add no new infrastructure").

    Missing job: logged and returned (not raised) — defensive only, same as
    `run_curriculum_job`. The enqueue endpoint (`routers/lessons.py`) always
    creates the row before scheduling this runner.
    """
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_lesson_job: job_id=%s not found, skipping", job_id)
            return

        job.status = "running"
        db.commit()

        try:
            # `job.params` came back off the JSON column as plain str/int/
            # dict — `source_id` needs an explicit uuid.UUID() conversion
            # before it can be used as a KnowledgeSource primary key lookup
            # (mirrors run_reingest_job's identical `uuid.UUID(job.params[
            # "source_id"])` conversion just below in this same module).
            lesson_id = draft_lesson_from_selection(
                db,
                source_id=uuid.UUID(job.params["source_id"]),
                page_no=job.params["page_no"],
                text=job.params["text"],
                language=job.params.get("language", "en"),
            )
        except GuidedJSONError:
            job.status = "failed"
            job.error_kind = "upstream"
            job.error = (
                "Lesson drafting failed (model returned invalid/truncated output). Try again."
            )
            db.commit()
        except (openai.APIConnectionError, httpx.TransportError):
            job.status = "failed"
            job.error_kind = "timeout"
            job.error = "Lesson drafting timed out. Try again."
            db.commit()
        except Exception:
            log.exception("run_lesson_job: job_id=%s failed unexpectedly", job_id)
            # Same recovery reasoning as run_curriculum_job's identical block:
            # rollback first (a flush-time error invalidates this Session's
            # transaction), then re-fetch `job` (rollback expires the
            # identity map) before recording failure.
            try:
                db.rollback()
                job = db.get(GenerationJob, job_id)
                if job is None:
                    log.warning(
                        "run_lesson_job: job_id=%s gone during failure recovery", job_id)
                    return
                job.status = "failed"
                job.error_kind = "internal"
                job.error = "Lesson drafting failed unexpectedly. Try again."
                db.commit()
            except Exception:
                # Best-effort recovery — see run_curriculum_job's identical
                # guard for the full reasoning (must not propagate, or an
                # uncaught exception here strands the job in "running"
                # forever).
                log.warning(
                    "run_lesson_job: failed to record failure status for job_id=%s",
                    job_id,
                    exc_info=True,
                )
        else:
            job.status = "succeeded"
            job.result_root_id = lesson_id
            db.commit()
    finally:
        db.close()


def run_ocr_job(job_id: uuid.UUID) -> None:
    """Run OCR for one source on its OWN session, off the request path
    (Plan 9 Task 6 — `POST /knowledge/sources/{id}/ocr` and the pdf branch of
    `POST /knowledge/sources/{id}/retry`, both in `routers/library.py`).

    Mirrors `run_curriculum_job`: broad `except Exception` is deliberate — a
    background task has no HTTP caller to surface to, so anything uncaught
    would strand the job in "running" forever. `ocr_source` already commits
    each page independently, so a mid-run failure keeps every page OCR'd so
    far (see `app/brain/ocr.py`'s own docstring for the per-page-commit
    contract).
    """
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_ocr_job: job_id=%s not found, skipping", job_id)
            return

        job.status = "running"
        db.commit()
        try:
            result = ocr_source(db, uuid.UUID(job.params["source_id"]))
        except Exception:
            log.exception("run_ocr_job: job_id=%s failed", job_id)
            try:
                db.rollback()
                job = db.get(GenerationJob, job_id)
                if job is None:
                    return
                job.status = "failed"
                job.error_kind = "internal"
                job.error = "OCR failed unexpectedly. Try again."
                db.commit()
            except Exception:
                log.warning(
                    "run_ocr_job: could not record failure for %s", job_id, exc_info=True)
        else:
            job.status = "succeeded"
            job.error = None if result.failed == 0 else f"{result.failed} page(s) unreadable"
            db.commit()
    finally:
        db.close()


def run_reingest_job(job_id: uuid.UUID) -> None:
    """Re-run the full ingest pipeline for a "url"-kind source, off the
    request path (Plan 9 Task 6's controller decision for the url branch of
    `POST /knowledge/sources/{id}/retry`).

    Re-fetches the `KnowledgeSource` row by id and reads its OWN stored
    `.url` column rather than trusting anything the retry caller supplied —
    that column exists precisely so a retry needs no fresh input (see
    `KnowledgeSource.url`'s docstring, Plan 9 Task 1).

    `ingest_source` already has its own swallow-and-record status lifecycle
    (it never raises, except for an unknown `source_id` — a
    caller/programming error, not an ingestion failure); after it returns,
    this just mirrors the source's resulting status onto the job row so a
    poller sees the same failed/succeeded distinction without a second
    request to `GET /knowledge/sources/{id}`.
    """
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_reingest_job: job_id=%s not found, skipping", job_id)
            return

        job.status = "running"
        db.commit()
        try:
            source_id = uuid.UUID(job.params["source_id"])
            source = db.get(KnowledgeSource, source_id)
            if source is None:
                raise ValueError(f"KnowledgeSource {source_id!r} not found")
            ingest_source(db, source_id, IngestPayload(kind="url", url=source.url))
        except Exception:
            log.exception("run_reingest_job: job_id=%s failed", job_id)
            try:
                db.rollback()
                job = db.get(GenerationJob, job_id)
                if job is None:
                    return
                job.status = "failed"
                job.error_kind = "internal"
                job.error = "Reingest failed unexpectedly. Try again."
                db.commit()
            except Exception:
                log.warning(
                    "run_reingest_job: could not record failure for %s", job_id, exc_info=True)
        else:
            source = db.get(KnowledgeSource, source_id)
            if source is not None and source.status == "failed":
                job.status = "failed"
                job.error_kind = "internal"
                job.error = source.error
            else:
                job.status = "succeeded"
                job.error = None
            db.commit()
    finally:
        db.close()
