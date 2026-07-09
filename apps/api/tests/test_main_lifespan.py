"""Test for `app.main`'s `lifespan` wiring (Plan 8 Task 3): the startup
sweep must actually run when the app starts, not merely exist as an unwired
function alongside it. Mirrors test_jobs_sweep.py's skip-guard +
setup_module pattern — same real-DB, no-LLM shape.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import Base, SessionLocal, engine

# Skip cleanly (not error) when no DB is reachable — mirrors test_jobs_sweep.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )

from app.main import app
from app.models.generation_job import GenerationJob


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


def test_app_startup_sweeps_orphaned_jobs_via_the_real_lifespan():
    """Real DB, no monkeypatch — the strongest available proof that
    `lifespan` is actually passed to `FastAPI(lifespan=...)` and calls the
    real `sweep_orphaned_jobs` on startup: seed a `pending` job, enter
    `TestClient(app)` AS A CONTEXT MANAGER (that's what triggers ASGI
    startup — a bare `TestClient(app)`, used everywhere else in this test
    suite, does NOT run lifespan events at all — verified empirically for
    this codebase's installed Starlette version), then confirm the row was
    already flipped to `failed` by the time the `with` block starts serving.
    """
    db = SessionLocal()
    try:
        job = GenerationJob(kind="curriculum", status="pending", params={"title": "T"})
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    with TestClient(app) as client:
        r = client.get(f"/jobs/{job_id}")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert body["error_kind"] == "internal"
    assert body["error"] == "interrupted by a restart"
