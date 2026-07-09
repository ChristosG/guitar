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
from sqlalchemy import update

from app.models.generation_job import GenerationJob


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
