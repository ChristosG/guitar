"""Integration test for `app.curriculum.generate`: authoring a curriculum tree
over the tutor's WHOLE library, persisted as a Block hierarchy.

UPDATED FOR STAGE 6. Two things changed that this test had to follow:

  * `domain` is gone (it was one dead prompt line and a filter that could no longer
    filter). `brief` — the tutor's own words about what the course is for —
    replaces it, and unlike `domain` it reaches every lesson-draft prompt.
  * `generate_curriculum` NO LONGER DRAFTS LESSON CONTENT. It runs ONE call (the
    outline, over the whole library) and persists the tree with every lesson
    `queued`; the lessons are then written by the fan-out
    (`jobs/curriculum_draft.py`), which is a separate, resumable job. So this
    asserts the SHAPE and the GROUNDING of the outline — the lesson bodies are
    `test_curriculum_grounding.py`'s live tests.

Mirrors test_retrieve.py's DB-skip-guard + fresh-session round-trip pattern. Hits
the real LLM — mark integration.
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
def test_generate_curriculum_builds_a_library_grounded_tree_of_queued_lessons():
    db = SessionLocal()
    try:
        _seed_tone_source(db)

        root_id = generate_curriculum(
            db,
            title="Guitar Tone Basics",
            language="en",
            profile={"level": "intermediate"},
            brief="Teach an intermediate player how pickups, amp and pedals shape his tone.",
            weeks=8,
            minutes_per_session=50,
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

        # THE SHAPE IS ENFORCED, not requested: 8 weekly sessions -> 2 modules x 4
        # lessons. "8 weeks, 3 modules" is no longer representable.
        modules = _children(db2, root_id)
        assert len(modules) == 2, f"expected exactly 2 modules, got {[m.title for m in modules]}"
        assert all(m.kind == "module" for m in modules)
        assert all(m.language == "en" for m in modules)

        all_lessons: list[Block] = []
        for module in modules:
            # The model READ the library and tiered this module itself.
            assert module.meta["tier"] in ("library", "general_knowledge", "web", "gap")
            if module.meta["tier"] == "gap":
                continue   # a gap module is deliberately unfilled
            lessons = _children(db2, module.id)
            assert len(lessons) == 4, f"module {module.title!r} has {len(lessons)} lessons"
            assert all(l.kind == "lesson" for l in lessons)
            assert all(l.language == "en" for l in lessons)
            assert all(l.est_minutes == 50 for l in lessons)
            # QUEUED, not drafted — the lessons are the fan-out's job, and the board
            # opens on this tree instantly.
            assert all(l.meta["draft_status"] == "queued" for l in lessons)
            assert all(not _children(db2, l.id) for l in lessons), (
                "generate_curriculum must not draft segment content — that is the "
                "fan-out's job, and doing it here would put 20 blocking calls on one "
                "job with no progress and no resume"
            )
            all_lessons.extend(lessons)

        # GROUNDING: the outline must talk about the tone material we seeded, not
        # about general guitar teaching. The model was handed the source WHOLE.
        all_nodes = [root, *_descendants(db2, root_id)]
        haystack = " ".join(f"{n.title} {n.body or ''}".lower() for n in all_nodes)
        assert any(k in haystack for k in _TONE_KEYWORDS), (
            f"expected one of {_TONE_KEYWORDS} in the generated outline, got: {haystack[:2000]!r}"
        )
        assert any(m.meta["tier"] == "library" for m in modules), (
            "not one module was tiered 'library' on a topic the seeded source is "
            "ABOUT — that is the false-gap bug this stage exists to remove"
        )

        print("\nGenerated modules:", [(m.title, m.meta["tier"]) for m in modules])
        print("Generated lesson titles:", [l.title for l in all_lessons])
    finally:
        db2.close()
