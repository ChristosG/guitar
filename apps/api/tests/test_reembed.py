"""`app.brain.reembed` — the deploy-time half of the embedding migration, plus the
lifespan BM25 warm-up (Plan 13, Stage 4.2/4.3).

The property under test is mostly a NEGATIVE one, and it is the whole reason this
script exists rather than a re-ingest: re-embedding must touch the `embedding`
column and NOTHING ELSE. `chunk.text` and `chunk.section_path` are the only place
the heading-aware extracted text lives — `Page.text` is populated only
opportunistically, so rebuilding the corpus from Pages would silently EMPTY any
PDF whose vision-OCR never ran, and drop `section_path` corpus-wide.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

from app.brain import lexical
from app.brain.reembed import reembed_all
from app.db import Base, SessionLocal, engine
from app.models.knowledge import EMBED_DIM, Chunk, KnowledgeSource

try:
    with engine.connect() as _c:
        _c.execute(sql_text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)

from app.main import app


def setup_module(_):
    Base.metadata.create_all(engine)


_MODEL = "stub-embedder/v1"


class _StubEmbedder:
    def __init__(self):
        self.calls = 0

    dim = EMBED_DIM
    model_id = _MODEL

    def embed(self, texts, *, is_query=False):
        self.calls += len(texts)
        # A vector derived from the text, so "was this row actually re-embedded?"
        # is an observable question rather than an act of faith.
        #
        # The `+ 1` is load-bearing: zero is the migration's sentinel for "never
        # embedded" (a3c7e1b90d42 zero-fills the column), so a stub that can EMIT
        # a zero vector makes the sentinel ambiguous. Without it a 28-char chunk
        # hits `28 % 7 == 0`, re-embeds correctly to all-zeros, and is then
        # indistinguishable from a row the script skipped — the test failed on a
        # row that was in fact written. Range is now 0.1-0.7, never 0.0.
        return [[float(len(t) % 7 + 1) / 10.0] * EMBED_DIM for t in texts]


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    stub = _StubEmbedder()
    monkeypatch.setattr("app.brain.reembed.get_embedder", lambda: stub)
    lexical.reset_index()
    yield stub
    lexical.reset_index()


def _seed(db) -> KnowledgeSource:
    """A source whose chunks carry the ZERO vectors the migration leaves behind."""
    source = KnowledgeSource(type="pdf", title="His Real Book", language="en")
    db.add(source)
    db.commit()
    for i, body in enumerate(
        ["Humbuckers cancel mains hum.", "Tube Screamers hump the midrange.", "Amp gain staging."]
    ):
        db.add(
            Chunk(
                source_id=source.id, text=body, section_path=f"Chapter {i}",
                embedding=[0.0] * EMBED_DIM,     # what a3c7e1b90d42 leaves behind
            )
        )
    db.commit()
    return source


def test_reembed_rewrites_every_zero_vector_from_the_chunks_own_text(db, _stub):
    source = _seed(db)

    summary = reembed_all(db)

    assert summary["chunks"] == 3
    assert summary["embed_model"] == _MODEL
    assert _stub.calls == 3
    chunks = db.scalars(sql_select_chunks(source)).all()
    for chunk in chunks:
        expected = [float(len(chunk.text) % 7 + 1) / 10.0] * EMBED_DIM
        assert [round(x, 4) for x in chunk.embedding] == [round(x, 4) for x in expected]
        assert any(x != 0.0 for x in chunk.embedding), "a chunk was left zero-filled"


def test_reembed_never_deletes_a_row_or_touches_text_or_section_path(db):
    """THE ANTI-TRUNCATE PROOF. Verified the same way against a copy of the real
    408-chunk library: 408 before, 408 after, `section_path` untouched."""
    source = _seed(db)
    before = {(c.id, c.text, c.section_path) for c in db.scalars(sql_select_chunks(source))}

    reembed_all(db)

    after = {(c.id, c.text, c.section_path) for c in db.scalars(sql_select_chunks(source))}
    assert after == before


def test_reembed_stamps_the_model_so_a_future_swap_can_find_stale_rows(db):
    source = _seed(db)
    assert source.embed_model is None

    reembed_all(db)

    db.refresh(source)
    assert source.embed_model == _MODEL


def test_only_stale_skips_a_source_already_on_the_current_model(db, _stub):
    """What makes a resumed run after a crash cheap."""
    _seed(db)
    reembed_all(db)
    _stub.calls = 0

    summary = reembed_all(db, only_stale=True)

    assert _stub.calls == 0
    assert summary["chunks"] == 0
    assert summary["sources_skipped"] == 1


def test_reembed_is_idempotent(db):
    source = _seed(db)
    reembed_all(db)
    first = {c.id: list(c.embedding) for c in db.scalars(sql_select_chunks(source))}

    reembed_all(db)

    second = {c.id: list(c.embedding) for c in db.scalars(sql_select_chunks(source))}
    assert first.keys() == second.keys()
    for cid in first:
        assert [round(x, 5) for x in first[cid]] == [round(x, 5) for x in second[cid]]


def test_reindex_route_runs_the_same_code(db):
    """`POST /knowledge/reindex` exists because the end state is a bundle on the
    tutor's iMac, where "run this script inside the container" is not an
    instruction anybody is going to follow."""
    _seed(db)

    r = TestClient(app).post("/knowledge/reindex")

    assert r.status_code == 200, r.text
    assert r.json()["chunks"] == 3
    assert r.json()["embed_model"] == _MODEL


def test_the_lifespan_warms_the_bm25_index(db):
    """Built lazily, the index would be built inside whichever request searched
    first — putting a full corpus scan on the tutor's FIRST question and nowhere
    else. `TestClient` as a CONTEXT MANAGER is what runs ASGI startup (a bare
    `TestClient(app)` does not — see test_main_lifespan.py).
    """
    _seed(db)
    lexical.reset_index()

    with TestClient(app):
        pass

    assert lexical._index is not None, "lifespan did not warm the BM25 index"
    assert lexical._index.n_docs == 3


def sql_select_chunks(source):
    from sqlalchemy import select

    return select(Chunk).where(Chunk.source_id == source.id).order_by(Chunk.id)
