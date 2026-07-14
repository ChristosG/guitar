import os

# MUST run before any `from app...` import (here or in any test module pytest
# collects afterwards): app.config.Settings() reads DATABASE_URL at import
# time, and app.db builds its `engine` from that setting at import time too.
# Forcing the env var here — before this module's own app imports below —
# guarantees every test binds to guitar_test, no matter how pytest was
# invoked (bare `pytest`, or with DATABASE_URL already pointed at the app's
# `guitar` DB). This is what stops integration tests from polluting the app
# DB: previously they ran against whatever DATABASE_URL was in the caller's
# environment, wrote rows, and never cleaned up.
os.environ["DATABASE_URL"] = "postgresql+psycopg://guitar:guitar@localhost:5434/guitar_test"

import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient

import app.models  # noqa: F401  register every model's table on Base.metadata
from app.db import Base, SessionLocal, engine
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Build the guitar_test schema once per test run.

    pgvector's `vector` type must exist before create_all, since Chunk.embedding
    is a Vector column. Individual test modules also call
    `Base.metadata.create_all(engine)` in their own `setup_module` (pre-dating
    this fixture) — that's a harmless no-op here since create_all checks for
    existing tables first, and this fixture (session-scoped) always runs
    before any module-scoped setup for the first test that needs it.
    """
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def _truncate_all_tables(_test_database):
    """Isolate tests from each other: wipe every table after each test.

    Depends on `_test_database` so table creation always happens first. Runs
    after (not before) each test so a failed test still leaves the DB clean
    for the next one, and TRUNCATE ... RESTART IDENTITY CASCADE handles FKs
    between tables (e.g. block.parent_id, chunk.source_id, the new
    curriculum tables) without needing per-table ordering.
    """
    yield
    table_names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    if table_names:
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
def _no_live_retrieval(monkeypatch):
    """Unit tests must not reach the live embedding server.

    Plan 13 INVERTED the forced-retrieval gate (`agent/loop.py::
    _is_content_bearing`): it used to require a positive "this looks like a
    question" match before grounding, and now it grounds by DEFAULT, skipping
    only obvious small talk and obvious entity commands. That is the right
    product behaviour — the old rule failed OPEN, silently answering every Greek
    question ungrounded — but it means the pre-hop now fires on nearly every turn
    a test drives, where before it fired on almost none.

    Sixteen loop/router unit tests, all using fake LLM providers, therefore
    started calling the real `search()` -> the real embedder -> a vLLM hostname
    that only resolves inside the compose network. They were never *meant* to do
    live retrieval; the narrow old gate was accidentally hiding that.

    So: retrieval returns nothing by default, and a test that actually cares
    about grounding patches `search` itself — its `monkeypatch.setattr` runs
    after this fixture and wins (see `test_agent_grounding.py`).
    """
    monkeypatch.setattr("app.agent.loop.search", lambda *a, **k: [], raising=False)


@pytest.fixture
def db():
    """Plain SQLAlchemy session for model-level tests (e.g. test_library_models.py).

    Mirrors `app.db.get_db`, just without the FastAPI generator wrapping —
    tests want a bare session they can `.add()`/`.commit()`/`.get()` on
    directly. Rows are cleaned up by `_truncate_all_tables` above, so this
    fixture itself does no rollback/truncation.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    """FastAPI TestClient for API endpoint tests."""
    return TestClient(app)
