"""Tests for `app.seed`: the idempotent demo-content seed script (Plan 7 Task 1).

Stubs the three slow/network-touching entry points `seed()` drives
(`generate_curriculum`, `generate_artifact`, and `ingest_source`) with fast
fakes that persist minimal real rows, so this whole module runs in
milliseconds against a real `guitar_test` Postgres with NO live LLM/embed
call — not marked `@pytest.mark.integration`, mirroring `test_jobs_runner.py`'s
own "monkeypatch the slow call, still hit a real DB" rationale.

`create_source` runs for real (per the task brief): its own logic — the
`assert_public_url` SSRF check (a DNS lookup, not a page fetch) plus the
`KnowledgeSource` row create/commit — is fast and side-effect-light on its
own. What makes `POST /sources` slow in production is the extract->chunk->
embed pipeline `create_source` calls via `ingest_source` immediately after
creating the row — so `ingest_source` is stubbed at `app.routers.knowledge.
ingest_source`, i.e. the name binding `create_source` itself resolves (it
was imported there via `from app.brain.ingest import ..., ingest_source`),
NOT at `app.brain.ingest.ingest_source` (patching the origin module would
leave the already-bound import in `app.routers.knowledge` untouched — the
same "patch where it's looked up" rule `test_jobs_runner.py` follows for
`generate_curriculum`).
"""
import pytest
from sqlalchemy import select, text

import app.routers.knowledge as knowledge_router
import app.seed as seed_module
from app.db import Base, SessionLocal, engine
from app.models.artifact import Artifact
from app.models.block import Block
from app.models.curriculum import Assignment
from app.models.knowledge import KnowledgeSource
from app.models.student import Student

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


# ---------------------------------------------------------------------------
# Fast fakes — each persists a minimal but real row, standing in for a live
# LLM/embed call. Signatures mirror the real functions exactly (see module
# docstring) so `seed.py`'s own call sites need no test-only branching.
# ---------------------------------------------------------------------------

def _fake_ingest_source(db, source_id, payload):
    source = db.get(KnowledgeSource, source_id)
    source.status = "ready"
    source.char_count = len(payload.text or payload.url or "")
    db.commit()


def _fake_generate_curriculum(
    db, *, title, language, profile, domain=None, target_minutes_total=None
):
    course = Block(
        kind="course", title=title, order=0, language=language,
        is_template=True, target_profile=profile,
    )
    db.add(course)
    db.flush()
    module = Block(
        kind="module", title=f"{title} — Module 1", order=0,
        parent_id=course.id, language=language,
    )
    db.add(module)
    db.flush()
    db.add(Block(
        kind="lesson", title="Lesson 1", order=0, parent_id=module.id,
        language=language, est_minutes=10,
    ))
    db.commit()
    return course.id


_FAKE_SPECS = {
    "chord_diagram": {"name": "x", "frets": [0, 2, 2, 1, 0, 0], "fingers": [0, 2, 3, 1, 0, 0]},
    "scale_diagram": {"name": "x", "root": "A", "positions": [{"string": 5, "fret": 0}]},
    "tone_recipe": {"guitar": "Stratocaster", "amp": "Fender", "chain": "guitar -> amp"},
    "signal_chain": {"nodes": [{"label": "guitar"}, {"label": "amp"}]},
    "amp_settings": {"dials": [{"label": "gain", "value": 5.0}]},
}


def _fake_generate_artifact(db, *, kind, prompt, block_id=None, ground=False):
    artifact = Artifact(kind=kind, spec=_FAKE_SPECS[kind], title=prompt, block_id=block_id)
    db.add(artifact)
    db.commit()
    return artifact


@pytest.fixture(autouse=True)
def _stub_slow_dependencies(monkeypatch):
    monkeypatch.setattr(seed_module, "generate_curriculum", _fake_generate_curriculum)
    monkeypatch.setattr(seed_module, "generate_artifact", _fake_generate_artifact)
    monkeypatch.setattr(knowledge_router, "ingest_source", _fake_ingest_source)


# ---------------------------------------------------------------------------
# seed() creates the expected rows
# ---------------------------------------------------------------------------

def test_seed_creates_expected_knowledge_sources():
    db = SessionLocal()
    try:
        summary = seed_module.seed(db)

        sources = db.scalars(select(KnowledgeSource)).all()
        titles = {s.title for s in sources}
        assert len(sources) == 4
        assert "Guitar Tone & Gear — Course Spine" in titles
        assert any("Humbucker" in t for t in titles)
        assert any("Distortion" in t for t in titles)
        assert any("amplifier" in t.lower() for t in titles)
        assert "knowledge_sources" in summary["created"]
        assert len(summary["created"]["knowledge_sources"]) == 4
    finally:
        db.close()


def test_seed_creates_expected_curricula():
    db = SessionLocal()
    try:
        summary = seed_module.seed(db)

        courses = db.scalars(select(Block).where(Block.kind == "course")).all()
        course_titles = [c.title for c in courses]
        # 2 templates + 1 clone (the beginner curriculum assigned to Maria).
        assert len(courses) == 3
        assert course_titles.count("Guitar Tone & Gear") == 1
        assert course_titles.count("Zero to Hero — Beginner Guitar") == 2
        assert summary["created"]["curricula"] == [
            "Guitar Tone & Gear", "Zero to Hero — Beginner Guitar",
        ]
    finally:
        db.close()


def test_seed_creates_expected_artifacts():
    db = SessionLocal()
    try:
        summary = seed_module.seed(db)

        artifacts = db.scalars(select(Artifact)).all()
        titles = {a.title for a in artifacts}
        assert len(artifacts) == 8
        for expected in (
            "G major open", "E minor open", "C major open", "D major open",
            "A minor pentatonic", "Stevie Ray Vaughan Texas Flood",
            "Classic pedalboard order", "Fender clean",
        ):
            assert expected in titles
        assert len(summary["created"]["artifacts"]) == 8
    finally:
        db.close()


def test_seed_creates_student_and_assignment():
    db = SessionLocal()
    try:
        summary = seed_module.seed(db)

        student = db.scalars(select(Student).where(Student.name == "Maria Ioannou")).first()
        assert student is not None
        assert student.level == "beginner"
        assert student.preferred_language == "el"

        assignments = db.scalars(
            select(Assignment).where(Assignment.student_id == student.id)
        ).all()
        assert len(assignments) == 1

        assert summary["created"]["student"] == "Maria Ioannou"
        assert "assignment" in summary["created"]
    finally:
        db.close()


def test_seed_first_call_reports_no_skips():
    db = SessionLocal()
    try:
        summary = seed_module.seed(db)
        assert summary["skipped"] == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Idempotency: a second seed(db) call changes nothing.
# ---------------------------------------------------------------------------

def _counts(db) -> dict:
    return {
        "sources": db.query(KnowledgeSource).count(),
        "courses": db.query(Block).filter(Block.kind == "course").count(),
        "blocks": db.query(Block).count(),
        "artifacts": db.query(Artifact).count(),
        "students": db.query(Student).count(),
        "assignments": db.query(Assignment).count(),
    }


def test_seed_second_call_is_idempotent():
    db = SessionLocal()
    try:
        seed_module.seed(db)
        before = _counts(db)

        summary2 = seed_module.seed(db)

        after = _counts(db)
        assert after == before
        assert summary2["created"] == {}
        assert len(summary2["skipped"]) >= 1 + 4 + 2 + 8  # student + sources + curricula + artifacts
    finally:
        db.close()


def test_seed_second_call_skips_every_known_title():
    db = SessionLocal()
    try:
        seed_module.seed(db)
        summary2 = seed_module.seed(db)

        skipped_text = " | ".join(summary2["skipped"])
        assert "Maria Ioannou" in skipped_text
        assert "Guitar Tone & Gear" in skipped_text
        assert "Zero to Hero" in skipped_text
        assert "G major open" in skipped_text
    finally:
        db.close()


# ---------------------------------------------------------------------------
# fresh=True: wipes prior seeded + legacy demo-titled rows, then reseeds clean.
# ---------------------------------------------------------------------------

def test_seed_fresh_wipes_and_recreates_seed_rows():
    db = SessionLocal()
    try:
        seed_module.seed(db)
        before = _counts(db)
        assert before["courses"] == 3

        summary = seed_module.seed(db, fresh=True)

        after = _counts(db)
        assert after == before  # wiped, then fully rebuilt to the same shape
        assert summary["created"]["curricula"] == [
            "Guitar Tone & Gear", "Zero to Hero — Beginner Guitar",
        ]
        assert summary["created"]["student"] == "Maria Ioannou"
    finally:
        db.close()


def test_seed_fresh_deletes_legacy_demo_titled_curricula():
    """`fresh=True` also sweeps the accumulated throwaway curricula titles
    from earlier manual live-testing sessions (not created by this script),
    named explicitly in the task brief — e.g. "Stack Health Check".
    """
    db = SessionLocal()
    try:
        legacy_root = Block(kind="course", title="Stack Health Check", order=0, is_template=True)
        db.add(legacy_root)
        db.flush()
        legacy_artifact = Artifact(
            kind="chord_diagram", title="stray", spec=_FAKE_SPECS["chord_diagram"],
            block_id=legacy_root.id,
        )
        db.add(legacy_artifact)
        db.commit()
        legacy_root_id = legacy_root.id
        legacy_artifact_id = legacy_artifact.id

        seed_module.seed(db, fresh=True)

        assert db.get(Block, legacy_root_id) is None
        assert db.get(Artifact, legacy_artifact_id) is None
    finally:
        db.close()
