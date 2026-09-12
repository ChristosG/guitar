import uuid
import pytest
from app.curriculum.corpus import LibraryContext
from app.curriculum.draft import LessonContext, build_lesson_messages
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


# ---------------------------------------------------------------------------
# Task 3.2 — THE CURRICULUM DRAFTER'S NEIGHBOURS AND THE TUTOR'S BRIEF
#
# (`app.curriculum.draft.build_lesson_messages` — the prompt every lesson of a
# CURRICULUM is written from, a different drafter from the selection-based one
# above, and the other half of "the lesson prompt" this file is named for.)
#
# Until now a lesson was told only «lesson 3 of 4 in module 2 of 5» and was
# ordered not to re-teach what came before — without ever being shown WHAT came
# before. It cannot obey an instruction it has no data for, so lessons re-taught
# each other. The neighbours block is the data; the tutor brief block is what he
# asked for THIS lesson, honoured whole.
# ---------------------------------------------------------------------------

_NEIGHBOURS = {"prev": "Τα ξύλα — ξύλα", "next": "—", "siblings": "Μαγνήτες"}


def _curriculum_ctx() -> LessonContext:
    return LessonContext(
        lesson_title="Το σχήμα C", lesson_objective="Βρες τη ρίζα.",
        module_title="CAGED", module_objective="Βλέπε το μπράτσο σε σχήματα.",
        course_title="Τόνος", tier="library", position="lesson 2 of 3 in module 1 of 2",
        minutes=50, teaching_minutes=50, target_words=2750, floor_words=2200,
    )


def _lesson_prompt(**kwargs) -> str:
    kw = {"student_brief": None, "course_brief": None, **kwargs}
    return build_lesson_messages(
        ctx=_curriculum_ctx(), library=LibraryContext(text="", token_count=0, fits=True),
        language="el", **kw,
    )[-1]["content"]


def test_the_draft_is_shown_the_lessons_on_either_side_of_it():
    text = _lesson_prompt(neighbours=_NEIGHBOURS)
    assert "ΠΡΟΗΓΟΥΜΕΝΟ ΜΑΘΗΜΑ: Τα ξύλα — ξύλα" in text
    assert "ΕΠΟΜΕΝΟ ΜΑΘΗΜΑ: —" in text                  # an edge lesson says so honestly
    assert "ΣΤΗΝ ΙΔΙΑ ΕΝΟΤΗΤΑ: Μαγνήτες" in text
    # Immediately after the POSITION line it explains: "do not re-teach" needs to
    # be read next to the thing that was taught.
    assert text.index("POSITION:") < text.index("ΠΡΟΗΓΟΥΜΕΝΟ ΜΑΘΗΜΑ:")
    assert text.index("ΠΡΟΗΓΟΥΜΕΝΟ ΜΑΘΗΜΑ:") < text.index("LENGTH IS NOT OPTIONAL")


def test_no_neighbours_renders_nothing_at_all():
    """The lesson AI panel and every old caller pass nothing — those prompts must
    be byte-identical to before this parameter existed."""
    assert _lesson_prompt() == _lesson_prompt(neighbours=None)
    assert "ΠΡΟΗΓΟΥΜΕΝΟ ΜΑΘΗΜΑ" not in _lesson_prompt()


def test_the_tutor_s_brief_for_this_lesson_is_carried_whole():
    text = _lesson_prompt(course_brief="Θέλω μάθημα για ερασιτέχνες.",
                          tutor_brief="Ξύλο μπράτσου: Maple έναντι Mahogany.")
    assert "Ξύλο μπράτσου: Maple έναντι Mahogany." in text
    assert "Ο ΚΑΘΗΓΗΤΗΣ ΖΗΤΗΣΕ ΡΗΤΑ" in text
    # After the COURSE brief: what he asked of the whole course is the context,
    # what he asked of THIS lesson is the instruction, and it reads second.
    assert text.index("Θέλω μάθημα για ερασιτέχνες.") < text.index("Ο ΚΑΘΗΓΗΤΗΣ ΖΗΤΗΣΕ ΡΗΤΑ")


def test_no_brief_and_a_blank_brief_both_render_nothing():
    assert _lesson_prompt() == _lesson_prompt(tutor_brief=None)
    assert _lesson_prompt() == _lesson_prompt(tutor_brief="   ")
