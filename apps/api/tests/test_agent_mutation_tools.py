"""Unit tests for the MUTATION tool fns in `app.agent.tools.TOOLS` (Plan 5
Task 3; Plan 6 Task 6 adds three more into this same suite — see that
section below). Dispatched through the registry exactly as `loop.py`/Task 4's
resolve step will (`TOOLS[name].fn(db, **args)`), NOT via the HTTP routes.

`run_agent_turn` never calls these fns itself (it suspends every mutation for
approval first — see `test_agent_hitl.py`), and the suspend-spy tests there
deliberately never run the real `fn`. So these are the ONLY tests that
exercise the real mutation logic — the arg names/shapes Task 4 (their first
live caller, inside the human-approval flow) will depend on. An arg-name or
shape drift in `StudentCreate`/`StudentUpdate`/`BlockUpdate`/`segment_block`/
`generate_artifact`/`generate_curriculum`/`NoteCreate`/`upsert_progress`/
`promote_note` would otherwise surface there, in a live approval flow,
instead of here.

Mirrors `test_agent_tools.py`'s convention exactly: same DB skip-guard +
`setup_module`, ORM-seeded rows via `SessionLocal()` for the synchronous DB
mutations (create/update_student, segment_block, update_block,
assign_curriculum, add_note, log_progress), and — for the LLM-backed
mutations (generate_artifact, generate_curriculum) — the SAME provider/
service monkeypatch pattern `search_knowledge`/`explain_concept` use there
(`monkeypatch.setattr(agent_tools, "<service>", fake)`), asserting the
wrapper's shape + dispatch, never a live model call. `promote_note_to_
knowledge` (Plan 6 Task 6) reuses a THIRD pattern instead — `test_notes_api.
py`'s "stub `ingest_source` at `app.routers.knowledge`, the name binding
`create_source` (and so `promote_note`) itself resolves" rule, since that
call chain goes through the real `create_source`/`promote_note` service code,
not a monkeypatched service function.
"""
import uuid

import pytest
from sqlalchemy import select, text

import app.agent.tools as agent_tools
import app.routers.knowledge as knowledge_router
from app.agent.tools import TOOLS
from app.db import Base, SessionLocal, engine
from app.models.block import Block
from app.models.curriculum import Assignment, Progress
from app.models.knowledge import KnowledgeSource
from app.models.note import Note
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


# ---------------------------------------------------------------------------
# add_note (Plan 6 Task 6) — real DB row, same "genuinely persisted, fresh
# session read-back" precedent as test_create_student_persists_and_returns_
# compact_row above.
# ---------------------------------------------------------------------------

def test_add_note_persists_and_returns_compact_row():
    db = SessionLocal()
    try:
        result = TOOLS["add_note"].fn(
            db, title="Barre chords", body="Maria struggled with barre chords today",
            tags=["technique"],
        )
    finally:
        db.close()

    assert result == {"note_id": result["note_id"], "title": "Barre chords"}

    db2 = SessionLocal()
    try:
        got = db2.get(Note, result["note_id"])
        assert got is not None
        assert got.body == "Maria struggled with barre chords today"
        assert got.tags == ["technique"]
        assert got.student_id is None
        assert got.promoted_to_knowledge is False
    finally:
        db2.close()


def test_add_note_defaults_tags_to_empty_list_when_omitted():
    db = SessionLocal()
    try:
        result = TOOLS["add_note"].fn(db, title="No tags", body="text")
        note = db.get(Note, result["note_id"])
        assert note.tags == []
    finally:
        db.close()


def test_add_note_links_to_a_valid_student():
    db = SessionLocal()
    try:
        student = TOOLS["create_student"].fn(db, name="Maria Ioannou")
        result = TOOLS["add_note"].fn(
            db, title="Progress", body="doing great", student_id=str(student["id"]),
        )
        note = db.get(Note, result["note_id"])
        assert note.student_id == student["id"]
    finally:
        db.close()


def test_add_note_malformed_student_id_returns_graceful_error():
    db = SessionLocal()
    try:
        result = TOOLS["add_note"].fn(db, title="x", body="y", student_id="not-a-uuid")
    finally:
        db.close()
    assert "error" in result


def test_add_note_unknown_student_id_returns_graceful_error():
    db = SessionLocal()
    try:
        result = TOOLS["add_note"].fn(db, title="x", body="y", student_id=str(uuid.uuid4()))
    finally:
        db.close()
    assert result == {"error": "student not found"}


def test_add_note_empty_title_returns_graceful_error():
    """Mirrors `routers/notes.py`'s own empty-title guard — see `test_notes_
    api.py`'s `test_create_note_rejects_empty_string_title` for the HTTP-layer
    equivalent of this same rule."""
    db = SessionLocal()
    try:
        result = TOOLS["add_note"].fn(db, title="", body="y")
    finally:
        db.close()
    assert result == {"error": "title cannot be empty"}


# ---------------------------------------------------------------------------
# promote_note_to_knowledge (Plan 6 Task 6) — stubs `ingest_source` at
# `app.routers.knowledge` (the name binding `create_source`/`promote_note`
# themselves resolve, NOT `app.brain.ingest.ingest_source` — patching the
# origin module would leave that already-bound import untouched), same
# "patch where it's looked up" rule `test_notes_api.py`/`test_seed.py` follow
# for the identical call chain (`promote_note` -> `create_source` ->
# `ingest_source`).
# ---------------------------------------------------------------------------

def _fake_ingest_source(db, source_id, payload):
    """Mirrors test_notes_api.py's own fake: stands in for the real extract->
    chunk->embed pipeline with a fast, schema-plausible status flip."""
    source = db.get(KnowledgeSource, source_id)
    source.status = "ready"
    source.char_count = len(payload.text or payload.url or "")
    db.commit()


def test_promote_note_to_knowledge_creates_source_and_flips_flag(monkeypatch):
    monkeypatch.setattr(knowledge_router, "ingest_source", _fake_ingest_source)

    db = SessionLocal()
    try:
        note = Note(title="Tone tip", body="Bridge pickup + heavy strings for extra bite.")
        db.add(note)
        db.commit()
        note_id = note.id

        result = TOOLS["promote_note_to_knowledge"].fn(db, note_id=str(note_id))
    finally:
        db.close()

    assert result["note_id"] == note_id
    assert result["title"] == "Tone tip"
    assert "source_id" in result

    db2 = SessionLocal()
    try:
        got = db2.get(Note, note_id)
        assert got.promoted_to_knowledge is True
        source = db2.get(KnowledgeSource, result["source_id"])
        assert source is not None
        assert source.title == "Tone tip"
        assert source.type == "text"
        assert source.status == "ready"  # from the stubbed ingest_source
    finally:
        db2.close()


def test_promote_note_to_knowledge_malformed_id_returns_graceful_error():
    db = SessionLocal()
    try:
        result = TOOLS["promote_note_to_knowledge"].fn(db, note_id="not-a-uuid")
    finally:
        db.close()
    assert "error" in result


def test_promote_note_to_knowledge_unknown_id_returns_graceful_error():
    db = SessionLocal()
    try:
        result = TOOLS["promote_note_to_knowledge"].fn(db, note_id=str(uuid.uuid4()))
    finally:
        db.close()
    assert result == {"error": "note not found"}


def test_promote_note_to_knowledge_already_promoted_returns_graceful_error_and_no_second_source(monkeypatch):
    monkeypatch.setattr(knowledge_router, "ingest_source", _fake_ingest_source)

    db = SessionLocal()
    try:
        note = Note(title="x", body="y")
        db.add(note)
        db.commit()
        note_id = note.id

        first = TOOLS["promote_note_to_knowledge"].fn(db, note_id=str(note_id))
        assert "source_id" in first
        count_before = len(db.scalars(select(KnowledgeSource)).all())

        second = TOOLS["promote_note_to_knowledge"].fn(db, note_id=str(note_id))
        count_after = len(db.scalars(select(KnowledgeSource)).all())
    finally:
        db.close()

    assert second == {"error": "note already promoted to knowledge"}
    assert count_after == count_before  # no duplicate source created


def test_promote_note_to_knowledge_empty_body_returns_graceful_error():
    db = SessionLocal()
    try:
        note = Note(title="Empty", body="")
        db.add(note)
        db.commit()
        result = TOOLS["promote_note_to_knowledge"].fn(db, note_id=str(note.id))
    finally:
        db.close()
    assert result == {"error": "cannot promote a note with an empty body"}


def test_promote_note_to_knowledge_whitespace_only_body_returns_graceful_error():
    db = SessionLocal()
    try:
        note = Note(title="Whitespace", body="   \n\t  ")
        db.add(note)
        db.commit()
        result = TOOLS["promote_note_to_knowledge"].fn(db, note_id=str(note.id))
    finally:
        db.close()
    assert result == {"error": "cannot promote a note with an empty body"}


# ---------------------------------------------------------------------------
# log_progress (Plan 6 Task 6) — wraps app.curriculum.progress.upsert_progress;
# same "create then update same block = one row, new status" upsert precedent
# test_students_api.py's test_upsert_progress_second_call_updates_same_row_
# not_duplicate already established at the HTTP layer.
# ---------------------------------------------------------------------------

def test_log_progress_creates_new_row():
    root_id = _seed_template_course()
    db = SessionLocal()
    try:
        student = TOOLS["create_student"].fn(db, name="Progress Kid")
        result = TOOLS["log_progress"].fn(
            db, student_id=str(student["id"]), block_id=str(root_id),
            status="practicing", notes="sounding good",
        )
    finally:
        db.close()

    assert result == {"status": "practicing", "block_id": root_id}

    db2 = SessionLocal()
    try:
        row = db2.scalars(
            select(Progress).where(
                Progress.student_id == student["id"], Progress.block_id == root_id,
            )
        ).first()
        assert row is not None
        assert row.status == "practicing"
        assert row.notes == "sounding good"
    finally:
        db2.close()


def test_log_progress_second_call_updates_same_row_not_duplicate():
    root_id = _seed_template_course()
    db = SessionLocal()
    try:
        student = TOOLS["create_student"].fn(db, name="Upsert Kid")
        student_id = student["id"]
        TOOLS["log_progress"].fn(db, student_id=str(student_id), block_id=str(root_id), status="introduced")
        TOOLS["log_progress"].fn(db, student_id=str(student_id), block_id=str(root_id), status="mastered")

        rows = db.scalars(
            select(Progress).where(Progress.student_id == student_id, Progress.block_id == root_id)
        ).all()
    finally:
        db.close()

    assert len(rows) == 1
    assert rows[0].status == "mastered"


def test_log_progress_malformed_student_id_returns_graceful_error():
    root_id = _seed_template_course()
    db = SessionLocal()
    try:
        result = TOOLS["log_progress"].fn(
            db, student_id="not-a-uuid", block_id=str(root_id), status="practicing",
        )
    finally:
        db.close()
    assert "error" in result


def test_log_progress_malformed_block_id_returns_graceful_error():
    db = SessionLocal()
    try:
        student = TOOLS["create_student"].fn(db, name="Malformed Block Kid")
        result = TOOLS["log_progress"].fn(
            db, student_id=str(student["id"]), block_id="not-a-uuid", status="practicing",
        )
    finally:
        db.close()
    assert "error" in result


def test_log_progress_unknown_student_id_returns_graceful_error():
    root_id = _seed_template_course()
    db = SessionLocal()
    try:
        result = TOOLS["log_progress"].fn(
            db, student_id=str(uuid.uuid4()), block_id=str(root_id), status="practicing",
        )
    finally:
        db.close()
    assert result == {"error": "student not found"}


def test_log_progress_unknown_block_id_returns_graceful_error():
    db = SessionLocal()
    try:
        student = TOOLS["create_student"].fn(db, name="Unknown Block Kid")
        result = TOOLS["log_progress"].fn(
            db, student_id=str(student["id"]), block_id=str(uuid.uuid4()), status="practicing",
        )
    finally:
        db.close()
    assert result == {"error": "block not found"}


def test_log_progress_invalid_status_returns_graceful_error():
    """The tool layer validates `status` against the allowed set even though
    `Progress.status` itself is a soft, unconstrained column (see `_log_
    progress`'s own docstring for why a chat model needs this guard when the
    HTTP route's fixed-choice UI control doesn't)."""
    root_id = _seed_template_course()
    db = SessionLocal()
    try:
        student = TOOLS["create_student"].fn(db, name="Bad Status Kid")
        result = TOOLS["log_progress"].fn(
            db, student_id=str(student["id"]), block_id=str(root_id), status="fluent",
        )
    finally:
        db.close()
    assert "error" in result
