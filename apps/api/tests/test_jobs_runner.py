"""Tests for `run_curriculum_job` (Plan 8 Task 2's background runner).

`generate_curriculum` is monkeypatched in every test here — no live LLM call
ever happens — but (unlike `test_curriculum_generate_errors.py`) this module
DOES need a live Postgres: `run_curriculum_job` opens its OWN `SessionLocal()`
and reads/writes a real `GenerationJob` row, and the entire point of these
tests is proving that own-session commit is actually visible to a separate
reader afterward. Mirrors `test_generation_job.py`'s skip-guard +
setup_module pattern (DB-touching, no live LLM -> no `integration` marker).

Own-session proof (every test below relies on this shape): the job is
created and committed in one `SessionLocal()` (closed before the runner ever
runs, via `_create_job`), `run_curriculum_job` is called with ONLY the id
(it cannot see the test's session at all, let alone reuse it), and the
result is observed through YET ANOTHER fresh `SessionLocal()` (`_reread`).
A same-session `db.refresh(job)` would only prove the runner mutated some
object reachable from the test's own session — which it isn't, since only a
bare UUID crosses the call boundary — so three independent sessions is the
strongest available proof that the status change is a real, durably
committed row, not an in-process artifact of a shared/stale session.
"""
import uuid

import pytest
from sqlalchemy import text

from app.db import Base, SessionLocal, engine

# Skip cleanly (not error) when no DB is reachable — mirrors test_generation_job.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )

import app.jobs.runner as runner
import app.models  # noqa: F401  register every model's table on Base.metadata
from app.llm.errors import GuidedJSONError
from app.models.block import Block
from app.models.generation_job import GenerationJob


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


_PARAMS = {"title": "Test Course", "language": "en", "profile": {"level": "beginner"}}


def _create_job(**overrides) -> GenerationJob:
    db = SessionLocal()
    try:
        job = GenerationJob(kind="curriculum", status="pending", params=dict(_PARAMS), **overrides)
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    finally:
        db.close()


def _reread(job_id: uuid.UUID) -> GenerationJob:
    """Fresh session -> a genuine DB round-trip (see module docstring)."""
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        return job
    finally:
        db.close()


def test_run_curriculum_job_outlines_then_hands_off_to_the_lesson_fan_out(monkeypatch):
    """`generate_curriculum` no longer drafts anything — it outlines the course over
    the whole library and persists a tree of QUEUED lessons. This job then hands
    that root to the fan-out (`run_curriculum_draft_job`), which is what actually
    writes the twenty lessons and finalizes the row.

    The handoff goes through `params["root_id"]` and not just `result_root_id`,
    because that is exactly the params shape a RESUME click later sends — the first
    run and the resume are the same code reading the same input.
    """
    job = _create_job()
    fixed_root_id = uuid.uuid4()
    seen_kwargs = {}
    handed_off = []

    def _fake_generate_curriculum(db, **kwargs):
        seen_kwargs.update(kwargs)
        return fixed_root_id

    monkeypatch.setattr(runner, "generate_curriculum", _fake_generate_curriculum)
    monkeypatch.setattr(runner, "run_curriculum_draft_job", handed_off.append)

    runner.run_curriculum_job(job.id)

    assert seen_kwargs == _PARAMS  # job.params unpacked as kwargs, verbatim
    assert handed_off == [job.id], "the outline must hand off to the lesson fan-out"
    got = _reread(job.id)
    assert got.result_root_id == fixed_root_id
    assert got.params["root_id"] == str(fixed_root_id)
    assert got.error_kind is None


def test_run_curriculum_job_drops_params_from_an_older_deploy(monkeypatch):
    """`GenerationJob.params` is a wire format with NO VERSION: a row can be written
    seconds before a deploy and executed seconds after it. Every curriculum job this
    app ever enqueued carried `domain`, which Stage 6 deleted — so
    `generate_curriculum(db, **params)` would `TypeError`, get swallowed by the
    broad `except Exception`, and be recorded as `internal` ("our bug") on a job
    whose only sin was being five seconds early.
    """
    db = SessionLocal()
    try:
        job = GenerationJob(
            kind="curriculum", status="pending",
            params={**_PARAMS, "domain": "tone", "allow_general": False},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    seen_kwargs = {}
    monkeypatch.setattr(runner, "run_curriculum_draft_job", lambda _id: None)
    monkeypatch.setattr(
        runner, "generate_curriculum",
        lambda db, **kwargs: (seen_kwargs.update(kwargs), uuid.uuid4())[1],
    )

    runner.run_curriculum_job(job_id)

    assert "domain" not in seen_kwargs, "a dead param must be dropped, not raised on"
    assert seen_kwargs["allow_general"] is False  # a live one still gets through
    assert _reread(job_id).error_kind is None


def test_run_curriculum_job_guided_json_error_sets_failed_upstream(monkeypatch):
    job = _create_job()

    def _raise(*args, **kwargs):
        raise GuidedJSONError("guided_json: response truncated (finish_reason='length')")

    monkeypatch.setattr(runner, "generate_curriculum", _raise)

    runner.run_curriculum_job(job.id)

    got = _reread(job.id)
    assert got.status == "failed"
    assert got.error_kind == "upstream"
    assert got.error and "try again" in got.error.lower()
    assert got.result_root_id is None


def test_run_curriculum_job_provider_timeout_sets_failed_timeout(monkeypatch):
    """Transport failures reach the runner as `LLMError(kind="timeout")` — the
    provider's `_mapped_errors` (llm/claude.py) maps every Anthropic
    connection/timeout exception into the app's own taxonomy, which is what
    replaced the old `except (openai.APIConnectionError, httpx.TransportError)`
    era. The runner's job is to file it under its own kind, not "internal"."""
    from app.llm.errors import LLMError

    job = _create_job()

    def _raise(*args, **kwargs):
        raise LLMError("timeout", "Could not reach Anthropic: timed out")

    monkeypatch.setattr(runner, "generate_curriculum", _raise)

    runner.run_curriculum_job(job.id)

    got = _reread(job.id)
    assert got.status == "failed"
    assert got.error_kind == "timeout"
    assert got.error and "timed out" in got.error.lower()
    assert got.result_root_id is None  # a failed job never carries a result


def test_run_curriculum_job_generic_exception_sets_failed_internal(monkeypatch):
    job = _create_job()

    def _raise(*args, **kwargs):
        raise RuntimeError("boom - some internal detail")

    monkeypatch.setattr(runner, "generate_curriculum", _raise)

    runner.run_curriculum_job(job.id)

    got = _reread(job.id)
    assert got.status == "failed"
    assert got.error_kind == "internal"
    assert got.error
    assert "boom" not in got.error  # real detail is logged, not stored on the row
    assert got.result_root_id is None  # a failed job never carries a result


def test_run_curriculum_job_unknown_job_id_is_a_defensive_noop():
    """The enqueue endpoint (Task 3) always creates the row before scheduling
    this runner, so a missing job is not expected in practice — but the
    runner must not raise if it somehow happens (see its docstring).
    """
    runner.run_curriculum_job(uuid.uuid4())  # must not raise


def test_run_curriculum_job_rolls_back_partial_writes_before_recording_internal_failure(monkeypatch):
    """The `db.rollback()` in the internal-failure branch earns its place.

    Unlike the other failure tests (which monkeypatch generate_curriculum to
    raise BEFORE touching the DB, so the rollback is a no-op), this one makes
    the generation dirty the runner's OWN session with a real INSERT — as the
    live `_persist_tree` does mid-tree — and THEN raise. Without the rollback,
    the failure-recording `db.commit()` would sweep that orphaned partial
    write into the DB alongside the failed status; with it, the write is
    discarded. Asserts both the failed/internal outcome AND that the stray
    Block was NOT persisted.
    """
    job = _create_job()
    stray_block_id = uuid.uuid4()

    def _dirty_then_raise(db, **kwargs):
        db.add(Block(id=stray_block_id, kind="course", title="partial", order=0, language="en"))
        db.flush()  # emit the INSERT into the transaction (mirrors _persist_tree's own flush)
        raise RuntimeError("blew up after a partial write")

    monkeypatch.setattr(runner, "generate_curriculum", _dirty_then_raise)

    runner.run_curriculum_job(job.id)

    got = _reread(job.id)
    assert got.status == "failed"
    assert got.error_kind == "internal"

    # The orphaned partial write was rolled back, not committed alongside the
    # failure record — proven via a fresh session.
    db = SessionLocal()
    try:
        assert db.get(Block, stray_block_id) is None
    finally:
        db.close()


def test_run_curriculum_job_swallows_db_failure_during_recovery(monkeypatch):
    """The recovery block is itself best-effort guarded.

    If the DB is unreachable at the exact moment the runner tries to RECORD
    the failure (the recovery rollback/re-fetch/commit), run_curriculum_job
    must still NOT propagate — otherwise the job is re-stranded in "running",
    the precise outcome this whole layer exists to prevent. Simulated by
    letting the first commit (status="running") succeed but making the second
    (the recovery commit) raise; the nested guard must swallow it.
    """
    job = _create_job()

    def _raise(*args, **kwargs):
        raise RuntimeError("generation blew up")

    monkeypatch.setattr(runner, "generate_curriculum", _raise)

    real_session_factory = runner.SessionLocal

    def _session_that_fails_on_recovery_commit():
        db = real_session_factory()
        real_commit = db.commit
        calls = {"n": 0}

        def _commit():
            calls["n"] += 1
            if calls["n"] == 1:
                return real_commit()  # the status="running" commit must succeed
            raise RuntimeError("db gone during recovery commit")

        db.commit = _commit
        return db

    monkeypatch.setattr(runner, "SessionLocal", _session_that_fails_on_recovery_commit)

    runner.run_curriculum_job(job.id)  # must NOT raise — the guard swallows the recovery failure

    # The recovery commit never landed, so the row is left at "running" (the
    # residual strand Task 3's startup sweep is the backstop for) — not
    # crashed, not silently "succeeded". `_reread` uses app.db.SessionLocal
    # (unpatched), so it sees the real committed state.
    got = _reread(job.id)
    assert got.status == "running"
