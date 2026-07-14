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

# Same import-time reasoning, for the password gate (Plan 13 Task 3.1). ~40 test
# modules drive the API through `TestClient` with no cookie jar; with the gate on
# they would every one of them 401. `Settings.auth_enabled` already defaults to
# False, so this is belt-and-braces — it pins the value even if a developer has
# `AUTH_ENABLED=1` exported in the shell they run pytest from (which they will,
# because that is what the deployed app needs). `tests/test_auth.py` turns the
# gate ON explicitly, per test, with monkeypatch.
os.environ["AUTH_ENABLED"] = "0"

import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient

import app.models  # noqa: F401  register every model's table on Base.metadata
from app.db import Base, SessionLocal, engine
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Rebuild the guitar_test schema from the models, once per test run.

    DROP THEN CREATE, and the drop is not optional. `create_all` SKIPS a table that
    already exists — it does not ALTER it. So the moment a model grows a column,
    every test that touches that table fails with `UndefinedColumn` against a
    schema built by some earlier run, and the failure names the column rather than
    the cause: 200 red tests, none of which have anything to do with the change.
    (Stage 6 added `block.meta`, `student.goals`, `generation_job.progress` and
    four interview columns, and that is exactly what happened.)

    Free to do: `_truncate_all_tables` already wipes every table after every test,
    so nothing in this database was ever meant to outlive a run. This just makes
    the SCHEMA as disposable as the rows always were.

    pgvector's `vector` type must exist first — `Chunk.embedding` is a Vector
    column. Individual test modules also call `create_all` in their own
    `setup_module` (pre-dating this fixture); harmless, since this session-scoped
    fixture always runs first.
    """
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.drop_all(engine)
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


class _FakeEmbedder:
    """Deterministic 384-dim vectors, no model, no network, ~0ms.

    Hash-based rather than random: the same text always yields the same vector,
    so a test can assert that two identical chunks embed identically, and a test
    run is reproducible. Unit-normalised, because everything downstream
    (`cosine_distance`, the RRF fusion, the cosine floor) assumes unit vectors —
    a fake that skipped that would let a bug through that the real embedder
    would have caught.
    """

    dim = 384
    model_id = "fake-embedder"

    def embed(self, texts, *, is_query: bool = False):
        import hashlib
        import struct

        out = []
        for t in texts:
            h = hashlib.sha256((("q:" if is_query else "p:") + t).encode()).digest()
            # stretch 32 bytes -> 384 floats deterministically
            raw = b"".join(
                hashlib.sha256(h + bytes([i])).digest() for i in range(48)
            )[: 384 * 4]
            # Unpack as UNSIGNED INTS and map into [-1, 1) — NOT as `<384f`.
            #
            # Reinterpreting hash bytes as raw float32 bit patterns looks equivalent
            # and is not: a float32 whose 8 exponent bits are all 1 is inf or NaN,
            # which is 1/256 of uniformly random patterns. Over 384 draws that is
            # 1 - (255/256)^384 = 78% of vectors carrying at least one non-finite
            # value, and a single NaN poisons the whole vector through the
            # normalisation sum (n becomes NaN; every x/n follows). Measured: 394 of
            # 500. It surfaced as `psycopg.errors.DataException: NaN not allowed in
            # vector` on any test that actually persisted a chunk. Integers have no
            # such trap representation.
            ints = struct.unpack("<384I", raw)
            v = [(u / 2147483647.5) - 1.0 for u in ints]
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out

    def health(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _fake_embedder(monkeypatch, request):
    """Unit tests must NOT load the real embedding model.

    Since Plan 13 Stage 4 flipped `embed_backend` to `local-e5`, `get_embedder()`
    builds a 470MB onnxruntime session and then CPU-embeds every chunk at ~70ms
    apiece. `test_seed.py` alone went from seconds to over three minutes, and the
    full suite from 50s to 10+. That is not a slow test, it is the wrong test:
    nothing in the unit suite is asserting anything about *embedding quality* —
    they need vectors of the right width that behave like vectors.

    The real embedder is exercised where it belongs: `scripts/retrieval_baseline.py`
    (Gate 4.6, against the real library) and the `@pytest.mark.integration` tests —
    which is why this fixture, though `autouse`, STANDS DOWN for anything marked
    `integration`. It did not always: as an unconditional autouse fixture it also
    patched the very tests whose docstrings say they "drive the real embed + chat
    servers", so they asserted against hash vectors and proved nothing about the
    model they existed to exercise. An autouse fake that cannot be escaped does not
    isolate the integration tests, it hollows them out.

    Patched at each CALL SITE, not at `embed_factory`, because every consumer does
    `from app.llm.embed_factory import get_embedder` — rebinding the factory alone
    would leave the already-imported names pointing at the real thing.
    """
    if request.node.get_closest_marker("integration"):
        return
    fake = _FakeEmbedder()
    for module in (
        "app.llm.embed_factory",
        "app.brain.ingest",
        "app.brain.ocr",
        "app.brain.retrieve",
        "app.brain.reembed",
    ):
        monkeypatch.setattr(f"{module}.get_embedder", lambda: fake, raising=False)


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
