"""Tests for `app/brain/repair.py` — healing a `status="ready"` source that
has zero `Page` rows (the "Guitar Tone & Gear — Course Spine" live bug: it
was ingested in Plan 7, before the Page model existed, so no Page was ever
created for it, and the Reader 404s trying to open it).

Only hits the DB (via the `db` fixture) — the embed provider is monkeypatched
throughout (`_Provider`, same shape as `tests/test_ingest_status.py`), so
none of this needs a live embed server and is safe to run outside
`-m integration`.
"""
import pytest
from sqlalchemy import text

from app.brain.chunk import chunk_sections
from app.brain.extract import Section
from app.brain.repair import (
    order_chunk_texts,
    reassemble_chunk_texts,
    repair_pageless_source,
)
from app.config import settings
from app.db import Base, engine
from app.models.knowledge import Chunk, KnowledgeSource, Page

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )


def setup_module(_):
    Base.metadata.create_all(engine)


class _Provider:
    def embed(self, texts, *, is_query=False):
        return [[0.1] * settings.embed_dim for _ in texts]


# A long document made of ~12,000 UNIQUELY-numbered tokens — long enough
# that chunk_sections(), with its real defaults (target_chars=1200,
# overlap_chars=150), produces several overlapping chunks, not just one.
# Every token is distinct so an overlap match can never be confused with an
# unrelated repeated phrase elsewhere in the text (a real prose fixture with
# repeated boilerplate wording — e.g. the same trailing clause in every
# paragraph — was tried first and produced exactly that ambiguity: multiple
# chunks shared a >20-char substring by coincidence, order_chunk_texts
# correctly refused to guess, and reassembly failed. Distinct tokens make
# every genuine chunk_sections overlap the ONLY possible match.)
_LONG_DOC = " ".join(f"word{i:05d}" for i in range(1200))


def _real_chunks_for(text_body: str) -> list[str]:
    drafts = chunk_sections([Section(heading=None, text=text_body, page=None)])
    return [d.text for d in drafts]


# --- order_chunk_texts / reassemble_chunk_texts (pure, no DB) ---------------

def test_real_chunker_output_has_more_than_one_chunk_for_the_long_doc():
    """Sanity check on the fixture itself: if this ever shrinks to 1 chunk,
    the tests below stop exercising the actual overlap-reconstruction logic."""
    chunks = _real_chunks_for(_LONG_DOC)
    assert len(chunks) > 3


def test_order_chunk_texts_recovers_original_order_from_a_shuffled_list():
    chunks = _real_chunks_for(_LONG_DOC)
    shuffled = list(reversed(chunks))  # deterministic "wrong" order

    recovered = order_chunk_texts(shuffled)

    assert recovered == chunks


def test_reassemble_chunk_texts_reconstructs_the_exact_original_text():
    chunks = _real_chunks_for(_LONG_DOC)

    rebuilt = reassemble_chunk_texts(chunks)

    assert rebuilt == _LONG_DOC  # byte-for-byte — not one character lost or added


def test_order_then_reassemble_from_a_shuffled_list_still_recovers_the_original():
    """The end-to-end shape `repair_pageless_source` actually relies on:
    chunks read back from the DB in whatever (untrustworthy) order, ordered,
    then reassembled."""
    chunks = _real_chunks_for(_LONG_DOC)
    # A deterministic, length-agnostic "wrong" order: rotate by one and swap
    # the first pair — works regardless of how many chunks the fixture yields.
    rotated = chunks[1:] + chunks[:1]
    shuffled = [rotated[1], rotated[0], *rotated[2:]]

    rebuilt = reassemble_chunk_texts(order_chunk_texts(shuffled))

    assert rebuilt == _LONG_DOC


def test_single_chunk_round_trips_unchanged():
    assert order_chunk_texts(["just one chunk"]) == ["just one chunk"]
    assert reassemble_chunk_texts(["just one chunk"]) == "just one chunk"


def test_no_chunks_reassembles_to_empty_string():
    assert reassemble_chunk_texts([]) == ""


def test_order_chunk_texts_falls_back_to_input_order_for_unrelated_chunks():
    """Chunks with no real overlap at all (e.g. from different documents)
    can't be chained — must not fabricate a fake order, just hand back
    what was given, so callers can still make a best-effort decision."""
    unrelated = ["Completely unrelated sentence A.", "Totally different sentence B."]

    assert order_chunk_texts(unrelated) == unrelated


# --- repair_pageless_source (DB-backed) -------------------------------------

def test_repair_pageless_text_source_reassembles_chunks_into_a_page(db, monkeypatch):
    """The exact live bug: a `type="text"` source sitting at status="ready"
    with real Chunks but zero Pages (legacy, pre-Page-model data)."""
    monkeypatch.setattr("app.brain.ingest.get_provider", lambda: _Provider())
    src = KnowledgeSource(type="text", title="Course Spine", status="ready",
                          char_count=len(_LONG_DOC))
    db.add(src); db.commit()

    for chunk_text in _real_chunks_for(_LONG_DOC):
        db.add(Chunk(source_id=src.id, text=chunk_text, embedding=[0.0] * settings.embed_dim))
    db.commit()

    assert db.query(Page).filter_by(source_id=src.id).count() == 0  # reproduces the bug

    healed = repair_pageless_source(db, src)

    assert healed is True
    db.expire_all()
    pages = db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()
    assert len(pages) >= 1
    assert "".join(p.text or "" for p in pages) == _LONG_DOC  # no chars lost
    got = db.get(KnowledgeSource, src.id)
    assert got.status == "ready"
    assert got.char_count > 0


def test_repair_pageless_text_source_does_not_duplicate_the_old_orphaned_chunks(db, monkeypatch):
    """Regression (found live, repairing the real "Course Spine" row): the
    pre-repair Chunks all have `page_id IS NULL` (no Page existed yet to
    link to) — `paginate_source`'s own replace-on-reingest cleanup only
    cascades away Chunks attached to a Page it deletes, so with ZERO Pages
    there is nothing for that cascade to key off, and a naive repair leaves
    the old orphaned Chunks sitting forever alongside the freshly-ingested
    ones, silently doubling the source's retrieval index."""
    monkeypatch.setattr("app.brain.ingest.get_provider", lambda: _Provider())
    src = KnowledgeSource(type="text", title="Course Spine", status="ready",
                          char_count=len(_LONG_DOC))
    db.add(src); db.commit()
    old_chunk_count = len(_real_chunks_for(_LONG_DOC))
    for chunk_text in _real_chunks_for(_LONG_DOC):
        db.add(Chunk(source_id=src.id, text=chunk_text, embedding=[0.0] * settings.embed_dim))
    db.commit()

    repair_pageless_source(db, src)

    db.expire_all()
    remaining = db.query(Chunk).filter_by(source_id=src.id).all()
    assert len(remaining) < old_chunk_count * 2  # not "old + new" stacked
    assert all(c.page_id is not None for c in remaining)  # no orphans survive


def test_repair_pageless_source_is_a_noop_with_no_chunks_and_no_url(db):
    """Unreachable for a genuine "ready" source per spec D6 (ready implies
    char_count > 0 implies chunks were persisted) — but must stay a
    documented no-op, not fabricate a Page out of nothing, if it somehow
    occurs (a directly-constructed test fixture, here)."""
    src = KnowledgeSource(type="text", title="Impossible per D6", status="ready")
    db.add(src); db.commit()

    healed = repair_pageless_source(db, src)

    assert healed is False
    assert db.query(Page).filter_by(source_id=src.id).count() == 0


def test_repair_pageless_url_source_refetches_its_stored_url_instead_of_reassembling(db, monkeypatch):
    """A `type="url"` source has a real original to go back to
    (`KnowledgeSource.url`, Plan 9 Task 1) — exactly what `POST .../retry`
    already does for a *failed* url source. Repair must prefer that over
    reassembling from (possibly stale/partial) chunks, even when some
    chunks happen to be present."""
    monkeypatch.setattr("app.brain.ingest.get_provider", lambda: _Provider())

    captured = {}

    def _fake_extract(kind, **kwargs):
        captured["kind"] = kind
        captured["url"] = kwargs.get("url")
        return [Section(heading=None, text="Freshly refetched tone tips.", page=1)]

    monkeypatch.setattr("app.brain.paginate.extract_text", _fake_extract)

    src = KnowledgeSource(type="url", title="Tone Tips", status="ready",
                          url="https://example.com/tone-tips")
    db.add(src); db.commit()
    # A stale chunk from some prior (buggy) state — must be ignored, not reassembled.
    db.add(Chunk(source_id=src.id, text="stale leftover text", embedding=[0.0] * settings.embed_dim))
    db.commit()

    healed = repair_pageless_source(db, src)

    assert healed is True
    assert captured["kind"] == "url"
    assert captured["url"] == "https://example.com/tone-tips"
    db.expire_all()
    pages = db.query(Page).filter_by(source_id=src.id).all()
    assert len(pages) == 1
    assert pages[0].text == "Freshly refetched tone tips."
    chunks = db.query(Chunk).filter_by(source_id=src.id).all()
    assert not any(c.text == "stale leftover text" for c in chunks)  # orphan not carried forward
