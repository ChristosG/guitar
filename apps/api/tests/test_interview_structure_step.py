"""Plan C, Task 5 — the wizard's optional, skippable "Lesson structure" step.

Inserted BETWEEN "scope" and "sources" (Resolved design call #1): it doesn't
disturb the existing `sources -> outline` auto-regenerate handoff, and the
blueprint shapes lesson DRAFTING, not the outline.

Spec invariant #7: the step is OPTIONAL + SKIPPABLE, pre-filled with
`resolve_default_blueprint(db)`. `{"skip": true}` advances with nothing recorded
in `interview.answers["structure"]`, and `_answer_confirm` (already wired, Task 3)
falls back to the settings default. `{"blueprint": {...}}` validates via
`validate_blueprint` and, on success, stores the NORMALIZED result — an invalid
one re-asks and LEAVES the step unchanged, same contract every other step in this
state machine honours.
"""
import uuid

import pytest

import app.curriculum.corpus as corpus_mod
import app.curriculum.interview as interview_mod
import app.curriculum.outline as outline_mod
from app.curriculum.blueprint import default_blueprint, validate_blueprint
from app.curriculum.blueprint_store import resolve_default_blueprint, save_default_blueprint
from app.curriculum.interview import (
    answer_interview,
    describe_step,
    generate_interview_outline,
    start_interview,
)
from app.models.block import Block
from app.models.knowledge import KnowledgeSource, Page


class _FakeProvider:
    def __init__(self, outline=None):
        self.outline = outline or _OUTLINE
        self.calls: list[dict] = []

    def guided_json(self, messages, schema, *, temperature=0.2, role="spec", max_tokens=None):
        self.calls.append({"messages": messages, "role": role})
        return self.outline

    def count_tokens(self, text: str) -> int:
        return len(text) // 3 + 1


_OUTLINE = {
    "title": "Tone Fundamentals",
    "modules": [
        {
            "title": "Module 0", "objective": "o", "tier": "library",
            "coverage_note": "p.19",
            "lessons": [{"title": "L0.0", "objective": "o", "est_minutes": 50}],
        },
    ],
}


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    provider = _FakeProvider()
    from app.curriculum.outline import generate_outline as real

    monkeypatch.setattr(interview_mod, "generate_outline", real)
    monkeypatch.setattr(outline_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)
    return provider


def _source(db) -> KnowledgeSource:
    source = KnowledgeSource(type="pdf", title="Getting Great Guitar Sounds", language="en",
                             char_count=62585, status="ready")
    db.add(source)
    db.flush()
    db.add(Page(source_id=source.id, page_no=19,
                text="A humbucker cancels hum by pairing opposed coils. " * 4,
                status="ready"))
    db.commit()
    return source


def _start(db, title="Tone Fundamentals"):
    return start_interview(db, title=title)


def _walk_to_structure(db, interview):
    """Drive the machine up to (not through) "structure"."""
    r = answer_interview(db, interview, {"student_id": None, "level": "all_levels"})
    assert r["ok"]
    r = answer_interview(db, interview, {"weeks": 8, "sessions_per_week": 1,
                                         "minutes_per_session": 50})
    assert r["ok"]
    r = answer_interview(db, interview, {"brief": "get him playing blues",
                                         "gap_policy": "general_knowledge"})
    assert r["ok"]
    assert interview.step == "structure", (
        "'structure' must sit directly after 'scope' (Resolved design call #1)"
    )


def _walk_through_structure(db, interview, structure_answer, *, source_ids=None):
    """`_walk_to_structure` plus answering it and driving all the way to 'confirm'."""
    _walk_to_structure(db, interview)
    r = answer_interview(db, interview, structure_answer)
    assert r["ok"], r.get("error")
    assert interview.step == "sources"

    r = answer_interview(db, interview, {"source_ids": [str(s) for s in (source_ids or [])]})
    assert r["ok"]
    assert interview.step == "outline"

    r = answer_interview(db, interview, {"regenerate": True})
    assert r.get("generate_outline") is True
    interview.outline = generate_interview_outline(db, interview)
    r = answer_interview(db, interview, {"outline": interview.outline})
    assert r["ok"]
    assert interview.step == "confirm"


# ---------------------------------------------------------------------------
# Placement + pre-fill
# ---------------------------------------------------------------------------

def test_structure_sits_between_scope_and_sources(db):
    interview = _start(db)
    _walk_to_structure(db, interview)
    assert interview.step == "structure"


def test_describe_step_prefills_the_resolved_settings_default(db):
    interview = _start(db)
    _walk_to_structure(db, interview)

    state = describe_step(db, interview)

    assert state["findings"]["blueprint"] == resolve_default_blueprint(db)


def test_describe_step_prefills_a_CUSTOM_settings_default_when_the_tutor_saved_one(db):
    custom = default_blueprint()
    custom["sections"][0]["weight"] = 0.42
    save_default_blueprint(db, custom)

    interview = _start(db)
    _walk_to_structure(db, interview)
    state = describe_step(db, interview)

    assert state["findings"]["blueprint"] == resolve_default_blueprint(db)
    assert state["findings"]["blueprint"]["sections"][0]["weight"] == 0.42


# ---------------------------------------------------------------------------
# Skip — invariant #7: OPTIONAL + SKIPPABLE
# ---------------------------------------------------------------------------

def test_skip_advances_to_sources_without_recording_a_blueprint(db):
    interview = _start(db)
    _walk_to_structure(db, interview)

    r = answer_interview(db, interview, {"skip": True})

    assert r["ok"] and not r["done"]
    assert interview.step == "sources"
    structure = interview.answers.get("structure") or {}
    assert "blueprint" not in structure, "a skip must leave no custom blueprint behind"


# ---------------------------------------------------------------------------
# A custom blueprint
# ---------------------------------------------------------------------------

def test_a_custom_blueprint_advances_and_is_stored_normalized(db):
    interview = _start(db)
    _walk_to_structure(db, interview)
    custom = default_blueprint()
    custom["sections"][0]["weight"] = 0.5

    r = answer_interview(db, interview, {"blueprint": custom})

    assert r["ok"] and not r["done"]
    assert interview.step == "sources"
    assert interview.answers["structure"]["blueprint"] == validate_blueprint(custom)


def test_an_invalid_blueprint_reasks_and_leaves_the_step_unchanged(db):
    interview = _start(db)
    _walk_to_structure(db, interview)
    renamed = default_blueprint()
    for s in renamed["sections"]:
        if s["kind"] == "exercises":
            s["key"] = "drills"  # forbidden rename — invariant #4

    r = answer_interview(db, interview, {"blueprint": renamed})

    assert r["ok"] is False
    assert interview.step == "structure"
    assert "structure" not in interview.answers


@pytest.mark.parametrize("bad", [
    None, "just a string", 42, ["a", "list"], {}, {"blueprint": "not-a-dict"},
])
def test_structure_step_rejects_garbage_without_crashing(db, bad):
    interview = _start(db)
    _walk_to_structure(db, interview)

    r = answer_interview(db, interview, bad)

    assert r["ok"] is False
    assert interview.step == "structure"


# ---------------------------------------------------------------------------
# Confirm materializes what "structure" decided (or the settings default)
# ---------------------------------------------------------------------------

def test_confirm_materializes_the_custom_blueprint_when_structure_supplied_one(db):
    source = _source(db)
    interview = _start(db)
    custom = default_blueprint()
    custom["sections"][0]["weight"] = 0.33
    _walk_through_structure(db, interview, {"blueprint": custom}, source_ids=[source.id])

    r = answer_interview(db, interview, {"approved": True})
    assert r["ok"] and r["done"]

    course = db.get(Block, interview.root_id)
    assert course.meta["blueprint"] == validate_blueprint(custom)


def test_confirm_materializes_the_settings_default_when_structure_was_skipped(db):
    custom_default = default_blueprint()
    custom_default["sections"][0]["weight"] = 0.19
    save_default_blueprint(db, custom_default)

    source = _source(db)
    interview = _start(db)
    _walk_through_structure(db, interview, {"skip": True}, source_ids=[source.id])

    r = answer_interview(db, interview, {"approved": True})
    assert r["ok"] and r["done"]

    course = db.get(Block, interview.root_id)
    assert course.meta["blueprint"] == resolve_default_blueprint(db)
    assert course.meta["blueprint"]["sections"][0]["weight"] == 0.19


def test_confirm_materializes_the_CODE_default_when_structure_was_skipped_and_no_override(db):
    """Nobody saved a settings default -> the code default, exactly as a course
    materialized with no wizard involvement at all would get (invariant #3)."""
    source = _source(db)
    interview = _start(db)
    _walk_through_structure(db, interview, {"skip": True}, source_ids=[source.id])

    r = answer_interview(db, interview, {"approved": True})
    assert r["ok"] and r["done"]

    course = db.get(Block, interview.root_id)
    assert course.meta["blueprint"] == default_blueprint()
