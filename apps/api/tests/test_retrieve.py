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
from app.models.knowledge import EMBED_DIM, KnowledgeSource, Chunk, Page

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
    hum-related query — the core ranking guarantee, end to end against the real
    embedder and the real BM25 index.

    Scoped with `source_ids` to the two sources this test seeds. That scoping is
    not incidental: this is a *persistent*, never-torn-down DB shared with every
    other test module (test_ingest.py, test_knowledge_router.py, and re-runs of
    this very test all leave "humbucker"-ish rows behind), so an unscoped top-1
    assertion is order- and history-dependent — test_knowledge_router.py's
    "Uploaded PDF" source ("A humbucker pickup cancels 60-cycle hum.") legitimately
    outranks this test's own longer paragraph when both are visible.

    It used to scope with a per-run-unique `domain` tag. `domain` is GONE from
    `search()` (Plan 13, Stage 4.4), and `source_ids` — the scoping mechanism that
    replaced it — does the same job here without a filter the model could guess.
    """
    db = SessionLocal()
    try:
        hum_source = _seed_source(db, "Pickups 101", _HUM_TEXT)
        delay_source = _seed_source(db, "Delay & Echo Basics", _DELAY_TEXT)

        hits = search(db, "what removes hum?", source_ids=[hum_source.id, delay_source.id])

        assert hits, "expected at least one hit"
        assert all(isinstance(h, Hit) for h in hits)
        top = hits[0]
        assert top.source_id == hum_source.id
        assert top.source_title == "Pickups 101"
        assert "humbucker" in top.text.lower()
        # `score` is the RRF fusion score — the ORDERING key. Non-increasing.
        assert all(a.score >= b.score for a, b in zip(hits, hits[1:]))
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