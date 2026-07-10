"""Unit tests for the MUTATION tool fns in `app.agent.tools.TOOLS` (Plan 5
Task 3). Dispatched through the registry exactly as `loop.py`/Task 4's
resolve step will (`TOOLS[name].fn(db, **args)`), NOT via the HTTP routes.

`run_agent_turn` never calls these fns itself (it suspends every mutation for
approval first — see `test_agent_hitl.py`), and the suspend-spy tests there
deliberately never run the real `fn`. So these are the ONLY tests that
exercise the real mutation logic — the arg names/shapes Task 4 (their first
live caller, inside the human-approval flow) will depend on. An arg-name or
shape drift in `StudentCreate`/`StudentUpdate`/`BlockUpdate`/`segment_block`/
`generate_artifact`/`generate_curriculum` would otherwise surface there, in a
live approval flow, instead of here.

Mirrors `test_agent_tools.py`'s convention exactly: same DB skip-guard +
`setup_module`, ORM-seeded rows via `SessionLocal()` for the five synchronous
DB mutations (create/update_student, segment_block, update_block,
assign_curriculum), and — for the two LLM-backed mutations
(generate_artifact, generate_curriculum) — the SAME provider/service
monkeypatch pattern `search_knowledge`/`explain_concept` use there
(`monkeypatch.setattr(agent_tools, "<service>", fake)`), asserting the
wrapper's shape + dispatch, never a live model call.
"""
import uuid

import pytest
from sqlalchemy import select, text

import app.agent.tools as agent_tools
from app.agent.tools import TOOLS
from app.db import Base, SessionLocal, engine
from app.models.block import Block
from app.models.curriculum import Assignment
from app.models.student import Student

# Skip cleanly (not error) when no DB is reachable — mirrors test_agent_tools.py.
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


def _seed_template_course() -> uuid.UUID:
    """A minimal template course (2 minute-bearing lessons) to segment/assign.
    Returns its root id. Mirrors test_agent_tools.py's own inline ORM seeding.
    """
    db = SessionLocal()
    try:
        root = Block(kind="course", title="Rhythm Basics", language="en", is_template=True)
        db.add(root)
        db.flush()
        db.add(Block(kind="lesson", title="L1", order=0, parent_id=root.id, language="en", est_minutes=20))
        db.add(Block(kind="lesson", title="L2", order=1, parent_id=root.id, language="en", est_minutes=25))
        db.commit()
        return root.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# create_student
# ---------------------------------------------------------------------------

def test_create_student_persists_and_returns_compact_row():
    db = SessionLocal()
    try:
        result = TOOLS["create_student"].fn(
            db, name="New Kid", birthdate="2015-05-05", level="beginner", instrument="guitar",
        )
    finally:
        db.close()

    assert result["name"] == "New Kid"
    assert result["level"] == "beginner"
    assert result["instrument"] == "guitar"
    assert result["preferred_language"] == "el"  # StudentCreate's own default, applied via the wrapper
    assert result["status"] == "active"  # Student model column default
    assert str(result["birthdate"]) == "2015-05-05"  # StudentCreate coerced the string to a date

    # genuinely persisted — a fresh session read-back, not the in-memory object
    db2 = SessionLocal()
    try:
        got = db2.get(Student, result["id"])
        assert got is not None and got.name == "New Kid"
    finally:
        db2.close()


# ---------------------------------------------------------------------------
# update_student
# ---------------------------------------------------------------------------

def test_update_student_applies_only_the_fields_given():
    db = SessionLocal()
    try:
        seed = TOOLS["create_student"].fn(db, name="Alex", level="beginner", instrument="guitar")
        student_id = str(seed["id"])
        # PATCH: change only level — name/instrument must survive untouched.
        result = TOOLS["update_student"].fn(db, student_id=student_id, level="advanced")
    finally:
        db.close()

    assert result["level"] == "advanced"
    assert result["name"] == "Alex"          # untouched
    assert result["instrument"] == "guitar"  # untouched


def test_update_student_malformed_id_returns_graceful_error():
    db = SessionLocal()
    try:
        result = TOOLS["update_student"].fn(db, student_id="not-a-uuid", level="advanced")
    finally:
        db.close()
    assert "error" in result


def test_update_student_unknown_id_returns_graceful_error():
    db = SessionLocal()
    try:
        result = TOOLS["update_student"].fn(db, student_id=str(uuid.uuid4()), level="advanced")
    finally:
        db.close()
    assert result == {"error": "student not found"}


# ---------------------------------------------------------------------------
# segment_block
# ---------------------------------------------------------------------------

def test_segment_block_returns_session_ids():
    root_id = _seed_template_course()
    db = SessionLocal()
    try:
        result = TOOLS["segment_block"].fn(db, block_id=str(root_id), session_minutes=30)
    finally:
        db.close()
    assert "session_ids" in result
    assert len(result["session_ids"]) >= 1


def test_segment_block_malformed_block_id_returns_graceful_error():
    db = SessionLocal()
    try:
        result = TOOLS["segment_block"].fn(db, block_id="not-a-uuid", session_minutes=30)
    finally:
        db.close()
    assert "error" in result


def test_segment_block_unknown_block_id_returns_graceful_error():
    """segment_block raises ValueError('block not found: ...') for a missing
    block; the wrapper catches it into a graceful dict rather than crashing.
    """
    db = SessionLocal()
    try:
        result = TOOLS["segment_block"].fn(db, block_id=str(uuid.uuid4()), session_minutes=30)
    finally:
        db.close()
    assert "error" in result


def test_segment_block_malformed_student_id_returns_graceful_error():
    root_id = _seed_template_course()
    db = SessionLocal()
    try:
        result = TOOLS["segment_block"].fn(
            db, block_id=str(root_id), session_minutes=30, student_id="not-a-uuid",
        )
    finally:
        db.close()
    assert "error" in result


# ---------------------------------------------------------------------------
# update_block
# ---------------------------------------------------------------------------

def test_update_block_updates_title():
    db = SessionLocal()
    try:
        block = Block(kind="lesson", title="Old", language="en", est_minutes=15)
        db.add(block)
        db.commit()
        block_id = str(block.id)
        result = TOOLS["update_block"].fn(db, block_id=block_id, title="New Title")
    finally:
        db.close()
    assert result["title"] == "New Title"
    assert result["est_minutes"] == 15  # untouched (PATCH)


def test_update_block_empty_title_returns_422_style_error():
    """The documented empty-title guard — an empty string satisfies NOT NULL
    but is a useless title, so the wrapper rejects it (the router's own 422,
    surfaced as a graceful dict at this tool layer)."""
    db = SessionLocal()
    try:
        block = Block(kind="lesson", title="Keep me", language="en")
        db.add(block)
        db.commit()
        result = TOOLS["update_block"].fn(db, block_id=str(block.id), title="")
    finally:
        db.close()
    assert result == {"error": "title cannot be empty"}


def test_update_block_malformed_id_returns_graceful_error():
    db = SessionLocal()
    try:
        result = TOOLS["update_block"].fn(db, block_id="not-a-uuid", title="x")
    finally:
        db.close()
    assert "error" in result


def test_update_block_unknown_id_returns_graceful_error():
    db = SessionLocal()
    try:
        result = TOOLS["update_block"].fn(db, block_id=str(uuid.uuid4()), title="x")
    finally:
        db.close()
    assert result == {"error": "block not found"}


# ---------------------------------------------------------------------------
# assign_curriculum
# ---------------------------------------------------------------------------

def test_assign_curriculum_clones_the_tree_and_records_an_assignment():
    root_id = _seed_template_course()
    db = SessionLocal()
    try:
        seed = TOOLS["create_student"].fn(db, name="Assignee")
        student_id = seed["id"]
        result = TOOLS["assign_curriculum"].fn(db, root_id=str(root_id), student_id=str(student_id))
        clone_id = result["id"]

        # the returned tree is the fresh clone (a NEW root), with both lessons
        assert result["title"] == "Rhythm Basics"
        assert clone_id != root_id
        assert len(result["children"]) == 2

        # the clone is a real, student-owned, non-template instance
        clone = db.get(Block, clone_id)
        assert clone.is_template is False
        assert clone.student_id == student_id

        # an Assignment audit row references the TEMPLATE (not the clone)
        assignment = db.scalars(
            select(Assignment).where(Assignment.student_id == student_id)
        ).first()
        assert assignment is not None
        assert assignment.curriculum_block_id == root_id
    finally:
        db.close()


def test_assign_curriculum_malformed_ids_return_graceful_error():
    db = SessionLocal()
    try:
        bad_root = TOOLS["assign_curriculum"].fn(db, root_id="nope", student_id=str(uuid.uuid4()))
        bad_student = TOOLS["assign_curriculum"].fn(db, root_id=str(uuid.uuid4()), student_id="nope")
    finally:
        db.close()
    assert "error" in bad_root
    assert "error" in bad_student


def test_assign_curriculum_unknown_root_and_student_return_graceful_error():
    root_id = _seed_template_course()
    db = SessionLocal()
    try:
        unknown_root = TOOLS["assign_curriculum"].fn(
            db, root_id=str(uuid.uuid4()), student_id=str(uuid.uuid4()),
        )
        # real root, but a student id that doesn't exist
        unknown_student = TOOLS["assign_curriculum"].fn(
            db, root_id=str(root_id), student_id=str(uuid.uuid4()),
        )
    finally:
        db.close()
    assert unknown_root == {"error": "curriculum not found"}
    assert unknown_student == {"error": "student not found"}


# ---------------------------------------------------------------------------
# generate_artifact — LLM-backed: monkeypatch the underlying service, assert
# the wrapper's shape + dispatch (no live model), same pattern search_knowledge
# uses for `search` in test_agent_tools.py.
# ---------------------------------------------------------------------------

class _FakeArtifact:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_generate_artifact_wraps_service_into_compact_dict(monkeypatch):
    captured = {}

    def _fake_generate(db, *, kind, prompt, block_id, ground):
        captured.update(kind=kind, prompt=prompt, block_id=block_id, ground=ground)
        return _FakeArtifact(
            id="art-1", kind=kind, title="G major", spec={"name": "G"}, block_id=block_id,
        )

    monkeypatch.setattr(agent_tools, "_generate_artifact_service", _fake_generate)

    # block_id omitted -> db is never dereferenced (no existence check), so a
    # None db proves the wrapper doesn't touch it on this path.
    result = TOOLS["generate_artifact"].fn(
        None, kind="chord_diagram", prompt="G major open chord", ground=True,
    )

    assert captured == {
        "kind": "chord_diagram", "prompt": "G major open chord",
        "block_id": None, "ground": True,
    }
    assert result == {
        "id": "art-1", "kind": "chord_diagram", "title": "G major",
        "spec": {"name": "G"}, "block_id": None,
    }


def test_generate_artifact_checks_block_exists_before_calling_the_service(monkeypatch):
    called = []

    def _fake_generate(db, **kwargs):
        called.append(kwargs)
        return _FakeArtifact(id="x", kind="tab", title="t", spec={}, block_id=None)

    monkeypatch.setattr(agent_tools, "_generate_artifact_service", _fake_generate)

    db = SessionLocal()
    try:
        # a real, existing block -> the service IS called, with the parsed UUID
        block = Block(kind="lesson", title="Lesson", language="en")
        db.add(block)
        db.commit()
        ok = TOOLS["generate_artifact"].fn(db, kind="tab", prompt="riff", block_id=str(block.id))
        assert called and called[0]["block_id"] == block.id

        # a well-formed but missing block id -> guarded, service NOT called again
        called.clear()
        missing = TOOLS["generate_artifact"].fn(
            db, kind="tab", prompt="riff", block_id=str(uuid.uuid4()),
        )
    finally:
        db.close()

    assert ok["kind"] == "tab"
    assert missing == {"error": "block not found"}
    assert called == []  # the guard short-circuited before the (fake) service


def test_generate_artifact_malformed_block_id_returns_graceful_error(monkeypatch):
    monkeypatch.setattr(
        agent_tools, "_generate_artifact_service",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("service must not be called")),
    )
    # malformed block_id fails to parse before any db/service touch -> None db ok
    result = TOOLS["generate_artifact"].fn(None, kind="tab", prompt="x", block_id="not-a-uuid")
    assert "error" in result


# ---------------------------------------------------------------------------
# generate_curriculum — LLM-backed + async_job=True: same monkeypatch approach;
# assert the wrapper forwards args + shapes the {"root_id": ...} result.
# ---------------------------------------------------------------------------

def test_generate_curriculum_wraps_service_root_id(monkeypatch):
    captured = {}
    fake_root = uuid.uuid4()

    def _fake_generate(db, *, title, language, profile, domain, target_minutes_total):
        captured.update(
            title=title, language=language, profile=profile,
            domain=domain, target_minutes_total=target_minutes_total,
        )
        return fake_root

    monkeypatch.setattr(agent_tools, "_generate_curriculum_service", _fake_generate)

    result = TOOLS["generate_curriculum"].fn(
        None, title="Blues 101", language="en", profile={"level": "beginner"},
        domain="theory", target_minutes_total=120,
    )

    assert captured == {
        "title": "Blues 101", "language": "en", "profile": {"level": "beginner"},
        "domain": "theory", "target_minutes_total": 120,
    }
    assert result == {"root_id": fake_root}
