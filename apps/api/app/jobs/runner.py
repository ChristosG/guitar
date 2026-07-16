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
from app.curriculum.interview import generate_interview_outline
from app.db import SessionLocal
from app.jobs.curriculum_draft import run_curriculum_draft_job
from app.i18n import DEFAULT_LOCALE
from app.lessons.draft import draft_lesson_from_selection
from app.llm.errors import GuidedJSONError, LLMError, LLMNotConfigured
from app.models.generation_job import GenerationJob
from app.models.interview import CurriculumInterview
from app.models.knowledge import KnowledgeSource

log = logging.getLogger(__name__)

# What `generate_curriculum` actually accepts. A `GenerationJob.params` blob is a
# WIRE FORMAT WITH NO VERSION: a row can be created moments before a deploy and
# executed moments after it, and `domain` — which Stage 6 deleted — was in every
# curriculum job this app has ever written. `generate_curriculum(db, **params)`
# would `TypeError` on it, get caught by the broad `except Exception`, and be
# recorded as `internal` ("our bug") on a job whose only sin was being enqueued
# five seconds early. Filtering is one line and it makes a deploy survivable.
_CURRICULUM_PARAMS = frozenset({
    "title", "language", "profile", "brief", "gap_policy", "allow_general",
    "weeks", "sessions_per_week", "minutes_per_session", "target_minutes_total",
    "source_ids", "student_id",
})


def _record_llm_failure(db, job_id: uuid.UUID, e: LLMError, what: str) -> None:
    """Record a provider failure UNDER ITS OWN KIND — the taxonomy the frontend
    translates and acts on. This is the branch the Claude providers were built
    to feed (`llm/claude.py::_mapped_errors` turns every Anthropic failure into
    an `LLMError` with kind auth/rate_limit/timeout/upstream) and that these
    runners never caught: a rejected key or a 429 fell through to the broad
    `except Exception` and was filed as error_kind="internal" — "our bug, try
    again" — sending the tutor to retry-spam the exact call that was
    rate-limited, instead of to Settings. Same rollback-before-recording rule
    as every sibling branch: partial flushed writes must not be swept into the
    failure-record's commit.
    """
    db.rollback()
    job = db.get(GenerationJob, job_id)
    if job is None:
        log.warning("%s: job_id=%s gone during failure recovery", what, job_id)
        return
    job.status = "failed"
    if e.kind == "auth":
        job.error_kind = "auth"
        job.error = ("Your Anthropic API key was rejected. Open Settings and "
                     "check your key.")
    elif e.kind == "rate_limit":
        job.error_kind = "rate_limit"
        job.error = "The model is rate-limited right now. Wait a minute, then try again."
    elif e.kind == "timeout":
        job.error_kind = "timeout"
        job.error = f"{what} timed out. Try again."
    else:
        job.error_kind = "upstream"
        job.error = str(e) or f"{what} failed at the model provider. Try again."
    db.commit()


def _curriculum_kwargs(params: dict) -> dict:
    dropped = set(params) - _CURRICULUM_PARAMS
    if dropped:
        log.info("run_curriculum_job: ignoring params from an older deploy: %s",
                 ", ".join(sorted(dropped)))
    return {k: v for k, v in params.items() if k in _CURRICULUM_PARAMS}


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
            root_id = generate_curriculum(db, **_curriculum_kwargs(job.params))
        except LLMNotConfigured:
            # The key was there when this job was enqueued (`require_llm_configured`
            # gates every producer) and is gone now — the tutor cleared it, or the
            # ENCRYPTION_SECRET was rotated mid-flight. Record it as "auth", the one
            # error_kind that means "you can fix this yourself, in Settings",
            # instead of letting the broad `except Exception` below file it under
            # "internal" — i.e. "our bug" — with a traceback in `job.error`.
            db.rollback()
            job = db.get(GenerationJob, job_id)
            if job is None:
                return
            job.status = "failed"
            job.error_kind = "auth"
            job.error = "No Anthropic API key is configured. Open Settings and paste your key."
            db.commit()
        except GuidedJSONError:
            # rollback FIRST (review fix, CRITICAL): generation flushes each
            # already-drafted module's Blocks onto this Session as it goes
            # (the multi-phase plan-then-ground-then-draft-then-persist
            # shape), so a GuidedJSONError raised while drafting a LATER
            # module can reach this branch with an EARLIER module's Blocks —
            # a half-built course — still sitting flushed-but-uncommitted in
            # this Session's pending transaction. Without a rollback here,
            # the failure-recording commit right below would sweep that
            # fragment into the DB alongside the failed status — a
            # tutor-visible, truncated "curriculum" with no indication it's
            # the wreckage of a failed run. Mirrors the generic `except
            # Exception` branch's own rollback+re-fetch below (rollback
            # expires this Session's identity map, including `job` itself,
            # so it must be re-fetched before being mutated again).
            db.rollback()
            job = db.get(GenerationJob, job_id)
            if job is None:
                log.warning(
                    "run_curriculum_job: job_id=%s gone during failure recovery", job_id)
                return
            job.status = "failed"
            job.error_kind = "upstream"
            job.error = (
                "Curriculum generation failed (model returned invalid/truncated output). Try again."
            )
            db.commit()
        except (openai.APIConnectionError, httpx.TransportError):
            # Same rollback-before-recording reasoning as the GuidedJSONError
            # branch just above — a transport error on module N's draft call
            # can equally strand modules 1..N-1's already-flushed writes in
            # this Session's pending transaction.
            db.rollback()
            job = db.get(GenerationJob, job_id)
            if job is None:
                log.warning(
                    "run_curriculum_job: job_id=%s gone during failure recovery", job_id)
                return
            job.status = "failed"
            job.error_kind = "timeout"
            job.error = "Curriculum generation timed out. Try again."
            db.commit()
        except LLMError as e:
            log.warning("run_curriculum_job: job_id=%s failed at the provider (%s)",
                        job_id, e.kind)
            _record_llm_failure(db, job_id, e, "Curriculum generation")
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
            # The outline is persisted and every lesson is `queued`. The job is NOT
            # done — it now hands off to the fan-out, which drafts them and does
            # the finalizing. Splitting the two would mean a second job row and a
            # second thing for the tutor to poll; keeping it one job means the
            # board he opens is already watching the right id.
            #
            # `root_id` goes into `params` (not just `result_root_id`) because that
            # is what `run_curriculum_draft_job` reads — the same params a RESUME
            # click will later send, so the resume path and the first run are the
            # same code with the same input.
            job.result_root_id = root_id
            job.params = {**job.params, "root_id": str(root_id)}
            db.commit()
            db.close()
            run_curriculum_draft_job(job_id)
            return
    finally:
        db.close()


def run_outline_job(job_id: uuid.UUID) -> None:
    """Generate the interview's outline off the request path, and write it onto the
    interview row. Mirrors `run_curriculum_job` EXACTLY (own session, same
    pending->running->succeeded|failed lifecycle, same `error_kind` classification,
    same best-effort nested failure guard).

    WHY THIS EXISTS: the outline call reads the WHOLE library and runs ~3 minutes —
    past Cloudflare's ~100s edge timeout under orange-cloud. Run synchronously inside
    `POST /curricula/interview/{id}/answer` it produced a 524 at the edge and a
    "could not save the answer" toast, while the model kept working for nobody. This
    is the same off-request treatment `generate_curriculum` and the draft fan-out
    already get; only the outline step was still synchronous.

    `params["interview_id"]` is all it stores: `generate_interview_outline` rebuilds
    the (large, DB-derived) inputs itself, so nothing expensive or version-fragile
    is frozen into the job row."""
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_outline_job: job_id=%s not found, skipping", job_id)
            return

        job.status = "running"
        db.commit()

        try:
            interview_id = uuid.UUID(str(job.params["interview_id"]))
            interview = db.get(CurriculumInterview, interview_id)
            if interview is None:
                raise ValueError(f"CurriculumInterview {interview_id!r} not found")
            # Assigned but NOT committed here — the `else` branch commits the outline
            # and job.status="succeeded" in one transaction (see the docstring of
            # `generate_interview_outline`): a crash between them writes neither.
            interview.outline = generate_interview_outline(db, interview)
        except LLMNotConfigured:
            # Key vanished mid-flight — the one error_kind that says "you can fix this
            # in Settings", not "our bug". Same handling as run_curriculum_job.
            db.rollback()
            job = db.get(GenerationJob, job_id)
            if job is None:
                return
            job.status = "failed"
            job.error_kind = "auth"
            job.error = "No Anthropic API key is configured. Open Settings and paste your key."
            db.commit()
        except GuidedJSONError:
            # `enforce_shape` raises this when the model returns no modules, as does a
            # truncated/invalid structured response. rollback first so the half-written
            # `interview.outline` assignment above is discarded, not committed.
            db.rollback()
            job = db.get(GenerationJob, job_id)
            if job is None:
                return
            job.status = "failed"
            job.error_kind = "upstream"
            job.error = (
                "Outline generation failed (model returned invalid/truncated output). Try again."
            )
            db.commit()
        except (openai.APIConnectionError, httpx.TransportError):
            db.rollback()
            job = db.get(GenerationJob, job_id)
            if job is None:
                return
            job.status = "failed"
            job.error_kind = "timeout"
            job.error = "Outline generation timed out. Try again."
            db.commit()
        except LLMError as e:
            log.warning("run_outline_job: job_id=%s failed at the provider (%s)",
                        job_id, e.kind)
            _record_llm_failure(db, job_id, e, "Outline generation")
        except Exception:
            log.exception("run_outline_job: job_id=%s failed unexpectedly", job_id)
            try:
                db.rollback()
                job = db.get(GenerationJob, job_id)
                if job is None:
                    log.warning(
                        "run_outline_job: job_id=%s gone during failure recovery", job_id)
                    return
                job.status = "failed"
                job.error_kind = "internal"
                job.error = "Outline generation failed unexpectedly. Try again."
                db.commit()
            except Exception:
                log.warning(
                    "run_outline_job: failed to record failure status for job_id=%s",
                    job_id,
                    exc_info=True,
                )
        else:
            # One commit for both the outline (assigned above) and the terminal status.
            job.status = "succeeded"
            job.error = None
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
            # `page_from`/`page_to` is the current job-params shape
            # (`routers/lessons.py::from_selection`, G4); `.get("page_no")`
            # is a defensive fallback for any job row enqueued by an older
            # deploy that still stored the single-page shape (a live app can
            # have a `GenerationJob` row created moments before a deploy).
            lesson_id = draft_lesson_from_selection(
                db,
                source_id=uuid.UUID(job.params["source_id"]),
                page_from=job.params.get("page_from", job.params.get("page_no")),
                page_to=job.params.get("page_to", job.params.get("page_no")),
                text=job.params["text"],
                # `.get(..., DEFAULT_LOCALE)`, not `"en"` (Plan 13, Stage 5.5):
                # `routers/lessons.py` and the chat approval path both put a real
                # locale in `params` now, so this fallback only ever fires for a
                # job row enqueued by an older deploy — and even then the honest
                # default is the app's own default language, not English.
                language=job.params.get("language", DEFAULT_LOCALE),
            )
        except LLMNotConfigured:
            # The key was there when this job was enqueued (`require_llm_configured`
            # gates every producer) and is gone now — the tutor cleared it, or the
            # ENCRYPTION_SECRET was rotated mid-flight. Record it as "auth", the one
            # error_kind that means "you can fix this yourself, in Settings",
            # instead of letting the broad `except Exception` below file it under
            # "internal" — i.e. "our bug" — with a traceback in `job.error`.
            db.rollback()
            job = db.get(GenerationJob, job_id)
            if job is None:
                return
            job.status = "failed"
            job.error_kind = "auth"
            job.error = "No Anthropic API key is configured. Open Settings and paste your key."
            db.commit()
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
        except LLMError as e:
            log.warning("run_lesson_job: job_id=%s failed at the provider (%s)",
                        job_id, e.kind)
            _record_llm_failure(db, job_id, e, "Lesson drafting")
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
        except LLMNotConfigured:
            # The key was there when this job was enqueued (`require_llm_configured`
            # gates every producer) and is gone now — the tutor cleared it, or the
            # ENCRYPTION_SECRET was rotated mid-flight. Record it as "auth", the one
            # error_kind that means "you can fix this yourself, in Settings",
            # instead of letting the broad `except Exception` below file it under
            # "internal" — i.e. "our bug" — with a traceback in `job.error`.
            db.rollback()
            job = db.get(GenerationJob, job_id)
            if job is None:
                return
            job.status = "failed"
            job.error_kind = "auth"
            job.error = "No Anthropic API key is configured. Open Settings and paste your key."
            db.commit()
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
