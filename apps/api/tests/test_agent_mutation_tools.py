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

    def _fake_generate(db, *, kind, prompt, block_id, ground, locale):
        captured.update(
            kind=kind, prompt=prompt, block_id=block_id, ground=ground, locale=locale,
        )
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
        # Not passed by this call — the fn's own default, which is the APP's
        # default locale (`el`), never "en" (Plan 13, Stage 5.4).
        "locale": "el",
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

    def _fake_generate(db, **kwargs):
        captured.update(kwargs)
        return fake_root

    monkeypatch.setattr(agent_tools, "_generate_curriculum_service", _fake_generate)

    result = TOOLS["generate_curriculum"].fn(
        None, title="Blues 101", language="en", profile={"level": "beginner"},
        brief="get him playing 12-bar blues", weeks=20, minutes_per_session=50,
    )

    assert captured["title"] == "Blues 101"
    assert captured["language"] == "en"
    assert captured["brief"] == "get him playing 12-bar blues"
    assert captured["weeks"] == 20
    assert captured["minutes_per_session"] == 50
    assert result == {"root_id": fake_root}


def test_generate_curriculum_no_longer_accepts_a_domain(monkeypatch):
    """`domain` is DEAD (Plan 13, Stage 6). Chris asked what it was for; the
    answer was one line in one prompt plus a retrieval filter that — after
    5d77bd0 — could no longer exclude anything. `brief` replaces it, and unlike
    `domain` it reaches every lesson-draft prompt rather than only the outline.
    """
    with pytest.raises(TypeError):
        TOOLS["generate_curriculum"].fn(None, title="Blues 101", domain="theory")


def test_generate_curriculum_schema_does_not_offer_domain_or_language_to_the_model():
    props = TOOLS["generate_curriculum"].schema["function"]["parameters"]["properties"]
    assert "domain" not in props
    # `language` was a REQUIRED, model-chosen parameter here — which is how a Greek
    # tutor got an English curriculum. It is injected from the session now.
    assert "language" not in props
    assert "brief" in props


def test_generate_curriculum_rejects_a_student_that_does_not_exist(db):
    """`student_id` is new on this tool, and it must resolve to a REAL student —
    the whole point of threading it through is that the lesson-draft prompt gets a
    brief built from his actual notes. A dangling id would silently produce a
    curriculum personalized for nobody.
    """
    result = TOOLS["generate_curriculum"].fn(
        db, title="Blues 101", student_id=str(uuid.uuid4()),
    )
    assert "error" in result
    assert "not found" in result["error"]


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


def _failing_ingest_source(db, source_id, payload):
    """Stands in for a swallowed ingestion failure (embed model down /
    extraction error): leaves the KnowledgeSource at status='failed' WITHOUT
    raising — exactly what the real `app.brain.ingest.ingest_source` does for
    an ordinary failure (see that module's swallow-not-raise design)."""
    source = db.get(KnowledgeSource, source_id)
    source.status = "failed"
    source.error = "embed model unreachable"
    db.commit()


def test_promote_note_to_knowledge_ingestion_failure_does_not_flip_flag(monkeypatch):
    """Regression (whole-plan review, Important 2): `ingest_source` swallows
    ordinary failures and leaves status='failed' without raising. Because
    promotion is a documented ONE-WAY flip (no un-promote; a second attempt
    hard-errors), flipping `promoted_to_knowledge` on a FAILED ingest would
    permanently and silently "succeed" an empty, non-retrievable source. The
    wrapper must surface the failure AND leave the flag False so a retry is
    still possible.
    """
    monkeypatch.setattr(knowledge_router, "ingest_source", _failing_ingest_source)

    db = SessionLocal()
    try:
        note = Note(title="Fails to ingest", body="some real body text")
        db.add(note)
        db.commit()
        note_id = note.id

        result = TOOLS["promote_note_to_knowledge"].fn(db, note_id=str(note_id))

        got = db.get(Note, note_id)
        assert "error" in result  # failure surfaced, not a silent success
        assert got.promoted_to_knowledge is False  # NOT flipped -> retryable
    finally:
        db.close()


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


def test_log_progress_omitting_notes_preserves_existing_notes():
    """Regression (whole-plan review, Important 1): the agent has NO read
    tool surfacing a student's Progress rows, so a plain "mark barre chords
    as mastered for Maria" makes the model call log_progress with `notes`
    OMITTED. `upsert_progress` overwrites notes WHOLESALE (documented — never
    merges), so an omitted notes would silently NULL a previously-recorded
    note, and the approval card (proposed args only, no `notes` key) can't
    even show the loss coming. The tool wrapper — the one caller structurally
    unable to resend a value it never saw (unlike progress-row.tsx, which
    resends the existing notes on every status click) — must preserve the
    existing notes when the caller omits them.
    """
    root_id = _seed_template_course()
    db = SessionLocal()
    try:
        student = TOOLS["create_student"].fn(db, name="Notes Preserve Kid")
        student_id = student["id"]
        # first: record a status WITH a note
        TOOLS["log_progress"].fn(
            db, student_id=str(student_id), block_id=str(root_id),
            status="practicing", notes="struggled with barre chords",
        )
        # then: change ONLY the status, omitting notes (exactly what the model
        # emits for "mark that as mastered")
        result = TOOLS["log_progress"].fn(
            db, student_id=str(student_id), block_id=str(root_id), status="mastered",
        )
        assert result == {"status": "mastered", "block_id": root_id}

        row = db.scalars(
            select(Progress).where(Progress.student_id == student_id, Progress.block_id == root_id)
        ).first()
    finally:
        db.close()

    assert row.status == "mastered"
    assert row.notes == "struggled with barre chords"  # PRESERVED, not nulled


def test_log_progress_explicit_notes_still_overwrites_existing():
    """The preserve-by-default guard above must NOT block an explicitly
    provided new note from replacing the old one — only an OMITTED `notes`
    preserves; a provided value (the caller genuinely restating the note)
    still applies, same as it always did.
    """
    root_id = _seed_template_course()
    db = SessionLocal()
    try:
        student = TOOLS["create_student"].fn(db, name="Notes Overwrite Kid")
        student_id = student["id"]
        TOOLS["log_progress"].fn(
            db, student_id=str(student_id), block_id=str(root_id),
            status="practicing", notes="old note",
        )
        TOOLS["log_progress"].fn(
            db, student_id=str(student_id), block_id=str(root_id),
            status="mastered", notes="new note",
        )
        row = db.scalars(
            select(Progress).where(Progress.student_id == student_id, Progress.block_id == root_id)
        ).first()
    finally:
        db.close()

    assert row.notes == "new note"


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


# ---------------------------------------------------------------------------
# Lesson Authoring agent tools (Plan 10 Task 3) — draft_lesson_from_selection/
# split_session/merge_sessions/add_session, wrapping app.lessons.draft/
# app.lessons.edit (Plan 10 Tasks 1-2) the same "thin wrapper, dispatched
# through TOOLS[name].fn(db, **args) exactly as Task 4's resolve step will"
# way every mutation above is tested.
# ---------------------------------------------------------------------------

def _seed_lesson(session_item_minutes: list[list[int]]) -> uuid.UUID:
    """A `lesson` Block with one `session` child per entry in
    `session_item_minutes`, each session's `item` children given the listed
    per-item `est_minutes`. Mirrors `_seed_template_course`'s own inline ORM
    seeding convention. Returns the lesson's root id.
    """
    db = SessionLocal()
    try:
        lesson = Block(kind="lesson", title="Test Lesson", language="en", is_template=False, plane="content")
        db.add(lesson)
        db.flush()
        for s_i, item_minutes in enumerate(session_item_minutes):
            session = Block(
                kind="session", title=f"Session {s_i + 1}", order=s_i, parent_id=lesson.id,
                language="en", is_template=False, plane="content",
                est_minutes=sum(item_minutes) or None,
            )
            db.add(session)
            db.flush()
            for i_i, minutes in enumerate(item_minutes):
                db.add(Block(
                    kind="item", title=f"Item {s_i + 1}.{i_i + 1}", order=i_i, parent_id=session.id,
                    language="en", is_template=False, plane="content", est_minutes=minutes,
                ))
        db.commit()
        return lesson.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# draft_lesson_from_selection — LLM-backed + async_job=True: same monkeypatch
# approach as generate_curriculum's own section above; the unknown-source-id
# guard lives INSIDE the real service (checked BEFORE the LLM call — see
# `draft_lesson_from_selection`'s own docstring), so that one test calls the
# real, unpatched tool and never triggers a live LLM call either.
# ---------------------------------------------------------------------------

def test_draft_lesson_from_selection_wraps_service_lesson_id(monkeypatch):
    captured = {}
    fake_lesson_id = uuid.uuid4()

    def _fake_draft(db, *, source_id, page_no, text, language):
        captured.update(source_id=source_id, page_no=page_no, text=text, language=language)
        return fake_lesson_id

    monkeypatch.setattr(agent_tools, "_draft_lesson_service", _fake_draft)

    source_id = uuid.uuid4()
    result = TOOLS["draft_lesson_from_selection"].fn(
        None, source_id=str(source_id), page_no=21, text="Open position chords...", language="en",
    )

    assert captured == {
        "source_id": source_id, "page_no": 21, "text": "Open position chords...", "language": "en",
    }
    assert result == {"lesson_id": fake_lesson_id}


def test_draft_lesson_from_selection_malformed_source_id_returns_graceful_error(monkeypatch):
    monkeypatch.setattr(
        agent_tools, "_draft_lesson_service",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("service must not be called")),
    )
    result = TOOLS["draft_lesson_from_selection"].fn(
        None, source_id="not-a-uuid", page_no=1, text="x",
    )
    assert "error" in result


def test_draft_lesson_from_selection_unknown_source_id_returns_graceful_error():
    """The real service checks `KnowledgeSource` existence BEFORE ever
    calling the LLM (its own docstring) — so this exercises the REAL,
    unpatched tool and never triggers a live model call.
    """
    db = SessionLocal()
    try:
        result = TOOLS["draft_lesson_from_selection"].fn(
            db, source_id=str(uuid.uuid4()), page_no=1, text="some passage",
        )
    finally:
        db.close()
    assert "error" in result


# ---------------------------------------------------------------------------
# split_session — deterministic, real DB, no LLM.
# ---------------------------------------------------------------------------

def test_split_session_tool_splits_a_real_session_preserving_every_item():
    lesson_id = _seed_lesson([[20, 20, 20, 20]])  # one 80-minute session, 4 items
    db = SessionLocal()
    try:
        session = db.scalars(select(Block).where(Block.parent_id == lesson_id)).first()
        result = TOOLS["split_session"].fn(db, session_id=str(session.id), session_minutes=30)
    finally:
        db.close()

    assert "sessions" in result
    assert len(result["sessions"]) >= 2
    for s in result["sessions"]:
        assert s["id"] and s["title"] and s["est_minutes"]

    db2 = SessionLocal()
    try:
        # the original session is gone...
        assert db2.get(Block, session.id) is None
        # ...but every item is preserved under the new sessions, same count.
        new_ids = [s["id"] for s in result["sessions"]]
        items = db2.scalars(select(Block).where(Block.parent_id.in_(new_ids))).all()
        assert len(items) == 4
    finally:
        db2.close()


def test_split_session_tool_returns_graceful_error_for_malformed_session_id():
    result = TOOLS["split_session"].fn(None, session_id="not-a-uuid", session_minutes=30)
    assert "error" in result


def test_split_session_tool_returns_graceful_error_for_unknown_session_id():
    db = SessionLocal()
    try:
        result = TOOLS["split_session"].fn(db, session_id=str(uuid.uuid4()), session_minutes=30)
    finally:
        db.close()
    assert "error" in result


def test_split_session_tool_returns_graceful_error_for_a_non_session_block():
    lesson_id = _seed_lesson([[20]])
    db = SessionLocal()
    try:
        result = TOOLS["split_session"].fn(db, session_id=str(lesson_id), session_minutes=30)
    finally:
        db.close()
    assert "error" in result


def test_split_session_tool_returns_graceful_error_when_session_has_no_items():
    lesson_id = _seed_lesson([[]])
    db = SessionLocal()
    try:
        session = db.scalars(select(Block).where(Block.parent_id == lesson_id)).first()
        result = TOOLS["split_session"].fn(db, session_id=str(session.id), session_minutes=30)
    finally:
        db.close()
    assert "error" in result


# ---------------------------------------------------------------------------
# merge_sessions — deterministic, real DB, no LLM. The non-adjacent-merge
# case is a REQUIRED test (task brief): a model choosing session ids freely
# WILL eventually pick non-adjacent ones.
# ---------------------------------------------------------------------------

def test_merge_sessions_tool_merges_adjacent_sessions():
    lesson_id = _seed_lesson([[20], [15]])
    db = SessionLocal()
    try:
        sessions = db.scalars(
            select(Block).where(Block.parent_id == lesson_id).order_by(Block.order)
        ).all()
        session_ids = [str(s.id) for s in sessions]
        result = TOOLS["merge_sessions"].fn(db, session_ids=session_ids)
    finally:
        db.close()

    assert result["id"] == sessions[0].id
    assert result["est_minutes"] == 35

    db2 = SessionLocal()
    try:
        assert db2.get(Block, sessions[1].id) is None  # donor gone
        items = db2.scalars(select(Block).where(Block.parent_id == sessions[0].id)).all()
        assert len(items) == 2  # both items now under the survivor
    finally:
        db2.close()


def test_merge_sessions_tool_returns_graceful_error_for_a_malformed_id():
    result = TOOLS["merge_sessions"].fn(None, session_ids=["not-a-uuid", str(uuid.uuid4())])
    assert "error" in result


def test_merge_sessions_tool_returns_graceful_error_for_fewer_than_two_ids():
    result = TOOLS["merge_sessions"].fn(None, session_ids=[str(uuid.uuid4())])
    assert "error" in result


def test_merge_sessions_tool_returns_graceful_error_for_unknown_session_ids():
    db = SessionLocal()
    try:
        result = TOOLS["merge_sessions"].fn(
            db, session_ids=[str(uuid.uuid4()), str(uuid.uuid4())],
        )
    finally:
        db.close()
    assert "error" in result


def test_merge_sessions_tool_returns_graceful_error_for_non_adjacent_sessions():
    """The critical guard: a model choosing session ids freely will
    eventually pick non-adjacent ones — `merge_sessions` (app.lessons.edit)
    rejects that with a ValueError, and this wrapper must surface it as a
    graceful dict, not raise into the ReAct loop.
    """
    lesson_id = _seed_lesson([[10], [10], [10]])
    db = SessionLocal()
    try:
        sessions = db.scalars(
            select(Block).where(Block.parent_id == lesson_id).order_by(Block.order)
        ).all()
        # first and LAST session — skips the middle one, non-adjacent.
        result = TOOLS["merge_sessions"].fn(
            db, session_ids=[str(sessions[0].id), str(sessions[2].id)],
        )
    finally:
        db.close()
    assert "error" in result
    assert "adjacent" in result["error"].lower()


# ---------------------------------------------------------------------------
# add_session — deterministic, real DB, no LLM.
# ---------------------------------------------------------------------------

def test_add_session_tool_creates_a_new_session_appended_at_the_end():
    lesson_id = _seed_lesson([[20]])
    db = SessionLocal()
    try:
        result = TOOLS["add_session"].fn(
            db, lesson_id=str(lesson_id), title="New Session", est_minutes=25,
        )
    finally:
        db.close()

    assert result["title"] == "New Session"
    assert result["est_minutes"] == 25

    db2 = SessionLocal()
    try:
        siblings = db2.scalars(
            select(Block).where(Block.parent_id == lesson_id).order_by(Block.order)
        ).all()
        assert len(siblings) == 2
        assert siblings[-1].id == result["id"]
    finally:
        db2.close()


def test_add_session_tool_inserts_after_a_given_session():
    lesson_id = _seed_lesson([[20], [15]])
    db = SessionLocal()
    try:
        original = db.scalars(
            select(Block).where(Block.parent_id == lesson_id).order_by(Block.order)
        ).all()
        first, second = original[0], original[1]
        result = TOOLS["add_session"].fn(
            db, lesson_id=str(lesson_id), title="Inserted", after=str(first.id),
        )
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        siblings = db2.scalars(
            select(Block).where(Block.parent_id == lesson_id).order_by(Block.order)
        ).all()
        assert [s.id for s in siblings] == [first.id, result["id"], second.id]
    finally:
        db2.close()


def test_add_session_tool_returns_graceful_error_for_malformed_lesson_id():
    result = TOOLS["add_session"].fn(None, lesson_id="not-a-uuid", title="x")
    assert "error" in result


def test_add_session_tool_returns_graceful_error_for_unknown_lesson_id():
    db = SessionLocal()
    try:
        result = TOOLS["add_session"].fn(db, lesson_id=str(uuid.uuid4()), title="x")
    finally:
        db.close()
    assert "error" in result


def test_add_session_tool_returns_graceful_error_for_malformed_after_id():
    lesson_id = _seed_lesson([[20]])
    db = SessionLocal()
    try:
        result = TOOLS["add_session"].fn(
            db, lesson_id=str(lesson_id), title="x", after="not-a-uuid",
        )
    finally:
        db.close()
    assert "error" in result


def test_add_session_tool_returns_graceful_error_when_after_is_not_a_session_of_this_lesson():
    lesson_id = _seed_lesson([[20]])
    other_lesson_id = _seed_lesson([[15]])
    db = SessionLocal()
    try:
        other_session = db.scalars(select(Block).where(Block.parent_id == other_lesson_id)).first()
        result = TOOLS["add_session"].fn(
            db, lesson_id=str(lesson_id), title="x", after=str(other_session.id),
        )
    finally:
        db.close()
    assert "error" in result
