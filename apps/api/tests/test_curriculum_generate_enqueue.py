"""Tests for POST /curricula/generate's async enqueue contract (Plan 8 Task
3). Replaces the old sync error-mapping tests that used to live in
test_curriculum_generate_errors.py (now deleted): that mapping moved into
the background runner (`app.jobs.runner.run_curriculum_job`), already
covered by test_jobs_runner.py.

Unlike its predecessor, this module DOES need a live Postgres: the endpoint
now unconditionally creates + commits a `GenerationJob` row before it
returns, and these tests poll `GET /jobs/{id}` to prove that — mirrors
test_jobs_api.py's skip-guard + setup_module pattern.

`run_curriculum_job` is monkeypatched to a capturing no-op in every test
here — the CRITICAL gotcha (Plan 8 Task 3 brief): Starlette's `TestClient`
runs `BackgroundTasks` AFTER the response, in-process, so an unpatched test
would trigger a real 49-179s LLM call. Patches `app.routers.curriculum.
run_curriculum_job` specifically (the name as bound into the router
module's own namespace by its `from ... import run_curriculum_job`), not
`app.jobs.runner.run_curriculum_job` — patching the origin module would not
affect the router's already-bound reference (mirrors the now-deleted
test_curriculum_generate_errors.py's identical reasoning about
generate_curriculum).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.routers.curriculum as curriculum_router
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.generation_job import GenerationJob

# Skip cleanly (not error) when no DB is reachable — mirrors test_jobs_api.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


client = TestClient(app)

_PAYLOAD = {
    "title": "Test Course",
    "language": "en",
    "profile": {"level": "beginner"},
    "domain": "test-domain",
    "target_minutes_total": 300,
}


def test_generate_curriculum_endpoint_returns_202_and_schedules_the_runner(monkeypatch):
    """Proves the enqueue contract end to end: 202 + job_id in the response;
    `run_curriculum_job` scheduled via BackgroundTasks with that SAME id;
    and — since the patched runner never advances anything — a
    `GET /jobs/{id}` poll right after still sees `pending`, which can only
    be explained by `db.commit()` happening inside the endpoint itself,
    BEFORE the 202 was returned (not left for the background task).
    """
    scheduled_job_ids = []
    monkeypatch.setattr(curriculum_router, "run_curriculum_job", scheduled_job_ids.append)

    r = client.post("/curricula/generate", json=_PAYLOAD)

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"
    job_id = uuid.UUID(body["job_id"])

    # BackgroundTasks (run in-process, after the response, by TestClient)
    # scheduled the runner with the SAME id the response returned.
    assert scheduled_job_ids == [job_id]

    r2 = client.get(f"/jobs/{job_id}")
    assert r2.status_code == 200, r2.text
    job = r2.json()
    assert job["kind"] == "curriculum"
    assert job["status"] == "pending"
    assert job["result_root_id"] is None
    assert job["error"] is None
    assert job["error_kind"] is None


def test_generate_curriculum_endpoint_persists_request_params_verbatim_on_the_job(monkeypatch):
    """The runner (T2) calls `generate_curriculum(db, **job.params)` — so the
    endpoint's `params` dict must carry every `generate_curriculum` kwarg,
    named and valued exactly as the request supplied them. `JobOut` doesn't
    expose `params`, so this checks the DB row directly (mirrors
    test_assign_curriculum_deep_clones_into_a_new_student_scoped_tree's own
    direct-DB-check style in test_curriculum_api.py).
    """
    monkeypatch.setattr(curriculum_router, "run_curriculum_job", lambda job_id: None)

    r = client.post("/curricula/generate", json=_PAYLOAD)
    assert r.status_code == 202, r.text
    job_id = uuid.UUID(r.json()["job_id"])

    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        assert job.params == _PAYLOAD
    finally:
        db.close()


def test_generate_curriculum_endpoint_persists_an_explicit_empty_source_ids_list(monkeypatch):
    """Review finding (MINOR, routers/curriculum.py): `if payload.source_ids:`
    is falsy for an explicitly-passed empty list `[]`, so it used to be
    silently DROPPED from `params` — `generate_curriculum` would then see
    `source_ids=None` (its own default) and fall back to whole-library
    retrieval, the opposite of "ground in nothing" the tutor asked for by
    sending `[]`. The router must distinguish "key omitted" (None — no
    scoping requested, old/back-compat behaviour) from "key explicitly []"
    (ground in nothing) via `is not None`, not truthiness.
    """
    monkeypatch.setattr(curriculum_router, "run_curriculum_job", lambda job_id: None)

    payload = {**_PAYLOAD, "source_ids": []}
    r = client.post("/curricula/generate", json=payload)
    assert r.status_code == 202, r.text
    job_id = uuid.UUID(r.json()["job_id"])

    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        assert "source_ids" in job.params, (
            "an explicit [] must be persisted, not silently dropped"
        )
        assert job.params["source_ids"] == []
    finally:
        db.close()
