"""Task 1 (Spec A, apply side): surgical segment-level ops.

Today the smallest revise op rewrites a whole LESSON (`modify_lesson`). This adds
`add_segment` / `edit_segment` / `remove_segment` — ops that target one SEGMENT
inside a lesson — to the ops schema and `apply_revision`, plus the custom-segment
survival rule in `persist_lesson`: without it, any later lesson redraft would
silently delete a surgically-added segment along with the blueprint sections it
regenerates.

`apply_revision` still keeps its four properties for these ops too: ONE
transaction, ONE commit, rollback-on-exception, ZERO provider calls — apply only
creates/marks/deletes blocks. GENERATING a queued segment's body is a later task
(the job), not this one.

Mirrors test_revise_apply.py's fixture/skip-guard/setup_module conventions.
"""
import logging
import uuid

import pytest
from sqlalchemy import func, select, text

from app.db import Base, SessionLocal, engine
from app.models.block import Block
from app.curriculum.blueprint import default_blueprint, section_keys
from app.curriculum.corpus import LibraryContext
from app.curriculum.ground import Passage
from app.curriculum.refine import undo_refine
from app.curriculum.segment_generate import generate_segment
from app.jobs.curriculum_revise import run_curriculum_revise_job
from app.llm.errors import LLMError
from app.models.generation_job import GenerationJob
import app.curriculum.draft as draft_mod
import app.curriculum.revise as revise
import app.curriculum.segment_generate as segment_mod
import app.jobs.curriculum_draft as fanout_mod
import app.jobs.curriculum_revise as revise_job

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


def _children(db, parent_id, kind=None):
    q = select(Block).where(Block.parent_id == parent_id)
    if kind:
        q = q.where(Block.kind == kind)
    return db.scalars(q.order_by(Block.order)).all()


@pytest.fixture
def db_and_tree():
    """course (blueprint = code default) -> module -> lesson -> 2 segments
    (theory, exercises). `floor_words=10` on the lesson so `meets_floor`
    recomputation is exercised (8 words to start, under floor)."""
    db = SessionLocal()
    course = Block(kind="course", title="Tone", is_template=True, language="el",
                   meta={"brief": None, "source_ids": None,
                         "gap_policy": "general_knowledge", "blueprint": default_blueprint()})
    db.add(course)
    db.flush()
    module = Block(kind="module", title="M1", parent_id=course.id, order=0, language="el",
                   meta={"objective": "m1"})
    db.add(module)
    db.flush()
    lesson = Block(kind="lesson", title="L1", parent_id=module.id, order=0, language="el",
                   meta={"objective": "l1", "floor_words": 10})
    db.add(lesson)
    db.flush()
    seg1 = Block(kind="segment", title="Theory", body="word word word word word",
                 order=0, parent_id=lesson.id, language="el", meta={"section": "theory"})
    seg2 = Block(kind="segment", title="Exercises", body="word word word",
                 order=1, parent_id=lesson.id, language="el", meta={"section": "exercises"})
    db.add_all([seg1, seg2])
    db.commit()
    yield db, course, module, lesson, seg1, seg2
    db.close()


# ---------------------------------------------------------------------------
# validate_ops
# ---------------------------------------------------------------------------

def test_validate_ops_accepts_the_three_new_ops_with_required_fields(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "New bit",
         "instruction": "add a paragraph", "reason": "r"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "More theory",
         "instruction": "expand", "section_key": "theory", "reason": "r"},
        {"op": "edit_segment", "segment_id": str(seg1.id), "instruction": "simplify", "reason": "r"},
        {"op": "remove_segment", "segment_id": str(seg2.id), "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert [o["op"] for o in out["ops"]] == [
        "add_segment", "add_segment", "edit_segment", "remove_segment",
    ]


def test_validate_ops_rejects_add_segment_with_a_disabled_section_key(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Bad",
         "instruction": "x", "section_key": "not_a_real_section", "reason": "r"}]}
    out = revise.validate_ops(db, course.id, plan)
    assert out["ops"] == []


def test_validate_ops_keeps_add_segment_coupled_with_update_blueprint_enabling_it(db_and_tree):
    """The exact coupled shape REVISE_TAIL instructs: a plan that both enables a
    disabled section via update_blueprint AND adds segments under that section_key
    in the same plan. Before the fix, section_key was checked against the STORED
    blueprint only (homework disabled there), so every add_segment was silently
    dropped even though the plan's own update_blueprint would enable it."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    bp = default_blueprint()
    for s in bp["sections"]:
        if s["key"] == "homework":
            s["enabled"] = False
    course.meta = {**course.meta, "blueprint": bp}
    db.add(course)
    db.commit()

    enabled_bp = default_blueprint()  # homework enabled (the default)
    plan = {"summary": "s", "ops": [
        {"op": "update_blueprint", "blueprint": enabled_bp, "reason": "r"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "HW 1",
         "instruction": "x", "section_key": "homework", "reason": "r"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "HW 2",
         "instruction": "y", "section_key": "homework", "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert [o["op"] for o in out["ops"]] == [
        "update_blueprint", "add_segment", "add_segment",
    ]


def test_validate_ops_still_drops_add_segment_for_a_key_enabled_nowhere(db_and_tree):
    """Negative case: a section_key enabled in NEITHER the stored blueprint NOR the
    plan's own update_blueprint op is still dropped — the union widening must not
    become a free pass for an arbitrary/hallucinated section_key."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    bp = default_blueprint()
    for s in bp["sections"]:
        if s["key"] == "homework":
            s["enabled"] = False
    course.meta = {**course.meta, "blueprint": bp}
    db.add(course)
    db.commit()

    still_disabled_bp = default_blueprint()
    for s in still_disabled_bp["sections"]:
        if s["key"] == "homework":
            s["enabled"] = False
    plan = {"summary": "s", "ops": [
        {"op": "update_blueprint", "blueprint": still_disabled_bp, "reason": "r"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "HW 1",
         "instruction": "x", "section_key": "homework", "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert [o["op"] for o in out["ops"]] == ["update_blueprint"]


# ---------------------------------------------------------------------------
# set_section_enabled (2026-07-20 hotfix for the "add a homework section"
# incident) — a full walkthrough of validate_ops, the add_segment union, and
# apply's blueprint rebuild, in the EXACT production shape.
# ---------------------------------------------------------------------------

def test_validate_ops_keeps_set_section_enabled_for_an_existing_key(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    plan = {"summary": "s", "ops": [
        {"op": "set_section_enabled", "section_key": "homework", "enabled": True, "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert [o["op"] for o in out["ops"]] == ["set_section_enabled"]
    assert out["dropped"] == []


def test_validate_ops_drops_set_section_enabled_for_an_unknown_key(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    plan = {"summary": "s", "ops": [
        {"op": "set_section_enabled", "section_key": "not_a_real_section", "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert out["ops"] == []
    assert len(out["dropped"]) == 1
    assert out["dropped"][0]["op"]["section_key"] == "not_a_real_section"
    assert "not_a_real_section" in out["dropped"][0]["reason"]


def test_validate_ops_set_section_enabled_widens_the_add_segment_union(db_and_tree):
    """THE EXACT production shape of the incident this hotfix targets: homework
    starts disabled, and a plan both enables it (with a renamed Greek label) via
    `set_section_enabled` AND files two `add_segment` ops under it, in the SAME
    plan. `update_blueprint` already widened this union (an earlier fix); this
    proves `set_section_enabled` widens it too — ALL THREE ops must survive, or
    this hotfix has not actually closed the incident."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    bp = default_blueprint()
    for s in bp["sections"]:
        if s["key"] == "homework":
            s["enabled"] = False
    course.meta = {**course.meta, "blueprint": bp}
    db.add(course)
    db.commit()

    plan = {"summary": "add a homework section to every lesson", "ops": [
        {"op": "set_section_enabled", "section_key": "homework", "enabled": True,
         "label": "Εργασίες για το σπίτι", "reason": "the tutor wants homework"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "HW 1",
         "instruction": "x", "section_key": "homework", "reason": "r"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "HW 2",
         "instruction": "y", "section_key": "homework", "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert [o["op"] for o in out["ops"]] == [
        "set_section_enabled", "add_segment", "add_segment",
    ]
    assert out["dropped"] == []


def test_validate_ops_set_section_enabled_disabled_grants_nothing_to_the_union(db_and_tree):
    """Negative case mirroring the update_blueprint one: a `set_section_enabled`
    op that DISABLES (or omits `enabled`, defaulting true, but targets a
    DIFFERENT key) grants nothing to a still-disabled section's add_segment."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    bp = default_blueprint()
    for s in bp["sections"]:
        if s["key"] == "homework":
            s["enabled"] = False
    course.meta = {**course.meta, "blueprint": bp}
    db.add(course)
    db.commit()

    plan = {"summary": "s", "ops": [
        {"op": "set_section_enabled", "section_key": "homework", "enabled": False, "reason": "r"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "HW 1",
         "instruction": "x", "section_key": "homework", "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert [o["op"] for o in out["ops"]] == ["set_section_enabled"]


def test_validate_ops_in_plan_disable_removes_an_already_enabled_key_from_the_union(db_and_tree):
    """2026-07-20 hotfix (a): the union used to only GROW, so a plan that DISABLES
    an already-enabled section while ALSO adding a segment under that same
    section_key let the add through anyway — the disable "won" at apply, but the
    stray add_segment stayed filed under a section the tutor just turned off.
    `theory` starts enabled (the default blueprint); a plan that disables it must
    now drop the coupled add_segment, with a reason naming the disable."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    plan = {"summary": "s", "ops": [
        {"op": "set_section_enabled", "section_key": "theory", "enabled": False, "reason": "r"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "More theory",
         "instruction": "x", "section_key": "theory", "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert [o["op"] for o in out["ops"]] == ["set_section_enabled"]
    assert len(out["dropped"]) == 1
    dropped = out["dropped"][0]
    assert dropped["op"]["op"] == "add_segment"
    assert "disabled" in dropped["reason"] and "theory" in dropped["reason"]


def test_validate_ops_set_section_enabled_last_toggle_per_key_wins(db_and_tree):
    """2026-07-20 hotfix (b): enable-then-disable the SAME key in one plan — the
    disable (last op for that key) wins, so a coupled add_segment still drops."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    bp = default_blueprint()
    for s in bp["sections"]:
        if s["key"] == "homework":
            s["enabled"] = False
    course.meta = {**course.meta, "blueprint": bp}
    db.add(course)
    db.commit()

    plan = {"summary": "s", "ops": [
        {"op": "set_section_enabled", "section_key": "homework", "enabled": True, "reason": "r"},
        {"op": "set_section_enabled", "section_key": "homework", "enabled": False, "reason": "r2"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "HW 1",
         "instruction": "x", "section_key": "homework", "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    # BOTH set_section_enabled ops survive validate_ops (each is individually
    # valid — section_key exists in the stored blueprint); it is the UNION's
    # final per-key state, not op survival, that reflects "last wins".
    assert [o["op"] for o in out["ops"]] == ["set_section_enabled", "set_section_enabled"]
    assert len(out["dropped"]) == 1
    assert out["dropped"][0]["op"]["op"] == "add_segment"


def test_apply_set_section_enabled_flips_enabled_and_renames_only_the_course_language(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    bp = default_blueprint()
    original_en_label = next(s["label"]["en"] for s in bp["sections"] if s["key"] == "homework")
    for s in bp["sections"]:
        if s["key"] == "homework":
            s["enabled"] = False
    course.meta = {**course.meta, "blueprint": bp}
    db.add(course)
    db.commit()

    plan = {"summary": "s", "ops": [
        {"op": "set_section_enabled", "section_key": "homework", "enabled": True,
         "label": "Εργασίες για το σπίτι", "reason": "r"},
    ]}
    out = revise.apply_revision(db, course.id, plan)
    assert out["applied"] == 1

    db.expire_all()
    refreshed = db.get(Block, course.id)
    hw = next(s for s in refreshed.meta["blueprint"]["sections"] if s["key"] == "homework")
    assert hw["enabled"] is True
    assert hw["label"]["el"] == "Εργασίες για το σπίτι"
    assert hw["label"]["en"] == original_en_label   # course.language is "el" — "en" untouched
    # the rest of the blueprint (weight/kind/audience/description) survives
    # the rebuild unchanged — this op flips ONE flag, not the whole section.
    original = next(s for s in bp["sections"] if s["key"] == "homework")
    assert hw["weight"] == original["weight"]
    assert hw["kind"] == original["kind"]


def test_apply_set_section_enabled_can_disable_a_section(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    out = revise.apply_revision(db, course.id, {"summary": "s", "ops": [
        {"op": "set_section_enabled", "section_key": "recap", "enabled": False, "reason": "r"},
    ]})
    assert out["applied"] == 1
    db.expire_all()
    refreshed = db.get(Block, course.id)
    recap = next(s for s in refreshed.meta["blueprint"]["sections"] if s["key"] == "recap")
    assert recap["enabled"] is False


def test_validate_ops_then_apply_revision_the_full_homework_incident_shape(db_and_tree):
    """The COMBINED apply-level production-shape test: the exact plan shape the
    "add a homework section" incident's fix targets — set_section_enabled
    (enable + rename) coupled with two add_segment ops under that section_key,
    in ONE plan — run through validate_ops (as chat.py's approval path does)
    THEN apply_revision (as the approved-plan endpoint does). Both steps must
    agree: nothing this hotfix's union-widening keeps in validate_ops may be
    dropped again at apply, and the blueprint flip + both segments must land in
    the SAME transaction."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    bp = default_blueprint()
    for s in bp["sections"]:
        if s["key"] == "homework":
            s["enabled"] = False
    course.meta = {**course.meta, "blueprint": bp}
    db.add(course)
    db.commit()

    plan = {"summary": "add a homework section to every lesson", "ops": [
        {"op": "set_section_enabled", "section_key": "homework", "enabled": True,
         "label": "Εργασίες για το σπίτι", "reason": "the tutor wants homework"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "HW 1",
         "instruction": "practice the new chord shape daily", "section_key": "homework",
         "reason": "r"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "HW 2",
         "instruction": "record yourself and listen back", "section_key": "homework",
         "reason": "r"},
    ]}

    validated = revise.validate_ops(db, course.id, plan)
    assert [o["op"] for o in validated["ops"]] == [
        "set_section_enabled", "add_segment", "add_segment",
    ]
    assert validated["dropped"] == []

    out = revise.apply_revision(db, course.id, plan)
    assert out["applied"] == 3

    db.expire_all()
    refreshed = db.get(Block, course.id)
    hw = next(s for s in refreshed.meta["blueprint"]["sections"] if s["key"] == "homework")
    assert hw["enabled"] is True
    assert hw["label"]["el"] == "Εργασίες για το σπίτι"

    new_segs = _children(db, lesson.id, "segment")[2:]
    assert len(new_segs) == 2
    for seg in new_segs:
        assert seg.meta["section"] == "homework"
        assert seg.meta["segment_status"] == "queued"
        assert "custom" not in seg.meta


def test_compute_impact_set_section_enabled_is_blueprint_changed_not_destructive():
    ops = [{"op": "set_section_enabled", "section_key": "homework", "enabled": True, "reason": "r"}]
    impact = revise.compute_impact(ops)
    assert impact["blueprint_changed"] is True
    assert impact["destructive"] is False


def test_validate_ops_dropped_diagnostics_shape(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(uuid.uuid4()), "title": "x",
         "instruction": "y", "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert out["ops"] == []
    assert len(out["dropped"]) == 1
    entry = out["dropped"][0]
    assert set(entry.keys()) == {"op", "reason"}
    assert entry["op"]["op"] == "add_segment"
    assert isinstance(entry["reason"], str) and entry["reason"]


def test_validate_ops_logs_the_offending_id_value_not_a_bare_does_not_resolve(db_and_tree, caplog):
    """The production incident's complaint (b): the OLD log line said only "an
    id/payload does not resolve under root <root>" — no id, so nobody could
    tell whether the model sent a malformed uuid or an M1/L1-style shorthand.
    The new line must carry the actual offending value."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    bogus_id = "M1-L1"   # the exact shorthand shape suspected in the incident
    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": bogus_id, "title": "x", "instruction": "y", "reason": "r"},
    ]}
    with caplog.at_level(logging.WARNING, logger="app.curriculum.revise"):
        out = revise.validate_ops(db, course.id, plan)
    assert out["ops"] == []
    assert any(bogus_id in rec.message for rec in caplog.records)


def test_validate_ops_rejects_edit_and_remove_segment_targeting_a_lesson_id(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    plan = {"summary": "s", "ops": [
        {"op": "edit_segment", "segment_id": str(lesson.id), "instruction": "x", "reason": "r"},
        {"op": "remove_segment", "segment_id": str(lesson.id), "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert out["ops"] == []


def test_validate_ops_rejects_segment_ops_targeting_a_segment_outside_the_course(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    other_course = Block(kind="course", title="Other", is_template=True, language="el", meta={})
    db.add(other_course)
    db.flush()
    other_module = Block(kind="module", title="OM", parent_id=other_course.id, order=0, language="el",
                         meta={})
    db.add(other_module)
    db.flush()
    other_lesson = Block(kind="lesson", title="OL", parent_id=other_module.id, order=0, language="el",
                         meta={})
    db.add(other_lesson)
    db.flush()
    other_seg = Block(kind="segment", title="OS", body="x", order=0, parent_id=other_lesson.id,
                      language="el", meta={})
    db.add(other_seg)
    db.commit()

    plan = {"summary": "s", "ops": [
        {"op": "edit_segment", "segment_id": str(other_seg.id), "instruction": "x", "reason": "r"},
        {"op": "remove_segment", "segment_id": str(other_seg.id), "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert out["ops"] == []


# ---------------------------------------------------------------------------
# apply_revision — add_segment
# ---------------------------------------------------------------------------

def test_apply_add_segment_without_section_key_is_custom(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    out = revise.apply_revision(db, course.id, {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Bonus: Pedal chains",
         "instruction": "explain pedal chaining order", "reason": "r"}]})
    assert out == {"applied": 1, "root_id": str(course.id)}

    segs = _children(db, lesson.id, "segment")
    assert [s.title for s in segs] == ["Theory", "Exercises", "Bonus: Pedal chains"]
    new = segs[-1]
    assert new.body == ""
    assert new.meta["segment_status"] == "queued"
    assert new.meta["segment_instruction"] == "explain pedal chaining order"
    assert new.meta["custom"] is True
    assert new.meta["section"] == "custom:bonus-pedal-chains"

    db.refresh(lesson)
    assert lesson.meta["word_count"] == 8            # 5 + 3, the new segment's body is ""
    assert lesson.meta["meets_floor"] is False        # 8 < floor_words=10


def test_apply_add_segment_with_enabled_section_key(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    revise.apply_revision(db, course.id, {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "More on scales",
         "instruction": "expand theory", "section_key": "theory", "reason": "r"}]})
    new = _children(db, lesson.id, "segment")[-1]
    assert new.meta["section"] == "theory"
    assert "custom" not in new.meta


# ---------------------------------------------------------------------------
# apply_revision — edit_segment, and the undo_refine contract match
# ---------------------------------------------------------------------------

def test_apply_edit_segment_matches_the_refine_contract_and_undo_restores_it(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    revise.apply_revision(db, course.id, {"summary": "s", "ops": [
        {"op": "edit_segment", "segment_id": str(seg1.id), "instruction": "simplify the wording",
         "reason": "r"}]})
    db.refresh(seg1)
    assert seg1.meta["segment_status"] == "queued"
    assert seg1.meta["segment_instruction"] == "simplify the wording"
    assert seg1.meta["prev_body"] == "word word word word word"
    assert seg1.meta["prev_title"] == "Theory"
    assert seg1.meta["refined"] is True
    assert seg1.meta["refine_instruction"] == "simplify the wording"

    db.refresh(lesson)
    assert lesson.meta["word_count"] == 8             # recomputed even though bodies unchanged

    # Simulate the LATER generation step overwriting the segment, then undo it —
    # this is undo_refine (refine.py:179-190) unchanged, proving the meta keys
    # edit_segment stashes are exactly what it restores/strips.
    seg1.body = "the generated replacement body"
    seg1.title = "Renamed by generation"
    db.commit()

    assert undo_refine(seg1) is True
    db.commit()
    db.expire_all()

    seg1 = db.get(Block, seg1.id)
    assert seg1.body == "word word word word word"
    assert seg1.title == "Theory"
    for key in ("prev_body", "prev_title", "refined", "refine_instruction"):
        assert key not in seg1.meta
    # segment_status/segment_instruction are NOT part of undo_refine's strip set —
    # they are left as-is, same as any other meta key it does not know about.
    assert seg1.meta["segment_status"] == "queued"


# ---------------------------------------------------------------------------
# apply_revision — remove_segment
# ---------------------------------------------------------------------------

def test_apply_remove_segment_deletes_renormalises_and_recomputes(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    seg3 = Block(kind="segment", title="Recap", body="word word", order=2, parent_id=lesson.id,
                language="el", meta={"section": "recap"})
    db.add(seg3)
    db.commit()

    out = revise.apply_revision(db, course.id, {"summary": "s", "ops": [
        {"op": "remove_segment", "segment_id": str(seg1.id), "reason": "r"}]})
    assert out["applied"] == 1

    segs = _children(db, lesson.id, "segment")
    assert [s.title for s in segs] == ["Exercises", "Recap"]
    assert [s.order for s in segs] == [0, 1]           # renormalised

    db.refresh(lesson)
    assert lesson.meta["word_count"] == 5              # 3 (Exercises) + 2 (Recap)
    assert lesson.meta["meets_floor"] is False


# ---------------------------------------------------------------------------
# apply_revision — transactionality
# ---------------------------------------------------------------------------

def test_apply_revision_rolls_back_the_whole_plan_on_a_later_bad_op(db_and_tree, monkeypatch):
    """A plan whose second op is invalid AT APPLY TIME (simulated by monkeypatching
    validate_ops to pass everything through unfiltered, standing in for a garbled
    id that slipped past a weakened validate) must roll back the first op too —
    ONE transaction, not partial application (Global Constraint #3)."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    before = {b.id: (b.title, b.order, b.parent_id, b.meta) for b in db.query(Block).all()}

    def passthrough(db_, root_id, raw):
        return {"summary": raw.get("summary") or "", "ops": raw.get("ops") or []}

    monkeypatch.setattr(revise, "validate_ops", passthrough)

    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Will be rolled back",
         "instruction": "x", "reason": "r"},
        {"op": "remove_segment", "segment_id": str(uuid.uuid4()),
         "reason": "bogus id — passed a weakened validate"},
    ]}
    with pytest.raises(Exception):
        revise.apply_revision(db, course.id, plan)
    db.rollback()

    after = {b.id: (b.title, b.order, b.parent_id, b.meta) for b in db.query(Block).all()}
    assert after == before


# ---------------------------------------------------------------------------
# persist_lesson — custom-segment survival
# ---------------------------------------------------------------------------

def _drafted_lesson() -> dict:
    """A minimal but schema-valid drafted lesson for the default blueprint —
    trimmed copy of test_blueprint_regression.py's `_lesson()` fixture shape."""
    def prose(n):
        return {"body": " ".join(["word"] * n), "citations": []}

    return {
        "title": "Sample lesson title here",
        "summary": " ".join(["sum"] * 12),
        "warm_up": prose(40),
        "theory": prose(500),
        "demonstration": prose(300),
        "exercises": {
            "body": " ".join(["ex"] * 120),
            "items": [
                {"title": "Ex one", "instructions": " ".join(["do"] * 120), "est_minutes": 5},
                {"title": "Ex two", "instructions": " ".join(["do"] * 80), "est_minutes": 5},
            ],
            "citations": [],
        },
        "common_mistakes": prose(90),
        "recap": prose(30),
        "homework": prose(35),
        "qa_prompts": {
            "body": " ".join(["qa"] * 20),
            "items": [
                {"question": " ".join(["q"] * 10), "answer_key": " ".join(["a"] * 15)},
            ],
            "citations": [],
        },
    }


def test_persist_lesson_preserves_custom_segments_appended_after(db):
    from app.curriculum.corpus import LibraryContext
    from app.curriculum.depth import measure
    from app.curriculum.draft import persist_lesson

    lesson_block = Block(kind="lesson", title="L1", language="el", meta={})
    db.add(lesson_block)
    db.flush()

    # A pre-existing blueprint segment that WILL be regenerated/replaced by the redraft.
    old_theory = Block(kind="segment", title="Old Theory", body="stale", order=0,
                       parent_id=lesson_block.id, language="el", meta={"section": "theory"})
    # A custom segment surgically added by add_segment (Task 1) — must SURVIVE.
    custom = Block(kind="segment", title="Bonus bit", body="", order=1,
                  parent_id=lesson_block.id, language="el",
                  meta={"custom": True, "section": "custom:bonus-bit",
                        "segment_status": "queued", "segment_instruction": "explain X"})
    db.add_all([old_theory, custom])
    db.commit()

    bp = default_blueprint()
    lesson = _drafted_lesson()
    library = LibraryContext(text="", token_count=0, fits=True)
    m = measure(lesson, bp, teaching_minutes=40)
    persist_lesson(db, lesson_block, lesson, m, library, bp, qa_minutes=10, teaching_minutes=40)
    db.commit()
    db.expire_all()

    lesson_block = db.get(Block, lesson_block.id)
    segments = sorted(lesson_block.children, key=lambda b: b.order)

    keys = [s.meta.get("section") for s in segments]
    assert keys[:-1] == list(section_keys(bp))         # blueprint sections regenerated, in order
    assert keys[-1] == "custom:bonus-bit"              # custom segment appended AFTER them

    assert segments[-1].id == custom.id                # the SAME row — not deleted/recreated
    assert segments[-1].body == ""                     # untouched by the redraft
    assert segments[-1].title == "Bonus bit"

    assert [s.order for s in segments] == list(range(len(segments)))   # sequential order


# ---------------------------------------------------------------------------
# Task 2 (Spec A, generation side): generate_segment
# ---------------------------------------------------------------------------

class _FakeProvider:
    """Mirrors `test_curriculum_editing.py`'s own `_FakeProvider` — a fixed
    result, captured calls."""

    def __init__(self, result=None):
        self.result = result or {"title": "New bit", "body": "word word word word"}
        self.calls: list[dict] = []

    def guided_json(self, messages, schema, *, temperature=0.2, role="draft", max_tokens=None):
        self.calls.append({"messages": messages, "role": role})
        return self.result


def _sample_passage(page: int = 12) -> Passage:
    return Passage(
        text="Practice the pedal chain in the same order every time.",
        source_id=uuid.uuid4(), source_title="Pedalboard Handbook",
        page_no=page, page_id=uuid.uuid4(), score=0.8,
    )


def test_generate_segment_fills_body_grounds_and_recomputes_word_count(db_and_tree, monkeypatch):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    passage = _sample_passage()
    monkeypatch.setattr(segment_mod, "ground_topic", lambda *a, **k: [passage])
    provider = _FakeProvider(result={
        "title": "Εργασίες για το σπίτι", "body": "word word word word",
    })
    monkeypatch.setattr(segment_mod, "get_provider", lambda: provider)

    seg3 = Block(kind="segment", title="Bonus: Pedal chains", body="", order=2,
                 parent_id=lesson.id, language="el",
                 meta={"segment_status": "queued",
                       "segment_instruction": "explain pedal chaining order"})
    db.add(seg3)
    db.commit()

    generate_segment(db, seg3)
    db.commit()

    assert seg3.title == "Εργασίες για το σπίτι"
    assert seg3.body == "word word word word"
    assert seg3.meta["segment_status"] == "done"
    assert "segment_instruction" not in seg3.meta
    assert seg3.meta["citations"] == [
        {"source_id": str(passage.source_id), "source_title": "Pedalboard Handbook", "page": 12},
    ]

    db.refresh(lesson)
    assert lesson.meta["word_count"] == 5 + 3 + 4       # seg1 (5) + seg2 (3) + seg3 (4)
    assert lesson.meta["meets_floor"] is True            # 12 >= floor_words=10

    sent = " ".join(m["content"] for m in provider.calls[0]["messages"])
    assert "L1" in sent, "the lesson's own title must reach the model"
    assert "Theory" in sent and "word word word word word" in sent, (
        "a sibling segment's title and body must reach the model, for consistency"
    )
    assert "explain pedal chaining order" in sent, "the tutor's instruction must reach the model"
    assert "Pedalboard Handbook" in sent, "the grounded passage must reach the model"


def test_generate_segment_propagates_the_providers_error_and_leaves_the_segment_untouched(
    db_and_tree, monkeypatch,
):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    monkeypatch.setattr(segment_mod, "ground_topic", lambda *a, **k: [])

    def _boom(*a, **k):
        raise LLMError("upstream", "the model is down")

    provider = _FakeProvider()
    provider.guided_json = _boom
    monkeypatch.setattr(segment_mod, "get_provider", lambda: provider)

    seg3 = Block(kind="segment", title="Bonus", body="", order=2, parent_id=lesson.id,
                 language="el", meta={"segment_status": "queued", "segment_instruction": "x"})
    db.add(seg3)
    db.commit()

    with pytest.raises(LLMError):
        generate_segment(db, seg3)

    assert seg3.body == ""
    assert seg3.meta["segment_status"] == "queued"
    assert seg3.meta["segment_instruction"] == "x"


# ---------------------------------------------------------------------------
# Task 2, job-level: run_curriculum_revise_job's per-segment generation loop +
# conditional draft chaining
# ---------------------------------------------------------------------------

def test_job_generates_queued_segments_and_does_not_chain_a_segment_only_plan(
    db_and_tree, monkeypatch,
):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    monkeypatch.setattr(segment_mod, "ground_topic", lambda *a, **k: [])
    monkeypatch.setattr(segment_mod, "get_provider", lambda: _FakeProvider())
    draft_calls: list[uuid.UUID] = []
    monkeypatch.setattr(revise_job, "run_curriculum_draft_job", lambda jid: draft_calls.append(jid))

    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Bonus",
         "instruction": "explain pedal chaining order", "reason": "r"},
    ]}
    job = GenerationJob(kind="curriculum_revise", status="pending",
                        params={"root_id": str(course.id), "instruction": "go", "plan": plan})
    db.add(job)
    db.commit()
    job_id = job.id

    run_curriculum_revise_job(job_id)

    db.expire_all()
    job = db.get(GenerationJob, job_id)
    assert job.status == "succeeded"
    assert job.progress["segments_total"] == 1
    assert job.progress["segments_done"] == 1
    assert job.progress["segments_failed"] == 0
    assert "draft_job_id" not in job.progress          # nothing was chained

    assert draft_calls == []
    draft_rows = db.scalar(
        select(func.count(GenerationJob.id)).where(GenerationJob.kind == "curriculum_draft")
    )
    assert draft_rows == 0

    new_seg = _children(db, lesson.id, "segment")[-1]
    assert new_seg.meta["segment_status"] == "done"
    assert new_seg.body == "word word word word"


def test_job_chains_the_draft_when_a_lesson_was_also_queued(db_and_tree, monkeypatch):
    """(c)'s second half: a plan with a `modify_lesson` (queuing a LESSON, not
    just a segment) still chains the ordinary draft fan-out — the conditional
    only skips the chain when NO lesson ended up queued."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    monkeypatch.setattr(segment_mod, "ground_topic", lambda *a, **k: [])
    monkeypatch.setattr(segment_mod, "get_provider", lambda: _FakeProvider())
    draft_calls: list[uuid.UUID] = []
    monkeypatch.setattr(revise_job, "run_curriculum_draft_job", lambda jid: draft_calls.append(jid))

    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Bonus",
         "instruction": "explain pedal chaining order", "reason": "r"},
        {"op": "modify_lesson", "lesson_id": str(lesson.id), "instruction": "make it punchier",
         "reason": "r"},
    ]}
    job = GenerationJob(kind="curriculum_revise", status="pending",
                        params={"root_id": str(course.id), "instruction": "go", "plan": plan})
    db.add(job)
    db.commit()
    job_id = job.id

    run_curriculum_revise_job(job_id)

    db.expire_all()
    job = db.get(GenerationJob, job_id)
    assert job.status == "succeeded"
    assert job.progress["segments_total"] == 1
    assert job.progress["segments_done"] == 1
    assert job.progress["draft_job_id"] == str(draft_calls[0])

    assert len(draft_calls) == 1
    draft_rows = db.scalar(
        select(func.count(GenerationJob.id)).where(GenerationJob.kind == "curriculum_draft")
    )
    assert draft_rows == 1


def test_job_marks_a_failing_segment_failed_and_still_generates_the_rest(db_and_tree, monkeypatch):
    """Per-segment failure isolation: one bad segment must not fail the whole
    row or stop the others (mirrors `curriculum_draft`'s per-lesson isolation)."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    monkeypatch.setattr(segment_mod, "ground_topic", lambda *a, **k: [])

    class _SelectiveProvider:
        def guided_json(self, messages, schema, *, temperature=0.2, role="draft", max_tokens=None):
            sent = " ".join(m["content"] for m in messages)
            if "make it fail" in sent:
                raise LLMError("upstream", "the model is down")
            return {"title": "Good bit", "body": "word word word word"}

    monkeypatch.setattr(segment_mod, "get_provider", lambda: _SelectiveProvider())
    monkeypatch.setattr(revise_job, "run_curriculum_draft_job", lambda jid: None)

    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Will fail",
         "instruction": "make it fail", "reason": "r"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Will succeed",
         "instruction": "make it good", "reason": "r"},
    ]}
    job = GenerationJob(kind="curriculum_revise", status="pending",
                        params={"root_id": str(course.id), "instruction": "go", "plan": plan})
    db.add(job)
    db.commit()
    job_id = job.id

    run_curriculum_revise_job(job_id)

    db.expire_all()
    job = db.get(GenerationJob, job_id)
    assert job.status == "succeeded"                    # a segment failure must not fail the row
    assert job.progress["segments_total"] == 2
    assert job.progress["segments_done"] == 1
    assert job.progress["segments_failed"] == 1

    # By ORDER, not title: a successful generation is allowed to rename the
    # segment (`SEGMENT_SCHEMA` mirrors `REFINE_SCHEMA`'s "keep unless the
    # instruction says otherwise"), so the original "Will succeed" title is not
    # a stable handle once the fake provider has returned its own.
    failed, succeeded = _children(db, lesson.id, "segment")[2:]
    assert failed.meta["segment_status"] == "failed"
    assert failed.meta["segment_error"]
    assert failed.body == ""
    assert succeeded.meta["segment_status"] == "done"
    assert succeeded.body == "word word word word"


# ---------------------------------------------------------------------------
# Task 3 (Spec B): the planner SEES the blueprint and the segment tree
#
# The 2026-07-20 "structurally impossible plan" incident: the planner could see
# modules and lessons but not the segment tree beneath them, nor the blueprint a
# lesson is actually built from, so it proposed ops the current shape could not
# hold. This gives it both, and steers it toward the surgical ops (Tasks 1-2)
# instead of `modify_lesson` for a targeted change.
# ---------------------------------------------------------------------------

def test_compact_tree_text_includes_segment_id_lines(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    txt = revise.compact_tree_text(db, course)
    assert f"    [{seg1.id}] Theory" in txt
    assert f"    [{seg2.id}] Exercises" in txt
    # still no bodies — the token discipline the module docstring is built on
    assert "word word word word word" not in txt


def test_build_revise_messages_blueprint_block_shows_enabled_and_disabled_keys(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    bp = default_blueprint()
    for s in bp["sections"]:
        if s["key"] == "homework":
            s["enabled"] = False
    course.meta = {**(course.meta or {}), "blueprint": bp}
    db.commit()

    msgs = revise.build_revise_messages(
        course_title=course.title, brief=None, language="el",
        tree_text=revise.compact_tree_text(db, course), instruction="add a warm-up drill",
        retrieved=None, course_meta=course.meta,
    )
    user = next(m["content"] for m in msgs if m["role"] == "user")
    assert "warm_up (Ζέσταμα)" in user                       # an ENABLED key + label
    assert "disabled: homework (Εργασία για το σπίτι)" in user  # the DISABLED key + label


def test_revise_tail_prefers_surgical_ops_and_states_coupled_blueprint_guidance():
    """(c) + the 2026-07-20 hotfix: the rendered REVISE_TAIL (default, via the
    overrides resolver's fallback) tells the model to prefer the surgical ops
    over `modify_lesson`; that enabling/renaming a RECURRING section is
    `set_section_enabled` + per-lesson `add_segment` in the SAME plan (NOT
    `update_blueprint`, which is reserved for a full restructure); that every
    id must be the exact bracketed uuid, never an M1/L1-style shorthand; and
    that an impossible-under-the-current-blueprint request must be said, not
    hidden."""
    from app.prompts.overrides import resolve

    rendered = resolve(None, revise.REVISE_SLICE_ID, revise.REVISE_TAIL)
    low = rendered.lower()
    # surgical-ops preference over modify_lesson
    assert "add_segment" in low and "modify_lesson" in low
    assert "before modify_lesson" in low or "prefer" in low
    # a new recurring section = set_section_enabled + add_segment, same plan;
    # update_blueprint is reserved for a full restructure, not this coupling.
    assert "set_section_enabled" in low and "same plan" in low
    assert "update_blueprint" in low and "full restructure" in low
    # every id must be the exact bracketed uuid, never the M1/L1 position label
    assert "m1" in low and "l1" in low and "exact" in low
    # an impossible-under-the-current-blueprint request must be said, not hidden
    assert "impossible" in low and "summary" in low


def test_plan_revision_grounds_the_tree_with_the_courses_own_blueprint(monkeypatch):
    """(d)-adjacent: `plan_revision` (not just `build_revise_messages` directly)
    threads the live course's blueprint through, so a real revise call sees the
    same enabled/disabled split `validate_ops` already enforces at apply time —
    the planner and the validator can no longer disagree about what add_segment's
    section_key may target."""
    db = SessionLocal()
    try:
        bp = default_blueprint()
        for s in bp["sections"]:
            if s["key"] == "qa_prompts":
                s["enabled"] = False
        course = Block(kind="course", title="Tone", is_template=True, language="el",
                       meta={"brief": None, "source_ids": None, "blueprint": bp})
        db.add(course)
        db.flush()
        module = Block(kind="module", title="M1", parent_id=course.id, order=0,
                       language="el", meta={"objective": "m1"})
        db.add(module)
        db.flush()
        lesson = Block(kind="lesson", title="L1", parent_id=module.id, order=0,
                       language="el", meta={"objective": "l1"})
        db.add(lesson)
        db.commit()

        captured = {}

        def _fake_provider():
            class _P:
                def guided_json(self, messages, schema, role="plan"):
                    captured["messages"] = messages
                    return {"summary": "s", "ops": []}
            return _P()

        monkeypatch.setattr(revise, "get_provider", _fake_provider)
        monkeypatch.setattr(revise, "ground_topic", lambda db, topic, *, source_ids=None, k=5: [])

        revise.plan_revision(db, course.id, instruction="add a warm-up drill")

        sent = " ".join(m["content"] for m in captured["messages"])
        assert "disabled: qa_prompts" in sent
        assert "warm_up (" in sent and "enabled:" in sent
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Task 4: compute_impact — pure, server-computed plan impact
# ---------------------------------------------------------------------------

def test_compute_impact_mixed_plan_counts_each_bucket():
    """One op per bucket, including insert_module's inline `lessons` array (2
    lessons -> +2 lessons_added on top of insert_lesson's +1) — every count and
    the derived `destructive` flag pinned exactly."""
    ops = [
        {"op": "modify_lesson", "lesson_id": "l1", "instruction": "x", "reason": "r"},
        {"op": "move_lesson", "lesson_id": "l2", "to_module_id": "m1", "reason": "r"},
        {"op": "add_segment", "lesson_id": "l1", "title": "t", "instruction": "i", "reason": "r"},
        {"op": "add_segment", "lesson_id": "l1", "title": "t2", "instruction": "i2", "reason": "r"},
        {"op": "edit_segment", "segment_id": "s1", "instruction": "i", "reason": "r"},
        {"op": "remove_segment", "segment_id": "s2", "reason": "r"},
        {"op": "remove_lesson", "lesson_id": "l3", "reason": "r"},
        {"op": "insert_lesson", "module_id": "m1", "title": "t", "objective": "o", "reason": "r"},
        {"op": "insert_module", "title": "t", "objective": "o", "tier": "library",
         "lessons": [{"title": "a", "objective": "oa"}, {"title": "b", "objective": "ob"}],
         "reason": "r"},
        {"op": "update_blueprint", "blueprint": {}, "reason": "r"},
    ]
    assert revise.compute_impact(ops) == {
        "rewrites": 2,
        "segment_additions": 2,
        "segment_edits": 1,
        "segment_removals": 1,
        "lesson_removals": 1,
        "lessons_added": 3,       # insert_lesson (1) + insert_module.lessons (2)
        "blueprint_changed": True,
        "destructive": True,      # rewrites>0 (also lesson/segment removals>0)
    }


def test_compute_impact_surgical_only_plan_is_not_destructive():
    ops = [
        {"op": "add_segment", "lesson_id": "l1", "title": "t", "instruction": "i", "reason": "r"},
        {"op": "edit_segment", "segment_id": "s1", "instruction": "i", "reason": "r"},
    ]
    impact = revise.compute_impact(ops)
    assert impact["destructive"] is False
    assert impact["rewrites"] == 0
    assert impact["lesson_removals"] == 0
    assert impact["segment_removals"] == 0
    assert impact["segment_additions"] == 1
    assert impact["segment_edits"] == 1


def test_compute_impact_insert_module_without_lessons_list_adds_zero_lessons():
    """A TIER_GAP insert_module (apply_revision fills it with gap_body, no
    lesson blocks) carries no `lessons` array — must count 0, not crash on the
    missing key, and must not itself count as destructive."""
    ops = [{"op": "insert_module", "title": "t", "objective": "o", "tier": "gap", "reason": "r"}]
    impact = revise.compute_impact(ops)
    assert impact["lessons_added"] == 0
    assert impact["destructive"] is False


def test_compute_impact_empty_plan_is_all_zero_and_not_destructive():
    assert revise.compute_impact([]) == {
        "rewrites": 0, "segment_additions": 0, "segment_edits": 0, "segment_removals": 0,
        "lesson_removals": 0, "lessons_added": 0, "blueprint_changed": False, "destructive": False,
    }


# ---------------------------------------------------------------------------
# Task 6 (Spec D): modify_lesson threads the lesson's LIVE content into the
# re-draft prompt instead of redrafting from a blank page.
#
# `revise.py`'s `modify_lesson` branch already folds the tutor's instruction into
# the lesson's volatile `objective` (`jobs/curriculum_draft.py:_draft_one`), and
# used to stash the lesson's top-level `body` as `meta.prev_body` — a write
# nothing ever read (that top-level `body` is only ever the draft's one-line
# `summary`, per `persist_lesson`, never the lesson's real content). This threads
# the lesson's actual SEGMENTS — what the tutor is really looking at — into the
# prompt as `revise_current`, and drops the dead write.
# ---------------------------------------------------------------------------

def _revise_lesson_ctx():
    from app.curriculum.draft import LessonContext

    return LessonContext(
        lesson_title="Το σχήμα C", lesson_objective="Βρες τη ρίζα.",
        module_title="CAGED", module_objective="Βλέπε το μπράτσο σε σχήματα.",
        course_title="Τόνος", tier="library", position="lesson 1 of 1, module 1 of 1",
        minutes=50, teaching_minutes=40, target_words=2200, floor_words=1600,
    )


def test_build_lesson_messages_is_byte_identical_without_revise_current():
    """Spec D regression pin: no `revise_current` -> the lesson prompt is EXACTLY
    today's. Same discipline as test_planning_chat.py's
    test_outline_prompt_byte_identical_without_planning_brief."""
    kwargs = dict(
        ctx=_revise_lesson_ctx(),
        library=LibraryContext(text="", token_count=0, fits=True),
        language="el", student_brief=None, course_brief=None,
    )
    baseline = draft_mod.build_lesson_messages(**kwargs)
    with_default = draft_mod.build_lesson_messages(**kwargs, revise_current=None)
    assert baseline == with_default


def test_build_lesson_messages_renders_the_current_content_when_revising():
    ctx = _revise_lesson_ctx()
    msgs = draft_mod.build_lesson_messages(
        ctx=ctx, library=LibraryContext(text="", token_count=0, fits=True),
        language="el", student_brief=None, course_brief=None,
        revise_current={"theory": "Το σχήμα C ξεκινάει στην πέμπτη χορδή, τρίτο τάστο."},
    )
    joined = " ".join(m["content"] for m in msgs)
    assert "Το σχήμα C ξεκινάει στην πέμπτη χορδή, τρίτο τάστο." in joined


def _draft_plan(lesson_id, *, blueprint=None) -> dict:
    """The minimal `plan` dict `jobs/curriculum_draft.py:_draft_one` needs,
    mirroring `test_curriculum_draft_job.py`'s own plan shape."""
    return {
        "library": LibraryContext(text="", token_count=0, fits=True),
        "student_brief": None,
        "prompts": None,
        "blueprint": blueprint if blueprint is not None else default_blueprint(),
        "course_brief": None,
        "source_ids": None,
        "positions": {str(lesson_id): "lesson 1 of 1, module 1 of 1"},
        "minutes_per_lesson": 50,
        "teaching_minutes": 40,
        "qa_minutes": 10,
        "target_words": 2200,
        "floor_words": 10,
    }


class _RevisingFakeProvider:
    """Captures the messages of the FIRST `guided_json` call and always returns a
    schema-valid, well-over-floor lesson so no deepen pass fires and the captured
    call stays the one under test."""

    def __init__(self):
        self.calls: list[list[dict]] = []

    def guided_json(self, messages, schema, *, temperature=0.2, role="draft", max_tokens=None):
        self.calls.append(messages)
        return _drafted_lesson()


def test_draft_one_threads_the_lessons_live_segments_into_the_revise_prompt(
    db_and_tree, monkeypatch,
):
    """The failing-test-first case from the brief: a `modify_lesson` re-draft
    (revise_instruction set, live segments with real bodies) must see the
    lesson's CURRENT content, not just the instruction."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    seg1.body = "The blues scale starts on the root note E, third fret."
    db.commit()

    provider = _RevisingFakeProvider()
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    lesson.meta = {**(lesson.meta or {}), "draft_status": "queued",
                   "revise_instruction": "make it punchier"}
    db.commit()

    fanout_mod._draft_one(lesson.id, _draft_plan(lesson.id))

    assert provider.calls, "the model was never called — the lesson could not be prepared"
    sent = " ".join(m["content"] for m in provider.calls[0])
    assert "The blues scale starts on the root note E, third fret." in sent
    assert "make it punchier" in sent

    db.expire_all()
    lesson = db.get(Block, lesson.id)
    assert lesson.meta["draft_status"] == "ready"


def test_draft_one_includes_custom_segments_in_the_revise_content(db_and_tree, monkeypatch):
    """Custom segments (meta.custom, added surgically by add_segment) are part of
    the lesson the tutor knows — they must reach the re-draft prompt too, not
    just the blueprint sections."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    custom = Block(kind="segment", title="Bonus: alt tunings", order=2, parent_id=lesson.id,
                   language="el", meta={"custom": True, "section": "custom:bonus"},
                   body="Drop D makes power chords a one-finger barre.")
    db.add(custom)
    db.commit()

    provider = _RevisingFakeProvider()
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    lesson.meta = {**(lesson.meta or {}), "draft_status": "queued",
                   "revise_instruction": "tighten the theory section"}
    db.commit()

    fanout_mod._draft_one(lesson.id, _draft_plan(lesson.id))

    assert provider.calls
    sent = " ".join(m["content"] for m in provider.calls[0])
    assert "Drop D makes power chords a one-finger barre." in sent


def test_draft_one_without_a_revise_instruction_sends_no_current_content(db_and_tree, monkeypatch):
    """An ordinary (non-revise) redraft — no `revise_instruction` — must not
    render the revise block at all, even though the lesson already has
    segments with real bodies. Byte-identical to today's plain redraft."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    seg1.body = "A sentence that must NOT reach the prompt without an instruction."
    db.commit()

    provider = _RevisingFakeProvider()
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    lesson.meta = {**(lesson.meta or {}), "draft_status": "queued"}   # no revise_instruction
    db.commit()

    fanout_mod._draft_one(lesson.id, _draft_plan(lesson.id))

    assert provider.calls
    sent = " ".join(m["content"] for m in provider.calls[0])
    assert "A sentence that must NOT reach the prompt without an instruction." not in sent


def test_revise_current_body_passes_short_bodies_through_unchanged():
    """Under the cap: stripped, but otherwise untouched — no marker appended."""
    body = "  Το σχήμα C ξεκινάει στην πέμπτη χορδή.  "
    out = fanout_mod._revise_current_body(body)
    assert out == body.strip()
    assert fanout_mod.REVISE_CURRENT_TRUNCATION_MARKER not in out


def test_revise_current_body_truncates_at_2000_and_appends_the_keep_directive():
    """Over the cap: cut at exactly 2000 chars of the original body, with the
    Greek "keep the rest as-is" marker appended — the model must be TOLD the
    tail was cut, not left to guess the section just ends mid-sentence."""
    tail = "ΟΥΡΑ_ΠΟΥ_ΔΕΝ_ΠΡΕΠΕΙ_ΝΑ_ΠΕΡΑΣΕΙ"
    body = ("Α" * fanout_mod.REVISE_CURRENT_CHAR_LIMIT) + tail

    out = fanout_mod._revise_current_body(body)

    assert out.endswith(fanout_mod.REVISE_CURRENT_TRUNCATION_MARKER)
    kept = out[: -len(fanout_mod.REVISE_CURRENT_TRUNCATION_MARKER)]
    assert len(kept) == fanout_mod.REVISE_CURRENT_CHAR_LIMIT
    assert kept == "Α" * fanout_mod.REVISE_CURRENT_CHAR_LIMIT
    assert tail not in out


def test_draft_one_excludes_a_freshly_queued_empty_segment_from_revise_current(
    db_and_tree, monkeypatch,
):
    """`add_segment` files a placeholder with `body=""` until `generate_segment`
    fills it (a later, separate job). A `modify_lesson` re-draft that lands
    before that happens must not hand the model an empty section — there is
    nothing yet to preserve, and an empty entry would only confuse it about
    what "keep the rest" refers to."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    seg1.body = "The blues scale starts on the root note E, third fret."
    db.commit()
    placeholder = Block(
        kind="segment", title="Bonus: new section", order=2, parent_id=lesson.id,
        language="el", meta={"segment_status": "queued", "segment_instruction": "write it",
                              "custom": True, "section": "custom:bonus"},
        body="",
    )
    db.add(placeholder)
    db.commit()

    provider = _RevisingFakeProvider()
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    lesson.meta = {**(lesson.meta or {}), "draft_status": "queued",
                   "revise_instruction": "make it punchier"}
    db.commit()

    fanout_mod._draft_one(lesson.id, _draft_plan(lesson.id))

    assert provider.calls
    sent = " ".join(m["content"] for m in provider.calls[0])
    assert "The blues scale starts on the root note E, third fret." in sent
    assert "custom:bonus" not in sent
    assert "Bonus: new section" not in sent


def test_draft_one_sends_the_keep_directive_when_a_section_is_over_the_cap(
    db_and_tree, monkeypatch,
):
    """A section body over `REVISE_CURRENT_CHAR_LIMIT` chars (routine for a
    normal ~300-450 word Greek section) must reach the model WITH the marker,
    and the un-sent tail must not leak through some other path."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    tail = "ΟΥΡΑ_ΠΟΥ_ΔΕΝ_ΠΡΕΠΕΙ_ΝΑ_ΦΤΑΣΕΙ_ΣΤΟ_ΜΟΝΤΕΛΟ"
    seg1.body = ("Β" * 2500) + tail
    db.commit()

    provider = _RevisingFakeProvider()
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    lesson.meta = {**(lesson.meta or {}), "draft_status": "queued",
                   "revise_instruction": "tighten it"}
    db.commit()

    fanout_mod._draft_one(lesson.id, _draft_plan(lesson.id))

    assert provider.calls
    sent = " ".join(m["content"] for m in provider.calls[0])
    # `revise_current` reaches the prompt via `json.dumps` (draft.py:257), which
    # escapes the marker's leading "\n" to a literal backslash-n — check the
    # marker's text, not its raw (pre-JSON-escaping) form.
    assert fanout_mod.REVISE_CURRENT_TRUNCATION_MARKER.lstrip("\n") in sent
    assert tail not in sent
