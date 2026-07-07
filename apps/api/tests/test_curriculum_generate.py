"""Integration test for app.curriculum.generate: Brain-grounded, guided-JSON
curriculum tree generation, persisted as a Block hierarchy.

Mirrors test_retrieve.py's/test_ingest.py's DB-skip-guard + fresh-session
round-trip pattern (test_curriculum_schema.py's pattern too, for the Block
tree read-back). Hits the real LLM (guided_json) and, transitively via Brain
search(), the real embed server — mark integration.
"""
import pytest
from sqlalchemy import select, text

from app.brain.ingest import IngestPayload, ingest_source
from app.curriculum.generate import generate_curriculum
from app.db import Base, SessionLocal, engine
from app.models.block import Block
from app.models.knowledge import KnowledgeSource

# Skip cleanly (not error) when no DB is reachable — mirrors test_retrieve.py.
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


_TONE_TEXT = (
    "Guitar tone starts at the pickups: single-coil pickups sound bright and "
    "articulate, while humbucker pickups sound thicker and quieter, with less "
    "hum. From the guitar the signal usually hits a pedalboard: an overdrive "
    "or distortion pedal adds gain and harmonic saturation, a compressor "
    "evens out picking dynamics, and a delay or reverb pedal adds space. The "
    "amplifier is the last and biggest tone-shaping stage: a tube amp driven "
    "into natural power-tube breakup sounds warmer and more dynamic than a "
    "clean solid-state amp with gain added only by a pedal. Turning up the "
    "amp's gain knob increases distortion; turning up the master volume "
    "increases loudness without adding more gain."
)

_TONE_KEYWORDS = ("amp", "pedal", "pickup", "gain", "tube")


def _seed_tone_source(db) -> KnowledgeSource:
    source = KnowledgeSource(type="text", title="Tone Basics", language="en", domain="tone")
    db.add(source)
    db.commit()
    ingest_source(db, source.id, IngestPayload(kind="text", text=_TONE_TEXT))
    db.refresh(source)
    assert source.status == "ready", f"seed ingestion failed: {source.status}/{source.error}"
    return source


def _children(db, parent_id) -> list[Block]:
    return db.scalars(
        select(Block).where(Block.parent_id == parent_id).order_by(Block.order)
    ).all()


def _descendants(db, root_id) -> list[Block]:
    """BFS: every Block transitively parented under root_id (not including it)."""
    out: list[Block] = []
    frontier = [root_id]
    while frontier:
        rows = db.scalars(select(Block).where(Block.parent_id.in_(frontier))).all()
        out.extend(rows)
        frontier = [r.id for r in rows]
    return out


@pytest.mark.integration
def test_generate_curriculum_builds_brain_grounded_tree():
    db = SessionLocal()
    try:
        _seed_tone_source(db)

        root_id = generate_curriculum(
            db,
            title="Guitar Tone Basics",
            language="en",
            profile={"level": "intermediate"},
            domain="tone",
            target_minutes_total=1200,
        )
    finally:
        db.close()

    # Fresh session: a genuine DB round-trip, not the identity-map object
    # reused under expire_on_commit=False (mirrors test_curriculum_schema.py).
    db2 = SessionLocal()
    try:
        root = db2.get(Block, root_id)
        assert root is not None
        assert root.kind == "course"
        assert root.is_template is True
        assert root.language == "en"

        modules = _children(db2, root_id)
        assert len(modules) >= 2, f"expected >=2 modules, got {[m.title for m in modules]}"
        assert all(m.kind == "module" for m in modules)
        assert all(m.language == "en" for m in modules)  # language propagated

        all_lessons: list[Block] = []
        for module in modules:
            lessons = _children(db2, module.id)
            assert len(lessons) >= 1, f"module {module.title!r} has no lessons"
            assert all(l.kind == "lesson" for l in lessons)
            assert all(l.language == "en" for l in lessons)
            assert all(l.est_minutes is not None and l.est_minutes > 0 for l in lessons), (
                f"module {module.title!r} lesson est_minutes: "
                f"{[(l.title, l.est_minutes) for l in lessons]}"
            )
            all_lessons.extend(lessons)

        # At least one node anywhere in the tree references a real tone
        # concept from the seeded CONTEXT (not invented) — proves grounding,
        # not just schema-shape compliance. `_descendants` already covers
        # modules/lessons/segments; `root` is the one node it excludes.
        all_nodes = [root, *_descendants(db2, root_id)]
        haystack = " ".join(f"{n.title} {n.body or ''}".lower() for n in all_nodes)
        assert any(k in haystack for k in _TONE_KEYWORDS), (
            f"expected one of {_TONE_KEYWORDS} in generated tree text, got: {haystack[:2000]!r}"
        )

        print("\nGenerated module titles:", [m.title for m in modules])
        print("Generated lesson titles:", [l.title for l in all_lessons])
    finally:
        db2.close()
