"""Tests for `sweep_orphaned_jobs` (Plan 8 Task 3's startup sweep).

`run_curriculum_job` can only ever advance a `GenerationJob` while the
process that scheduled it (via `BackgroundTasks`) is alive — a restart kills
that in-process background task mid-flight, leaving the row stuck at
`pending`/`running` forever. `sweep_orphaned_jobs` is the backstop `app.main`'s
`lifespan` runs once on startup to fail every such orphan outright. Mirrors
`test_jobs_runner.py`'s skip-guard + setup_module + own-session pattern: a
plain DB-backed bulk update, no LLM involved, so not marked `integration`.
"""
import pytest
from sqlalchemy import text

from app.db import Base, SessionLocal, engine

# Skip cleanly (not error) when no DB is reachable — mirrors test_jobs_runner.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )

import app.models  # noqa: F401  register every model's table on Base.metadata
from app.jobs.sweep import sweep_orphaned_jobs
from app.models.generation_job import GenerationJob


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


def _create_job(**overrides) -> GenerationJob:
    fields = {
        "kind": "curriculum",
        "status": "pending",
        "params": {"title": "T", "language": "en", "profile": {}},
        **overrides,
    }
    db = SessionLocal()
    try:
        job = GenerationJob(**fields)
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    finally:
        db.close()


def _reread(job_id) -> GenerationJob:
    """Fresh session -> a genuine DB round-trip (mirrors test_jobs_runner.py)."""
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        return job
    finally:
        db.close()


def test_sweep_orphaned_jobs_fails_pending_and_running_but_leaves_succeeded_alone():
    pending_job = _create_job(status="pending")
    running_job = _create_job(status="running")
    succeeded_job = _create_job(status="succeeded")

    db = SessionLocal()
    try:
        count = sweep_orphaned_jobs(db)
    finally:
        db.close()

    assert count == 2

    pending_after = _reread(pending_job.id)
    assert pending_after.status == "failed"
    assert pending_after.error_kind == "internal"
    assert pending_after.error == "interrupted by a restart"

    running_after = _reread(running_job.id)
    assert running_after.status == "failed"
    assert running_after.error_kind == "internal"
    assert running_after.error == "interrupted by a restart"

    succeeded_after = _reread(succeeded_job.id)
    assert succeeded_after.status == "succeeded"  # untouched
    assert succeeded_after.error is None
    assert succeeded_after.error_kind is None


def test_sweep_orphaned_jobs_returns_zero_when_nothing_is_orphaned():
    _create_job(status="succeeded")
    _create_job(status="failed", error_kind="upstream", error="already recorded")

    db = SessionLocal()
    try:
        count = sweep_orphaned_jobs(db)
    finally:
        db.close()

    assert count == 0


def test_sweep_stuck_compiles_fails_running_and_leaves_terminal_states_alone():
    """`compile_book` commits `status="running"` BEFORE its one long model call.
    A force-quit during those minutes used to leave the row `running` forever —
    a permanent spinner in the Library with no button, because nothing ever
    read the crash marker `models/canon.py` documents. The boot sweep is that
    reader."""
    from app.jobs.sweep import sweep_stuck_compiles
    from app.models.canon import BookCompile
    from app.models.knowledge import KnowledgeSource

    db = SessionLocal()
    try:
        # One compile per source — `source_id` IS the primary key.
        srcs = [KnowledgeSource(type="pdf", title=t, status="ready")
                for t in ("Running", "Ready", "Failed")]
        db.add_all(srcs)
        db.commit()
        db.add_all([
            BookCompile(source_id=srcs[0].id, status="running"),
            BookCompile(source_id=srcs[1].id, status="ready"),
            BookCompile(source_id=srcs[2].id, status="failed", error="old reason"),
        ])
        db.commit()
        ids = [s.id for s in srcs]

        count = sweep_stuck_compiles(db)
        assert count == 1
    finally:
        db.close()

    # Fresh session -> genuine DB round-trip (`_reread`'s reasoning: the bulk
    # UPDATE bypasses the identity map, so same-session reads are stale).
    db = SessionLocal()
    try:
        assert db.get(BookCompile, ids[0]).status == "failed"
        assert "interrupted" in db.get(BookCompile, ids[0]).error
        assert db.get(BookCompile, ids[1]).status == "ready"      # untouched
        assert db.get(BookCompile, ids[2]).error == "old reason"  # untouched
    finally:
        db.close()


def test_sweep_stuck_ingests_fails_sources_orphaned_mid_ingest():
    """Upload ingest runs synchronously inside the HTTP request, so a source
    still `ingesting` at BOOT can only mean the process died mid-ingest —
    nothing was ever going to finish it, and the row spun forever."""
    from app.jobs.sweep import sweep_stuck_ingests
    from app.models.knowledge import KnowledgeSource

    db = SessionLocal()
    try:
        stuck = KnowledgeSource(type="pdf", title="Half-uploaded", status="ingesting")
        fine = KnowledgeSource(type="pdf", title="Fine", status="ready")
        db.add_all([stuck, fine])
        db.commit()
        stuck_id, fine_id = stuck.id, fine.id

        count = sweep_stuck_ingests(db)
        assert count == 1
    finally:
        db.close()

    db = SessionLocal()  # fresh session — same identity-map reasoning as above
    try:
        after = db.get(KnowledgeSource, stuck_id)
        assert after.status == "failed"
        assert "upload the file again" in after.error
        assert db.get(KnowledgeSource, fine_id).status == "ready"  # untouched
    finally:
        db.close()
