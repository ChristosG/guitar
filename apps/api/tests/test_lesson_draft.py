import uuid
import pytest
from app.lessons.draft import draft_lesson_from_selection
from app.models.block import Block
from app.models.knowledge import KnowledgeSource


class _FakeProvider:
    """guided_json is schema-constrained, so a fake returns a valid tree."""
    def __init__(self):
        self.messages = None
        self.role = None

    def guided_json(self, messages, schema, *, temperature=0.2, role="spec", **kw):
        self.messages = messages
        self.role = role
        return {
            "title": "Pick Thickness and Tone",
            "sessions": [
                {"title": "Session 1: What a pick does", "est_minutes": 45,
                 "items": [{"title": "Thin vs heavy", "body": "A thin pick is brighter."}]},
                {"title": "Session 2: Choosing yours", "est_minutes": 30,
                 "items": [{"title": "The three-pick test", "body": "Buy thin, medium, heavy."}]},
            ],
        }


def _source(db) -> KnowledgeSource:
    src = KnowledgeSource(type="pdf", title="Getting Great Guitar Sounds", status="ready")
    db.add(src); db.commit()
    return src


def test_drafts_a_lesson_with_sessions_and_items(db, monkeypatch):
    fake = _FakeProvider()
    monkeypatch.setattr("app.lessons.draft.get_provider", lambda: fake)
    src = _source(db)

    lesson_id = draft_lesson_from_selection(
        db, source_id=src.id, page_no=19,
        text="You will notice that the tone is much thinner and brighter with the lighter pick.",
    )

    lesson = db.get(Block, lesson_id)
    assert lesson.kind == "lesson"
    assert lesson.title == "Pick Thickness and Tone"
    assert lesson.plane == "content"
    # The call must run under the `draft` role — without it, it lands on the
    # `spec` role's 4,096-token budget and a long Greek lesson truncates.
    assert fake.role == "draft"

    sessions = db.query(Block).filter_by(parent_id=lesson.id).order_by(Block.order).all()
    assert [s.kind for s in sessions] == ["session", "session"]
    assert sessions[0].est_minutes == 45
    items = db.query(Block).filter_by(parent_id=sessions[0].id).all()
    assert items[0].kind == "item"
    assert "thin pick" in items[0].body.lower()


def test_records_page_provenance_so_the_lesson_can_cite_its_source(db, monkeypatch):
    # B3 — this is what makes a lesson traceable back to the scan it came from
    monkeypatch.setattr("app.lessons.draft.get_provider", lambda: _FakeProvider())
    src = _source(db)

    lesson_id = draft_lesson_from_selection(db, source_id=src.id, page_no=19, text="passage")

    prov = db.get(Block, lesson_id).target_profile["provenance"]
    assert prov["source_id"] == str(src.id)
    assert prov["page_no"] == 19


def test_the_selected_passage_is_actually_given_to_the_model(db, monkeypatch):
    # a "grounded" draft that never sees the passage is not grounded
    fake = _FakeProvider()
    monkeypatch.setattr("app.lessons.draft.get_provider", lambda: fake)
    src = _source(db)

    draft_lesson_from_selection(db, source_id=src.id, page_no=19,
                                text="UNIQUE_PASSAGE_MARKER about pick thickness")

    sent = " ".join(m["content"] for m in fake.messages)
    assert "UNIQUE_PASSAGE_MARKER" in sent


def test_unknown_source_raises(db, monkeypatch):
    monkeypatch.setattr("app.lessons.draft.get_provider", lambda: _FakeProvider())
    with pytest.raises(ValueError):
        draft_lesson_from_selection(db, source_id=uuid.uuid4(), page_no=1, text="x")


def test_records_a_cross_page_range_in_provenance(db, monkeypatch):
    """G4 (Plan 12 Task 4): a selection made in the continuous-scroll Reader
    can span pages — the provenance must record the WHOLE range, not just
    one page, while `page_no` still reads as the range's start so the
    existing single-page UI (ProvenanceChip) keeps working unchanged."""
    monkeypatch.setattr("app.lessons.draft.get_provider", lambda: _FakeProvider())
    src = _source(db)

    lesson_id = draft_lesson_from_selection(
        db, source_id=src.id, page_from=21, page_to=23, text="passage spanning pages",
    )

    prov = db.get(Block, lesson_id).target_profile["provenance"]
    assert prov["source_id"] == str(src.id)
    assert prov["page_no"] == 21
    assert prov["page_from"] == 21
    assert prov["page_to"] == 23


def test_the_whole_ranged_passage_is_given_to_the_model(db, monkeypatch):
    # Grounding on a range still means grounding on the FULL text, not a
    # truncated/one-page slice of it.
    fake = _FakeProvider()
    monkeypatch.setattr("app.lessons.draft.get_provider", lambda: fake)
    src = _source(db)

    draft_lesson_from_selection(
        db, source_id=src.id, page_from=21, page_to=23,
        text="RANGE_MARKER_START ... spans three pages ... RANGE_MARKER_END",
    )

    sent = " ".join(m["content"] for m in fake.messages)
    assert "RANGE_MARKER_START" in sent and "RANGE_MARKER_END" in sent
    assert "pages 21-23" in sent


def test_page_to_before_page_from_is_rejected(db, monkeypatch):
    monkeypatch.setattr("app.lessons.draft.get_provider", lambda: _FakeProvider())
    src = _source(db)
    with pytest.raises(ValueError):
        draft_lesson_from_selection(db, source_id=src.id, page_from=23, page_to=21, text="x")


def test_missing_page_range_and_page_no_is_rejected(db, monkeypatch):
    monkeypatch.setattr("app.lessons.draft.get_provider", lambda: _FakeProvider())
    src = _source(db)
    with pytest.raises(ValueError):
        draft_lesson_from_selection(db, source_id=src.id, text="x")
