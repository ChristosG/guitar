"""Tests for the `GenerationJob` model's DB round-trip (Plan 8 Task 1).

Mirrors `test_artifact_specs.py`'s skip-guard + `setup_module` pattern (this
codebase's current convention for DB-touching test modules) rather than
`test_models_roundtrip.py`'s older standalone one.
"""
import pytest
from sqlalchemy import text

from app.db import Base, SessionLocal, engine

# Skip cleanly (not error) when no DB is reachable — mirrors test_students_api.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )

import app.models  # noqa: F401  register every model's table on Base.metadata
from app.models.generation_job import GenerationJob


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


def test_generation_job_persists_and_rereads():
    db = SessionLocal()
    try:
        job = GenerationJob(
            kind="curriculum",
            status="pending",
            params={"title": "X", "language": "en", "profile": {}},
        )
        db.add(job); db.commit()
        job_id = job.id
    finally:
        db.close()

    # Fresh session -> a genuine DB round-trip, not the identity-map object
    # reused under expire_on_commit=False (mirrors test_models_roundtrip.py).
    db2 = SessionLocal()
    try:
        got = db2.get(GenerationJob, job_id)
        assert got is not None
        assert got.kind == "curriculum"
        assert got.status == "pending"
        assert got.params == {"title": "X", "language": "en", "profile": {}}  # JSON round-trip
        assert got.result_root_id is None
        assert got.error is None
        assert got.error_kind is None
    finally:
        db2.close()


def test_generation_job_defaults_status_pending_when_omitted():
    db = SessionLocal()
    try:
        job = GenerationJob(kind="curriculum", params={"title": "Y"})
        db.add(job); db.commit()
        db.refresh(job)
        assert job.status == "pending"        # column default, not passed explicitly
        assert job.result_root_id is None
        assert job.error is None
        assert job.error_kind is None
    finally:
        db.close()
