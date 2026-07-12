import uuid
import pytest
from app.lessons.draft import draft_lesson_from_selection
from app.models.block import Block
from app.models.knowledge import KnowledgeSource


class _FakeProvider:
    """guided_json is schema-constrained, so a fake returns a valid tree."""
    def __init__(self):
        self.messages = None

    def guided_json(self, messages, schema, *, temperature=0.2):
        self.messages = messages
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
