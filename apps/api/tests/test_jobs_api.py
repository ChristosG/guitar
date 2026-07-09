"""Tests for `GET /jobs/{job_id}` (Plan 8 Task 2's poll endpoint).

Mirrors `test_artifacts_api.py`'s DB skip-guard + setup_module pattern — a
plain DB-backed CRUD-read route, no LLM involved at all, so (like that
module's create/list/delete tests) nothing here is marked `integration`.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.generation_job import GenerationJob

# Skip cleanly (not error) when no DB is reachable — mirrors test_artifacts_api.py.
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


def test_get_job_returns_pending_job_by_id():
    job = _create_job()

    r = client.get(f"/jobs/{job.id}")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(job.id)
    assert body["kind"] == "curriculum"
    assert body["status"] == "pending"
    assert body["result_root_id"] is None
    assert body["error"] is None
    assert body["error_kind"] is None
    assert body["created_at"]
    assert body["updated_at"]


def test_get_job_reflects_succeeded_status_and_result_root_id():
    root_id = uuid.uuid4()
    job = _create_job(status="succeeded", result_root_id=root_id)

    r = client.get(f"/jobs/{job.id}")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["result_root_id"] == str(root_id)


def test_get_job_reflects_failed_status_and_error_fields():
    job = _create_job(
        status="failed", error="Curriculum generation timed out. Try again.", error_kind="timeout",
    )

    r = client.get(f"/jobs/{job.id}")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert body["error_kind"] == "timeout"
    assert body["error"] == "Curriculum generation timed out. Try again."


def test_get_job_unknown_id_404s():
    r = client.get(f"/jobs/{uuid.uuid4()}")
    assert r.status_code == 404, r.text
