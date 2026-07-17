"""Unit tests for `app.agent.tools.TOOLS` — the read-only tool registry
(Plan 5 Task 2). `list_students`/`list_curricula`/`list_artifacts`/
`get_curriculum` are plain DB queries with no LLM/embed call anywhere (same
precedent `test_students_api.py` states for why it isn't marked
`@pytest.mark.integration`) — seeded directly via ORM rows (mirrors
`test_curriculum_api.py`'s own `_build_template_course` precedent) rather
than through the HTTP routes, since these tools are dispatched as plain
`fn(db, **args)` calls, never through FastAPI. `search_knowledge`/
`explain_concept` wrap `app.brain.retrieve.search`/`answer`, which themselves
call the live embed/chat model — monkeypatched here (mirrors
`test_artifact_generate.py`'s exact `monkeypatch.setattr(artifact_generate,
"search", _fake_search)` pattern) so this module never touches a live model.
"""
import uuid

import pytest
from sqlalchemy import text

import app.agent.tools as agent_tools
from app.agent.tools import TOOLS
from app.brain.retrieve import Answer, Hit
from app.db import Base, SessionLocal, engine
from app.models.artifact import Artifact
from app.models.block import Block
from app.models.student import Student

# Skip cleanly (not error) when no DB is reachable — mirrors test_students_api.py.
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
# Registry shape
# ---------------------------------------------------------------------------

def test_registry_has_exactly_the_read_tools_registered_so_far():
    """`TOOLS` is a SHARED registry dict (Plan 5 Task 3 adds "mutation" entries
    into this same dict — see `test_agent_hitl.py`'s own registry test for
    those), so this only asserts the READ subset: the original six (Plan 5 Task
    2), `find_lesson` (Plan 11 Task 2, C5), and `search_concepts` (C8) — same
    registry, same "kind" convention, so this set grows rather than a new one
    replacing it.
    """
    read_names = {name for name, entry in TOOLS.items() if entry.kind == "read"}
    assert read_names == {
        "search_knowledge", "explain_concept", "search_concepts",
        "list_students", "list_curricula", "list_artifacts", "get_curriculum",
        "find_lesson",
    }
    for name in read_names:
        entry = TOOLS[name]
        assert entry.schema["type"] == "function"
        fn_schema = entry.schema["function"]
        assert fn_schema["name"] == name
        assert fn_schema["description"]  # non-empty — the model reads this
        assert fn_schema["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# list_students
# ---------------------------------------------------------------------------

def test_list_students_returns_compact_rows_newest_first():
    db = SessionLocal()
    try:
        s1 = Student(name="Alex Doe", level="beginner", instrument="guitar")
        db.add(s1)
        db.commit()
        s2 = Student(name="Blair Roe", level="advanced", instrument="bass")
        db.add(s2)
        db.commit()

        rows = TOOLS["list_students"].fn(db)
    finally:
        db.close()

    ids = [r["id"] for r in rows]
    assert ids.index(s2.id) < ids.index(s1.id)  # newest first
    row = next(r for r in rows if r["id"] == s1.id)
    assert row == {
        "id": s1.id, "name": "Alex Doe", "level": "beginner",
        "instrument": "guitar", "status": "active",
    }


# ---------------------------------------------------------------------------
# list_curricula
# ---------------------------------------------------------------------------

def test_list_curricula_lists_template_roots_only():
    db = SessionLocal()
    try:
        root = Block(kind="course", title="Rhythm Basics", language="en", is_template=True)
        db.add(root)
        db.commit()
        # a non-root, non-template block must NOT show up as a curriculum
        db.add(Block(kind="module", title="child", language="en", parent_id=root.id))
        db.commit()

        rows = TOOLS["list_curricula"].fn(db)
    finally:
        db.close()

    assert any(r["id"] == root.id and r["title"] == "Rhythm Basics" for r in rows)
    assert all(r["title"] != "child" for r in rows)


# ---------------------------------------------------------------------------
# list_artifacts
# ---------------------------------------------------------------------------

def test_list_artifacts_filters_by_kind_and_block_id():
    db = SessionLocal()
    try:
        block = Block(kind="lesson", title="Lesson", language="en")
        db.add(block)
        db.commit()

        a1 = Artifact(kind="chord_diagram", spec={"name": "G"}, title="G", block_id=block.id)
        a2 = Artifact(kind="tab", spec={"alphaTex": "x"}, title="Tab", block_id=None)
        db.add_all([a1, a2])
        db.commit()

        by_kind = TOOLS["list_artifacts"].fn(db, kind="chord_diagram")
        by_block = TOOLS["list_artifacts"].fn(db, block_id=str(block.id))
        unfiltered = TOOLS["list_artifacts"].fn(db)
    finally:
        db.close()

    assert [r["id"] for r in by_kind] == [a1.id]
    assert [r["id"] for r in by_block] == [a1.id]
    assert {r["id"] for r in unfiltered} >= {a1.id, a2.id}


def test_list_artifacts_invalid_block_id_returns_graceful_error_not_a_crash():
    db = SessionLocal()
    try:
        result = TOOLS["list_artifacts"].fn(db, block_id="not-a-uuid")
    finally:
        db.close()
    assert "error" in result


# ---------------------------------------------------------------------------
# get_curriculum
# ---------------------------------------------------------------------------

def test_get_curriculum_returns_nested_tree_sorted_by_order():
    db = SessionLocal()
    try:
        root = Block(kind="course", title="Course", language="en", is_template=True)
        db.add(root)
        db.commit()
        # inserted out of `order` sequence to genuinely exercise the sort,
        # not coincidentally match insertion order (mirrors
        # test_curriculum_api.py's own `_build_template_course` precedent).
        db.add(Block(kind="lesson", title="Second", order=1, language="en", parent_id=root.id))
        db.add(Block(
            kind="lesson", title="First", order=0, language="en",
            parent_id=root.id, body="do this",
        ))
        db.commit()
        root_id = root.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        tree = TOOLS["get_curriculum"].fn(db2, root_id=str(root_id))
    finally:
        db2.close()

    assert tree["title"] == "Course"
    assert [c["title"] for c in tree["children"]] == ["First", "Second"]
    assert tree["children"][0]["body"] == "do this"


def test_get_curriculum_unknown_root_id_returns_graceful_error_not_a_crash():
    db = SessionLocal()
    try:
        result = TOOLS["get_curriculum"].fn(db, root_id=str(uuid.uuid4()))
    finally:
        db.close()
    assert "error" in result


def test_get_curriculum_malformed_root_id_returns_graceful_error():
    db = SessionLocal()
    try:
        result = TOOLS["get_curriculum"].fn(db, root_id="not-a-uuid")
    finally:
        db.close()
    assert "error" in result


# ---------------------------------------------------------------------------
# search_knowledge / explain_concept — provider-backed, monkeypatched
# ---------------------------------------------------------------------------

def test_search_knowledge_wraps_search_into_compact_rows(monkeypatch):
    # `score` reported to the model is the COSINE (`vector_score`), never
    # `Hit.score` — which is now an RRF fusion score in the ~0.03 range and would
    # read to the model as "nothing in the library is relevant".
    hits = [
        Hit(chunk_id="c1", source_id="s1", source_title="Pickups", text="hum is cancelled",
            section_path=None, page=None, score=0.0328, vector_score=0.912345),
    ]
    captured = {}

    def _fake_search(db, query, *, k=8):
        captured.update(query=query, k=k)
        return hits

    monkeypatch.setattr(agent_tools, "search", _fake_search)

    result = TOOLS["search_knowledge"].fn(None, query="what cancels hum?", k=3)

    assert captured["query"] == "what cancels hum?"
    assert captured["k"] == 3
    assert result == [{"source": "Pickups", "text": "hum is cancelled", "score": 0.912}]


def test_explain_concept_wraps_answer_with_numbered_citations(monkeypatch):
    hits = [
        Hit(chunk_id="c1", source_id="s1", source_title="Pickups", text="...",
            section_path=None, page=None, score=0.9),
        Hit(chunk_id="c2", source_id="s2", source_title="Delay", text="...",
            section_path=None, page=None, score=0.7),
    ]

    def _fake_answer(db, query, *, locale, k=8):
        return Answer(text="A humbucker cancels hum. [1]", citations=hits)

    monkeypatch.setattr(agent_tools, "answer", _fake_answer)

    result = TOOLS["explain_concept"].fn(None, query="what is a humbucker?", locale="en")

    assert result["text"] == "A humbucker cancels hum. [1]"
    assert result["citations"] == [
        {"n": 1, "source": "Pickups"},
        {"n": 2, "source": "Delay"},
    ]


# ---------------------------------------------------------------------------
# search_concepts (C8) — the cross-book tool `search_knowledge` cannot be
# ---------------------------------------------------------------------------

def test_search_concepts_surfaces_divergence_with_both_positions_and_citations():
    """THE POINT OF THE TOOL. Two books disagree on one concept; the tool result
    must flag the divergence and carry BOTH positions, each with its own book and
    real page — never averaged into one line. An English query needs no
    translation, so this runs the real search with no model."""
    import app.canon.search as canon_search
    from app.models.canon import Concept, ConceptClaim
    from app.models.knowledge import KnowledgeSource, Page

    canon_search.reset_concept_index()
    db = SessionLocal()
    try:
        hunter = KnowledgeSource(type="pdf", title="Tone Manual (Hunter)", status="ready")
        gallagher = KnowledgeSource(type="pdf", title="Guitar Tone (Gallagher)", status="ready")
        db.add_all([hunter, gallagher])
        db.flush()
        for src in (hunter, gallagher):
            db.add(Page(source_id=src.id, page_no=1, text="p", status="ready"))
        concept = Concept(key="pickup-height", label_en="Pickup height", label_el="Ύψος μαγνήτη")
        db.add(concept)
        db.flush()
        db.add(ConceptClaim(concept_id=concept.id, source_id=hunter.id,
                            text="Lowering the pickup kills sustain", pages=[113],
                            stance="warns a low pickup kills sustain", grounding="author"))
        db.add(ConceptClaim(concept_id=concept.id, source_id=gallagher.id,
                            text="A lower treble-side pickup fixes harshness", pages=[201],
                            stance="prefers a lower treble-side height", grounding="author"))
        db.commit()

        result = TOOLS["search_concepts"].fn(db, query="pickup height")
    finally:
        db.close()
        canon_search.reset_concept_index()

    hit = next(r for r in result if r["concept"].startswith("Pickup height"))
    assert hit["divergence"] is True
    positions = {p["position"] for p in hit["positions"]}
    assert "warns a low pickup kills sustain" in positions
    assert "prefers a lower treble-side height" in positions
    # Each position names its own book and carries a real page — cross-book, cited.
    books = {b for p in hit["positions"] for b in p["books"]}
    assert books == {"Tone Manual (Hunter)", "Guitar Tone (Gallagher)"}
    pages = {c["pages"] for p in hit["positions"] for c in p["citations"]}
    assert {"p.113", "p.201"} <= pages
