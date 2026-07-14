"""Regression tests for Plan 9 Task 5 (spec D6 + the Wikimedia 403).

Three real Wikipedia sources sat "ready" in the live DB for two days with
char_count=0 — the UI painted them green and nobody noticed, because
ingest_source unconditionally set status="ready" regardless of char_count.
That lie hid a broken knowledge base and is why the agent had nothing to
retrieve, so it invented answers instead. Two independent causes, pinned here:

1. D6: ingest_source must never call a zero-character source "ready" — it
   must be "empty" instead.
2. The actual root cause of the three zero-char Wikipedia ingests: the URL
   fetch used to send httpx's default `python-httpx/...` User-Agent, which
   Wikimedia bot-blocks with a 403. (Already fixed on this branch in
   app/brain/urlsafe.py's `safe_fetch_html` — `_extract_url` no longer calls
   `httpx.get` directly at all, it goes through that redirect-safe,
   SSRF-guarded fetch, which sets a browser-like UA. This test pins that
   fix against regression at the `extract_text` boundary.)

   Plan 12 Task 1 found that a browser-like UA alone was not enough —
   Wikimedia TLS-fingerprints and blocks `httpx` itself, UA or not (proven:
   an identical UA/headers request via `urllib` succeeds where `httpx`
   gets a bare 403). The transport moved to `app.brain.fetch.fetch_one_hop`
   (urllib-based); this test now monkeypatches that seam instead of
   `httpx.stream`, which no longer sits on this path at all.
"""
from app.brain.ingest import IngestPayload, ingest_source
from app.models.knowledge import EMBED_DIM, KnowledgeSource, Page


class _Provider:
    def embed(self, texts, *, is_query=False):
        return [[0.1] * EMBED_DIM for _ in texts]


def test_zero_character_ingest_is_empty_never_ready(db, monkeypatch):
    """REGRESSION (spec D6). Three real sources sat 'ready' with 0 chars and
    rendered as healthy — that lie hid a broken knowledge base for two days."""
    monkeypatch.setattr("app.brain.ingest.get_embedder", lambda: _Provider())
    src = KnowledgeSource(type="text", title="Nothing", status="ingesting")
    db.add(src)
    db.commit()

    ingest_source(db, src.id, IngestPayload(kind="text", text=""))

    got = db.get(KnowledgeSource, src.id)
    assert got.status == "empty"  # NOT "ready"
    assert got.char_count == 0


def test_real_content_still_becomes_ready_with_a_page(db, monkeypatch):
    monkeypatch.setattr("app.brain.ingest.get_embedder", lambda: _Provider())
    src = KnowledgeSource(type="text", title="Real", status="ingesting")
    db.add(src)
    db.commit()

    ingest_source(db, src.id, IngestPayload(kind="text", text="Humbuckers cancel mains hum by combining two coils wound in opposition."))

    got = db.get(KnowledgeSource, src.id)
    assert got.status == "ready"
    assert got.char_count > 0
    pages = db.query(Page).filter_by(source_id=src.id).all()
    assert len(pages) == 1  # D2: even a text source gets a Page


def test_chunks_carry_a_real_page_id_after_ingest(db, monkeypatch):
    """Task 1 dropped the old Chunk.page int column; until this task wired
    paginate_source into ingest_source, Chunk.page_id was NULL for every
    chunk ever created. Pin that it is now populated for real."""
    from app.models.knowledge import Chunk

    monkeypatch.setattr("app.brain.ingest.get_embedder", lambda: _Provider())
    src = KnowledgeSource(type="text", title="Real", status="ingesting")
    db.add(src)
    db.commit()

    ingest_source(db, src.id, IngestPayload(kind="text", text="Humbuckers cancel mains hum by combining two coils wound in opposition."))

    chunks = db.query(Chunk).filter_by(source_id=src.id).all()
    assert len(chunks) >= 1
    page_ids = {c.page_id for c in chunks}
    assert None not in page_ids
    pages = db.query(Page).filter_by(source_id=src.id).all()
    assert page_ids == {p.id for p in pages}


def test_url_fetch_sends_a_real_user_agent(monkeypatch):
    """Wikimedia 403s a bare/default UA (and, per Plan 12 Task 1, blocks
    httpx's TLS fingerprint outright regardless of UA) — that is why all
    three Wikipedia sources ingested zero characters. `_extract_url` fetches
    through `safe_fetch_html` (app/brain/urlsafe.py), which calls
    `app.brain.fetch.fetch_one_hop(...)` with a browser-like
    `_DEFAULT_HEADERS` User-Agent — pin that here at the `extract_text`
    boundary so this can't silently regress back to the library default.
    """
    from app.brain import extract, fetch as fetch_module
    from app.brain.fetch import FetchResult

    captured = {}

    def fake_fetch_one_hop(url, *, headers, timeout, max_bytes):
        captured["headers"] = headers
        return FetchResult(
            status_code=200,
            headers={"content-type": "text/html"},
            body=b"<html><body><p>Humbuckers cancel 60-cycle hum.</p></body></html>",
        )

    monkeypatch.setattr(fetch_module, "fetch_one_hop", fake_fetch_one_hop)

    sections = extract.extract_text("url", url="https://en.wikipedia.org/wiki/Humbucker")

    ua = captured["headers"]["User-Agent"]
    assert "python-httpx" not in ua.lower()
    assert ua.strip()
    assert sections and sections[0].text.strip()  # the fetch actually produced content
