"""Startup sweep for `GenerationJob` rows orphaned by a server restart
(Plan 8 Task 3).

`run_curriculum_job` (`app.jobs.runner`) only ever flips a job `pending` ->
`running` -> `succeeded`|`failed` while the process that scheduled it (via
`BackgroundTasks`) stays alive. A restart (deploy, crash, OOM) kills that
in-process background task mid-flight, leaving the row stuck at `pending`
(never got scheduled/started before the restart) or `running` (mid-generation
when the process died) forever — nothing is left running to ever flip it to
a terminal status. `app.main`'s `lifespan` calls this once on startup, before
serving any request, to fail every such orphan outright rather than leave a
poller waiting on a job nothing will ever finish.

A single bulk `UPDATE`, not a fetch-then-loop — mirrors `app.curriculum.
segment`'s own bulk `delete(Block)...` for a whole-set mutation that needs no
already-loaded ORM object, and is one round-trip regardless of row count.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from app.models.block import Block
from app.models.chat import ChatSession, Message
from app.models.generation_job import GenerationJob
from app.models.interview import CurriculumInterview


def sweep_interrupted_lessons(db) -> int:
    """Every lesson left `drafting` by a restart goes back to `queued`. Returns the
    count.

    NOT `failed`, and the distinction is the difference between a feature and a
    bug report. A lesson that was mid-draft when the container restarted has
    nothing wrong with it — no model has judged it, no content is bad. Marking it
    `failed` would paint the tutor's board red after a routine deploy and tell him
    his curriculum broke, when the truth is "click Resume". `queued` is exactly
    what it is: waiting to be written.

    Row-by-row rather than a bulk UPDATE, unlike its sibling below, because
    `Block.meta` is a JSON blob and the status is one key inside it — a bulk
    `values(meta=...)` would have to overwrite the whole document and would take
    `word_count`, `citations` and `objective` with it.
    """
    stuck = db.scalars(
        select(Block).where(
            Block.kind == "lesson",
            Block.meta["draft_status"].as_string() == "drafting",
        )
    ).all()
    for lesson in stuck:
        # Whole-dict reassignment — plain sa.JSON, no MutableDict (see Block.meta).
        lesson.meta = {
            **(lesson.meta or {}),
            "draft_status": "queued",
            "error": "interrupted by a restart — press Resume",
        }
    db.commit()
    return len(stuck)


def sweep_orphaned_jobs(db) -> int:
    """Fail every `GenerationJob` left `pending`/`running` (restart-orphaned);
    return the count of rows updated. `db` is a caller-owned SQLAlchemy
    Session (mirrors `segment_block`/`ingest_source` — untyped for the same
    reason: this is a plain business-logic module, not a router). Commits
    here — the sweep's persisted effect is its entire contract, same
    reasoning as `generate_curriculum`'s own commit.
    """
    result = db.execute(
        update(GenerationJob)
        .where(GenerationJob.status.in_(("pending", "running")))
        .values(status="failed", error_kind="internal", error="interrupted by a restart")
    )
    db.commit()
    return result.rowcount


# Retention windows. Generous on purpose: these tables are audit/debugging
# records worth keeping for a while, not worth keeping FOREVER — this app's
# contract is "runs unattended for years", and these were its only unbounded
# tables (a job row per OCR press/draft/resume, an invisible empty chat
# session per visit to /chat before the first message ever lands, an
# interview row per opened dialog).
_JOB_RETENTION_DAYS = 30
_EMPTY_SESSION_RETENTION_DAYS = 7
_INTERVIEW_RETENTION_DAYS = 30


def sweep_expired_records(db) -> dict:
    """Boot-time retention: terminal job rows past their window, chat sessions
    that never got a message, and interviews that finished (or were abandoned)
    weeks ago. Returns per-table counts. Boot-time (not a cron) for the same
    reason the other sweeps are: a restart is the one moment guaranteed to
    happen on a machine with no operator, and daily granularity is plenty."""
    now = datetime.now(timezone.utc)

    jobs = db.execute(
        delete(GenerationJob).where(
            GenerationJob.status.in_(("succeeded", "failed")),
            GenerationJob.created_at < now - timedelta(days=_JOB_RETENTION_DAYS),
        )
    ).rowcount

    empty_sessions = db.execute(
        delete(ChatSession).where(
            ChatSession.created_at < now - timedelta(days=_EMPTY_SESSION_RETENTION_DAYS),
            ~ChatSession.id.in_(select(Message.session_id).distinct()),
        )
    ).rowcount

    interviews = db.execute(
        delete(CurriculumInterview).where(
            CurriculumInterview.updated_at < now - timedelta(days=_INTERVIEW_RETENTION_DAYS),
        )
    ).rowcount

    db.commit()
    return {"jobs": jobs, "empty_sessions": empty_sessions, "interviews": interviews}
