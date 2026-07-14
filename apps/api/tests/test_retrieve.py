"""Tests for app.brain.retrieve: cross-lingual search() + grounded answer().

The two `@pytest.mark.integration` tests drive the real embed + chat servers
(per this task's brief: seed 2 real sources, ingest them for real via
`ingest_source`, then assert real cosine ranking and a real grounded Greek
answer) — mirrors test_ingest.py's/test_brain_schema.py's DB-skip-guard
pattern. `test_build_grounded_messages_...` is a fast, pure-function unit
test with no DB and no model — it only exercises the prompt-shape logic.
"""
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.brain.ingest import IngestPayload, ingest_source
from app.brain.retrieve import Hit, answer, build_grounded_messages, search
from app.db import Base, SessionLocal, engine
from app.models.knowledge import KnowledgeSource, Chunk, Page

# Skip cleanly (not error) when no DB is reachable — mirrors test_ingest.py.
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


_HUM_TEXT = (
    "A humbucker pickup cancels 60-cycle mains hum by combining two coils "
    "wound in opposite magnetic and electrical polarity: hum picked up "
    "equally by both coils cancels out, while the guitar string signal itself "
    "still adds constructively. This is why humbucker-equipped guitars are "
    "prized in high-gain settings where single coils would buzz audibly."
)

_DELAY_TEXT = (
    "A delay pedal repeats the input signal as one or more discrete echoes. "
    "The time knob sets the gap between repeats and the feedback knob sets "
    "how many repeats occur before decaying to silence. Unlike reverb, which "
    "blurs many reflections together, delay produces distinct, countable echoes."
)


def _seed_source(
    db, title: str, body: str, *, language: str = "en", domain: str | None = None
) -> KnowledgeSource:
    source = KnowledgeSource(type="text", title=title, language=language, domain=domain)
    db.add(source)
    db.commit()
    ingest_source(db, source.id, IngestPayload(kind="text", text=body))
    db.refresh(source)
    assert source.status == "ready", f"seed ingestion failed: {source.status}/{source.error}"
    return source


@pytest.mark.integration
def test_search_ranks_humbucker_chunk_first_for_hum_query():
    """search() must rank the humbucker chunk above the delay chunk for a
    hum-related query — the core cosine-ordering guarantee.

    Scoped to a fresh, per-run-unique `domain` tag shared by only these two
    sources: this is a *persistent*, never-torn-down DB shared with every
    other test module (test_ingest.py, test_knowledge_router.py, and re-runs
    of this very test all leave "humbucker"-ish rows behind), so an unscoped
    top-1 assertion is order- and history-dependent — e.g. test_knowledge_
    router.py's "Uploaded PDF" source ("A humbucker pickup cancels 60-cycle
    hum.") legitimately outscores this test's own longer paragraph for this
    query when both are visible in the same unscoped search. Tagging with a
    uuid4 domain (never reused, unlike a hardcoded literal) makes the ranking
    check deterministic regardless of any other rows the shared DB accumulates.
    """
    db = SessionLocal()
    try:
        domain = f"t5rank-{uuid4().hex[:16]}"  # domain column is String(30); stay well under
        hum_source = _seed_source(db, "Pickups 101", _HUM_TEXT, domain=domain)
        _seed_source(db, "Delay & Echo Basics", _DELAY_TEXT, domain=domain)

        hits = search(db, "what removes hum?", domain=domain)

        assert hits, "expected at least one hit"
        assert all(isinstance(h, Hit) for h in hits)
        top = hits[0]
        assert top.source_id == hum_source.id
        assert top.source_title == "Pickups 101"
        assert "humbucker" in top.text.lower()
        # Scores are non-increasing (top-k ordering, not just "top is right").
        assert all(a.score >= b.score for a, b in zip(hits, hits[1:]))
    finally:
        db.close()


@pytest.mark.integration
def test_search_domain_filter_includes_unclassified_null_domain_sources():
    """Plan 12 CRITICAL bug (this task's report has the full numbers):
    `KnowledgeSource.domain` is essentially unpopulated on the real deployed
    library — 12 of Chris's 16 real sources (95% of his real library's
    characters, including his entire 77-page book) have `domain=NULL`. The
    OLD hard `KnowledgeSource.domain == domain` filter treated NULL as "not
    this domain" and excluded it, so a curriculum interview run with
    `domain="tone"` threw away his entire real library and left every
    module a false "gap".

    Fix: `domain=NULL` means UNCLASSIFIED, not "a different domain" — a
    `domain=` filter must still match it. This test used to assert the OLD
    (wrong) behaviour (an untagged source excluded alongside a genuinely
    off-topic one); it's rewritten here to assert the correct invariant
    instead — see `test_search_domain_filter_excludes_a_source_tagged_a_
    different_domain` immediately below for the "the filter still excludes
    something" half of this same story.

    Generated fresh per run (not a hardcoded literal): a hardcoded domain
    string would collide with itself the second time this suite runs against
    this same persistent DB (that row from the earlier run never gets deleted
    either) — a prior version of this test used a fixed literal and failed
    exactly that way on a second full-suite run.
    """
    db = SessionLocal()
    try:
        domain = f"t12null-{uuid4().hex[:16]}"  # domain column is String(30); stay well under
        null_source = _seed_source(db, "His Real Book (untagged)", _HUM_TEXT, domain=None)
        _seed_source(
            db, "Explicitly Other-Domain Source", _DELAY_TEXT,
            domain=f"t12other-{uuid4().hex[:8]}",
        )

        hits = search(db, "what removes hum?", domain=domain, k=5)

        assert hits, "a NULL-domain (unclassified) source must still be retrievable under a domain filter"
        assert any(h.source_id == null_source.id for h in hits), (
            "domain filter wrongly excluded an unclassified (domain=NULL) source — "
            f"got source_ids: {[h.source_id for h in hits]}"
        )
    finally:
        db.close()


@pytest.mark.integration
def test_search_domain_filter_excludes_a_source_tagged_a_different_domain():
    """The domain filter must still do something useful: it excludes a
    source EXPLICITLY tagged a different domain — only an exact match or an
    unclassified (NULL) source should survive. Without this, "OR domain IS
    NULL" alone would make the filter a no-op for any explicitly-tagged
    off-domain source too.
    """
    db = SessionLocal()
    try:
        domain = f"t12tone-{uuid4().hex[:16]}"  # domain column is String(30); stay well under
        other_domain = f"t12theory-{uuid4().hex[:16]}"
        tagged = _seed_source(db, "Tagged Hum Source", _HUM_TEXT, domain=domain)
        _seed_source(db, "Explicitly Other-Domain Source", _DELAY_TEXT, domain=other_domain)

        hits = search(db, "what removes hum?", domain=domain, k=5)

        assert hits
        assert all(h.source_id == tagged.id for h in hits)
    finally:
        db.close()


@pytest.mark.integration
def test_search_language_filter_scopes_to_matching_sources_only():
    """Mirrors the domain-filter test above for the other optional filter —
    both are implemented the same way (an extra `.where(...)` on the joined
    KnowledgeSource), so this proves the `language` arm independently rather
    than assuming it works because `domain` does.
    """
    db = SessionLocal()
    try:
        lang = f"z{uuid4().hex[:3]}"  # language column is String(5); fake-but-valid-length, unique per run
        tagged = _seed_source(db, "Lang Tagged Hum Source", _HUM_TEXT, language=lang)
        _seed_source(db, "Lang Untagged Delay Source", _DELAY_TEXT, language="en")

        hits = search(db, "what removes hum?", language=lang, k=5)

        assert hits
        assert all(h.source_id == tagged.id for h in hits)
    finally:
        db.close()


@pytest.mark.integration
def test_answer_is_grounded_greek_with_citation():
    db = SessionLocal()
    try:
        _seed_source(db, "Pickups 101 v2", _HUM_TEXT)
        _seed_source(db, "Delay & Echo Basics v2", _DELAY_TEXT)

        result = answer(db, "what is a humbucker?", locale="el")

        assert result.text.strip() != ""
        assert len(result.citations) >= 1
        assert all(isinstance(c, Hit) for c in result.citations)
        # Cheap script-based check that the model actually answered in Greek
        # (U+0370-U+03FF covers the Greek-and-Coptic block) rather than just
        # echoing back the locale code or answering in English.
        assert any("Ͱ" <= ch <= "Ͽ" for ch in result.text), (
            f"expected Greek script in the answer, got: {result.text!r}"
        )
    finally:
        db.close()


def test_build_grounded_messages_has_locale_instruction_and_numbered_context():
    hits = [
        Hit(
            chunk_id="c1", source_id="s1", source_title="Pickups",
            text="hum is cancelled by two coils", section_path=None, page=None, score=0.91,
        ),
        Hit(
            chunk_id="c2", source_id="s1", source_title="Pickups",
            text="single coils buzz more", section_path=None, page=None, score=0.77,
        ),
    ]

    messages = build_grounded_messages("what is a humbucker?", hits, locale="el")

    assert [m["role"] for m in messages] == ["system", "user"]

    system = messages[0]["content"]
    assert "el" in system  # the locale instruction
    assert "Cite sources as [n]" in system

    user = messages[1]["content"]
    assert "what is a humbucker?" in user
    assert "[1] hum is cancelled by two coils" in user  # numbered context, in order
    assert "[2] single coils buzz more" in user


def test_search_returns_chunks_with_and_without_pages(db, monkeypatch):
    """Regression test for outerjoin vs join on Page: chunks with page_id=NULL
    must still be returned (not silently dropped), with page=None / page_id=None.

    If someone ever "tidies" the outerjoin into a plain join, those chunks would
    silently disappear from search results — this test catches that regression.

    Tests two branches:
    - A chunk WITH a page resolves its real page_no and page_id.
    - A chunk WITHOUT a page (page_id=NULL, legacy rows from before Page model
      existed) is STILL RETURNED with page=None and page_id=None.
    """
    from app.config import settings

    # Fake provider: returns a constant zero vector for all queries/documents.
    # The search query embedding doesn't matter (all chunks have the same
    # embedding, so cosine distance is 0 for all); we're testing retrieval
    # cardinality and nullability, not ranking.
    class _ZeroVectorProvider:
        def embed(self, texts, *, is_query=False):
            return [[0.0] * settings.embed_dim for _ in texts]

    monkeypatch.setattr("app.brain.retrieve.get_embedder", lambda: _ZeroVectorProvider())

    # Create a source.
    source = KnowledgeSource(type="text", title="Test Source", language="en")
    db.add(source)
    db.commit()

    # Create a page linked to this source.
    page = Page(source_id=source.id, page_no=42)
    db.add(page)
    db.commit()

    # Create a chunk WITH a page.
    chunk_with_page = Chunk(
        source_id=source.id,
        page_id=page.id,
        text="This chunk has a page",
        section_path=None,
        embedding=[0.0] * settings.embed_dim,
    )
    db.add(chunk_with_page)
    db.commit()

    # Create a chunk WITHOUT a page (legacy, page_id=NULL).
    chunk_without_page = Chunk(
        source_id=source.id,
        page_id=None,  # Explicitly NULL: legacy chunk from before Page model existed.
        text="This chunk has no page",
        section_path=None,
        embedding=[0.0] * settings.embed_dim,
    )
    db.add(chunk_without_page)
    db.commit()

    # Search and verify both chunks are returned.
    hits = search(db, "test query")

    # Both chunks should be in the results (equal distance, both returned by outerjoin).
    assert len(hits) == 2, f"expected 2 hits, got {len(hits)}"
    assert all(isinstance(h, Hit) for h in hits)

    # Find the hits by text to distinguish them (order might vary).
    hit_with_page = next((h for h in hits if h.text == "This chunk has a page"), None)
    hit_without_page = next((h for h in hits if h.text == "This chunk has no page"), None)

    assert hit_with_page is not None, "chunk with page not found"
    assert hit_without_page is not None, "chunk without page not found (regression: outerjoin dropped it)"

    # Verify the chunk WITH a page resolves its page number and page_id.
    assert hit_with_page.page == 42, f"expected page_no=42, got {hit_with_page.page}"
    assert hit_with_page.page_id == page.id, f"expected page_id={page.id}, got {hit_with_page.page_id}"

    # Verify the chunk WITHOUT a page has page=None and page_id=None (not dropped).
    assert hit_without_page.page is None, f"expected page=None, got {hit_without_page.page}"
    assert hit_without_page.page_id is None, f"expected page_id=None, got {hit_without_page.page_id}"
