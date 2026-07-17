"""Compile at ingest: read a finished book into the canon, once, resumably, and
never twice for free (Part B, Task C6).

A background job, SIBLING TO OCR and behind the exact same in-flight guard
(`routers/library.py::_enqueue_ocr` — `SELECT ... FOR UPDATE` on the source row,
then refuse a second job for a source that already has one). Two compiles of one
book racing would both delete-and-rewrite that book's ledger; the guard makes
"press it twice" mean one job, the same way it does for OCR — and the tutor DOES
press twice, because a compile of a real book is a minute of model time he cannot
see the end of.

THE MONEY GUARD IS THE POINT, NOT AN OPTIMISATION. Chris: *"when i send him an
update of the app, the data of the user must be the same"* — an update that
silently re-read ten books would re-spend ~$13 of his own subscription to produce
what it already had. `compile_book` returns without spending on a `status="ready"`
book; this job and its enqueue both honour that (an already-compiled book is not
even enqueued), so wasted spend — the top severity class in this plan — cannot
enter through here.

RUNS WHEN OCR COMPLETES, but only once the book is FULLY read. `autocompile_
after_ocr` gates on there being no page still pending: a run parked on a rate
limit leaves pages unread, and compiling then would read a half book to `ready`
and poison the money guard against the complete one.

RECONCILE (C3) RUNS AFTER A FRESH COMPILE, and this is a deliberate choice.
Reconcile is library-wide, not per-book: a new book's concepts have to be able to
merge against the ones already in the canon, and the compile is exactly the moment
a new book enters. It is cheap to run — `rapidfuzz` blocks first and a canon with
no near-miss names costs nothing to reconcile — so running it after each fresh
compile is safe. It runs in its own guard (`_reconcile_new_book`) so that a
transient reconcile failure cannot un-succeed a book that IS compiled: a missed
merge degrades to a slightly-redundant canon (C4 renders both names — the safe
direction, never a wrong merge) and the next book's compile runs it again.
"""
from __future__ import annotations

import logging
import uuid

from app.brain.ocr import page_counts
from app.canon.compile import compile_book
from app.canon.reconcile import reconcile
from app.db import SessionLocal
from app.llm.errors import LLMError, LLMNotConfigured
from app.models.canon import BookCompile
from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource

log = logging.getLogger(__name__)

# A job is "in flight" in exactly these two statuses — identical to the OCR
# guard's (`brain/ocr.py::_IN_FLIGHT`): `pending` counts because the row is
# committed before `BackgroundTasks` runs the job, so there is a real window in
# which a job about to run still reads `pending`. `jobs/sweep.py` fails every
# pending/running job at boot, so a restart cannot strand this guard.
_IN_FLIGHT = ("pending", "running")


def active_compile_job(db, source_id) -> GenerationJob | None:
    """The in-flight `canon_compile` job for one source, or None. THE IN-FLIGHT
    GUARD — the direct analogue of `brain/ocr.active_ocr_job`. `source_id` lives
    in `GenerationJob.params` (plain JSON), filtered in Python because the
    in-flight set is a handful of rows at most."""
    jobs = (
        db.query(GenerationJob)
        .filter(GenerationJob.kind == "canon_compile",
                GenerationJob.status.in_(_IN_FLIGHT))
        .order_by(GenerationJob.created_at)
        .all()
    )
    sid = str(source_id)
    for job in jobs:
        if str((job.params or {}).get("source_id") or "") == sid:
            return job
    return None


def enqueue_canon_compile(db, source_id, *, force=False) -> tuple[uuid.UUID | None, str]:
    """The GUARDED enqueue — mirrors `routers/library.py::_enqueue_ocr` exactly.

    Returns `(job_id, status)` with `status` one of:
      * `"enqueued"`        — a fresh `canon_compile` job row is committed; the
                              caller schedules `run_canon_compile_job`.
      * `"already_running"` — a compile is already in flight; the existing job id
                              is returned and nothing new is started.
      * `"already_compiled"`— the book is `ready` and `force` was not set, so no
                              job at all — the money guard, one level up from
                              `compile_book`'s own, so a done book does not even
                              cost a job row and a reconcile.

    `SELECT ... FOR UPDATE` on the SOURCE row, not a bare read: FastAPI runs the
    sync handler in a threadpool, so two presses 30ms apart are genuinely
    concurrent and a check-then-insert without a lock is a race. The job row is
    committed here (releasing the lock) so an immediate poll and the guard above
    both see it.
    """
    db.query(KnowledgeSource).filter(KnowledgeSource.id == source_id).with_for_update().one()

    existing = active_compile_job(db, source_id)
    if existing is not None:
        db.commit()                                   # release the row lock
        log.info("canon: compile job %s already in flight for source=%s — not "
                 "enqueuing a second", existing.id, source_id)
        return existing.id, "already_running"

    if not force:
        record = db.get(BookCompile, source_id)
        if record is not None and record.status == "ready":
            db.commit()
            log.info("canon: source=%s is already compiled — not enqueuing (the "
                     "money guard: an update must not re-spend on a book we read)",
                     source_id)
            return None, "already_compiled"

    job = GenerationJob(kind="canon_compile", status="pending",
                        params={"source_id": str(source_id)})
    db.add(job)
    db.commit()
    return job.id, "enqueued"


def autocompile_after_ocr(db, source_id) -> uuid.UUID | None:
    """Enqueue a compile IFF the book is FULLY read and not yet compiled. Returns
    the job id to run, or None. NEVER RAISES — an autocompile hiccup must not fail
    the OCR job that legitimately succeeded.

    "Fully read" is `page_counts(...).pending == 0` (no page pending or
    ocr_running) AND at least one readable page. A parked run with pages still
    pending returns None: the tutor re-presses OCR, and the compile fires when the
    book is genuinely finished — never on a half book.
    """
    try:
        counts = page_counts(db, [source_id]).get(source_id)
        if counts is None or counts.pending > 0 or counts.ready == 0:
            return None
        job_id, status = enqueue_canon_compile(db, source_id)
        return job_id if status == "enqueued" else None
    except Exception:
        log.warning("canon: could not enqueue an auto-compile for source=%s",
                    source_id, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def run_canon_compile_job(job_id: uuid.UUID) -> None:
    """Compile one book into the canon on its OWN session, off the request path.

    Mirrors `runner.run_ocr_job`: the broad `except Exception` is deliberate — a
    background task has no HTTP caller to surface to, so anything uncaught would
    strand the job in "running" forever. `compile_book` commits `status="failed"`
    plus the reason BEFORE re-raising a provider error, so the ledger is honest
    even when this then records the job as failed.
    """
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_canon_compile_job: job %s not found, skipping", job_id)
            return

        job.status = "running"
        db.commit()

        source_id = uuid.UUID(job.params["source_id"])
        try:
            pre = db.get(BookCompile, source_id)
            already_ready = pre is not None and pre.status == "ready"
            record = compile_book(db, source_id)     # money guard inside
            if record.status == "ready" and not already_ready:
                # A book was actually read this run — merge it into the canon.
                _reconcile_new_book(db)
        except LLMNotConfigured:
            db.rollback()
            job = db.get(GenerationJob, job_id)
            if job is None:
                return
            job.status = "failed"
            job.error_kind = "auth"
            job.error = "No Anthropic API key is configured. Open Settings and paste your key."
            db.commit()
        except LLMError as e:
            log.warning("run_canon_compile_job: job %s failed at the provider (%s)",
                        job_id, e.kind)
            db.rollback()
            job = db.get(GenerationJob, job_id)
            if job is None:
                return
            job.status = "failed"
            if e.kind == "rate_limit":
                job.error_kind = "rate_limit"
                job.error = "The model is rate-limited right now. Wait a minute, then try again."
            elif e.kind == "auth":
                job.error_kind = "auth"
                job.error = "Your Anthropic API key was rejected. Open Settings and check your key."
            elif e.kind == "timeout":
                job.error_kind = "timeout"
                job.error = "Compiling the book timed out. Try again."
            else:
                job.error_kind = "upstream"
                job.error = str(e) or "Compiling the book failed at the model provider. Try again."
            db.commit()
        except Exception:
            log.exception("run_canon_compile_job: job %s failed unexpectedly", job_id)
            try:
                db.rollback()
                job = db.get(GenerationJob, job_id)
                if job is None:
                    return
                job.status = "failed"
                job.error_kind = "internal"
                job.error = "Compiling the book failed unexpectedly. Try again."
                db.commit()
            except Exception:
                log.warning("run_canon_compile_job: could not record failure for %s",
                            job_id, exc_info=True)
        else:
            job.status = "succeeded"
            # A book with no readable text compiles to "failed" WITHOUT spending —
            # the job ran fine; surface the reason without a red banner (the same
            # shape `run_ocr_job` uses for "N page(s) unreadable").
            job.error = None if record.status == "ready" else record.error
            db.commit()
    finally:
        db.close()


def _reconcile_new_book(db) -> None:
    """Merge the freshly-compiled book's concepts against the rest of the canon,
    in its OWN guard.

    The compile is the expensive, money-guarded work and it is already committed
    by `compile_book`. Reconcile is a cheap, idempotent follow-up — one model call
    only if `rapidfuzz` finds near-miss names, none at all otherwise. Its failure
    must NOT mark a successfully-compiled book's job as failed, which would hide
    that the book IS compiled and send a Retry straight into the money guard. So a
    reconcile error is logged and swallowed: the canon is left un-merged (C4
    renders both un-merged synonyms — the safe direction, never a wrong merge) and
    the next book's compile runs reconcile again.
    """
    try:
        reconcile(db)
    except Exception:
        log.warning("canon: reconcile after compile failed — the book IS compiled; "
                    "the canon stays un-merged until the next compile", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
