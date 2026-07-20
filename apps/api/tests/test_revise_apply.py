"""D1b — the apply engine: the ONE writer.

apply_revision executes an approved plan on one Session and commits ONCE
(Global Constraint #3). It reuses the commit-free cores of edit.add_lesson /
add_module and the new edit._move_block (cross-parent, renormalises BOTH
parents). New/changed lessons re-enter the draft queue (Constraint #4);
update_blueprint writes course.meta["blueprint"] and NEVER requeues a lesson
(re-drafting is the opt-in /redraft route only).
"""
import uuid

import pytest
from sqlalchemy import select, text

from app.db import Base, SessionLocal, engine
from app.models.block import Block
from app.curriculum import edit
import app.curriculum.revise as revise

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


def _children(db, parent_id, kind):
    return db.scalars(
        select(Block).where(Block.parent_id == parent_id, Block.kind == kind).order_by(Block.order)
    ).all()


@pytest.fixture
def db_and_tree():
    """M1: L1, L2   |   M2: L3."""
    db = SessionLocal()
    course = Block(kind="course", title="Tone", is_template=True, language="el",
                   meta={"brief": None, "source_ids": None,
                         "gap_policy": "general_knowledge",
                         "shape": {"minutes_per_lesson": 50}})
    db.add(course)
    db.flush()
    m1 = Block(kind="module", title="M1", parent_id=course.id, order=0, language="el",
               meta={"objective": "m1"})
    m2 = Block(kind="module", title="M2", parent_id=course.id, order=1, language="el",
               meta={"objective": "m2"})
    db.add_all([m1, m2])
    db.flush()
    l1 = Block(kind="lesson", title="L1", parent_id=m1.id, order=0, language="el",
               meta={"objective": "l1"})
    l2 = Block(kind="lesson", title="L2", parent_id=m1.id, order=1, language="el",
               meta={"objective": "l2"})
    l3 = Block(kind="lesson", title="L3", parent_id=m2.id, order=0, language="el",
               meta={"objective": "l3"})
    db.add_all([l1, l2, l3])
    db.commit()
    yield db, course, m1, m2, l1, l2, l3
    db.close()


def test_insert_lesson_after_positions_and_queues(db_and_tree):
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    plan = {"summary": "x", "ops": [
        {"op": "insert_lesson", "module_id": str(m1.id), "after_lesson_id": str(l1.id),
         "title": "NEW", "objective": "o", "reason": "r"}]}
    out = revise.apply_revision(db, course.id, plan)
    assert out == {"applied": 1, "root_id": str(course.id)}
    lessons = _children(db, m1.id, "lesson")
    assert [x.title for x in lessons] == ["L1", "NEW", "L2"]           # positional
    assert [x.order for x in lessons] == [0, 1, 2]                     # renormalised
    new = lessons[1]
    assert (new.meta or {})["draft_status"] == "queued"               # constraint #4


def test_insert_lesson_appends_when_no_after(db_and_tree):
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    revise.apply_revision(db, course.id, {"summary": "x", "ops": [
        {"op": "insert_lesson", "module_id": str(m1.id),
         "title": "TAIL", "objective": "o", "reason": "r"}]})
    assert [x.title for x in _children(db, m1.id, "lesson")] == ["L1", "L2", "TAIL"]


def test_move_lesson_across_modules_renormalises_both_parents(db_and_tree):
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    plan = {"summary": "x", "ops": [
        {"op": "move_lesson", "lesson_id": str(l2.id), "to_module_id": str(m2.id),
         "after_lesson_id": str(l3.id), "reason": "r"}]}
    revise.apply_revision(db, course.id, plan)
    assert [x.title for x in _children(db, m1.id, "lesson")] == ["L1"]
    assert [x.order for x in _children(db, m1.id, "lesson")] == [0]    # old parent closed
    m2_l = _children(db, m2.id, "lesson")
    assert [x.title for x in m2_l] == ["L3", "L2"]
    assert [x.order for x in m2_l] == [0, 1]                           # new parent contiguous
    moved = next(x for x in m2_l if x.title == "L2")
    assert moved.parent_id == m2.id and (moved.meta or {})["draft_status"] == "queued"


def test_move_lesson_within_the_same_module_reorders_cleanly(db_and_tree):
    """The validator permits `to_module_id` == the lesson's own module (a reorder).
    The block must not be inserted twice — orders stay a contiguous 0..n-1 run."""
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    revise.apply_revision(db, course.id, {"summary": "x", "ops": [
        {"op": "move_lesson", "lesson_id": str(l1.id), "to_module_id": str(m1.id),
         "after_lesson_id": str(l2.id), "reason": "r"}]})
    lessons = _children(db, m1.id, "lesson")
    assert [x.title for x in lessons] == ["L2", "L1"]
    assert [x.order for x in lessons] == [0, 1]


def test_remove_lesson_deletes_and_renormalises(db_and_tree):
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    revise.apply_revision(db, course.id, {"summary": "x", "ops": [
        {"op": "remove_lesson", "lesson_id": str(l1.id), "reason": "r"}]})
    assert [x.title for x in _children(db, m1.id, "lesson")] == ["L2"]
    assert [x.order for x in _children(db, m1.id, "lesson")] == [0]


def test_modify_lesson_requeues_with_instruction_and_does_not_stash_prev_body(db_and_tree):
    """`prev_body` used to be stashed here too — a dead write nothing ever read.
    `_draft_one` now threads the lesson's live SEGMENTS into the re-draft prompt
    instead (Spec D, `revise_current`), so this key must not reappear."""
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    l1.body = "old body"
    db.commit()
    revise.apply_revision(db, course.id, {"summary": "x", "ops": [
        {"op": "modify_lesson", "lesson_id": str(l1.id), "instruction": "harder", "reason": "r"}]})
    db.refresh(l1)
    assert (l1.meta or {})["draft_status"] == "queued"
    assert (l1.meta or {})["revise_instruction"] == "harder"
    assert "prev_body" not in (l1.meta or {})


def test_insert_module_queues_its_lessons_and_clamps_tier(db_and_tree):
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    revise.apply_revision(db, course.id, {"summary": "x", "ops": [
        {"op": "insert_module", "after_module_id": str(m1.id), "title": "M-new",
         "objective": "o", "tier": "library", "reason": "r",
         "lessons": [{"title": "a", "objective": "oa"}, {"title": "b", "objective": "ob"}]}]})
    mods = _children(db, course.id, "module")
    assert [x.title for x in mods] == ["M1", "M-new", "M2"]
    m_new = mods[1]
    assert m_new.meta["tier"] == "library"
    assert [x.title for x in _children(db, m_new.id, "lesson")] == ["a", "b"]
    assert all((x.meta or {})["draft_status"] == "queued" for x in _children(db, m_new.id, "lesson"))


def test_insert_module_under_library_only_becomes_a_gap_with_no_lessons(db_and_tree):
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    course.meta = {**(course.meta or {}), "gap_policy": "library_only"}
    db.commit()
    revise.apply_revision(db, course.id, {"summary": "x", "ops": [
        {"op": "insert_module", "title": "Gap topic", "objective": "o",
         "tier": "general_knowledge", "reason": "r",
         "lessons": [{"title": "a", "objective": "oa"}]}]})
    gap = _children(db, course.id, "module")[-1]
    assert gap.meta["tier"] == "gap"
    assert _children(db, gap.id, "lesson") == []      # a gap module gets NO lessons
    assert gap.body                                    # carries the honest gap body


def test_apply_is_atomic_all_or_nothing(db_and_tree, monkeypatch):
    """A raise partway through leaves the tree untouched (one transaction)."""
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    before = {b.id: (b.title, b.order, b.parent_id) for b in db.query(Block).all()}

    calls = {"n": 0}
    real = edit._add_lesson

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom on the second insert")
        return real(*a, **k)

    monkeypatch.setattr(edit, "_add_lesson", boom)
    plan = {"summary": "x", "ops": [
        {"op": "insert_lesson", "module_id": str(m1.id), "title": "A", "objective": "o", "reason": "r"},
        {"op": "insert_lesson", "module_id": str(m1.id), "title": "B", "objective": "o", "reason": "r"}]}
    with pytest.raises(Exception):
        revise.apply_revision(db, course.id, plan)
    db.rollback()
    after = {b.id: (b.title, b.order, b.parent_id) for b in db.query(Block).all()}
    assert after == before


def test_apply_revalidates_and_drops_a_bogus_id(db_and_tree):
    """Defence in depth: apply re-validates, so an op with an id that no longer
    resolves is dropped rather than applied — applied count reflects only the
    valid ops."""
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    out = revise.apply_revision(db, course.id, {"summary": "x", "ops": [
        {"op": "remove_lesson", "lesson_id": str(uuid.uuid4()), "reason": "bogus"},
        {"op": "remove_lesson", "lesson_id": str(l3.id), "reason": "r"}]})
    assert out["applied"] == 1
    assert _children(db, m2.id, "lesson") == []


def test_apply_drops_a_self_referential_move_lesson_and_applies_the_rest(db_and_tree):
    """A move_lesson whose after_lesson_id equals its own lesson_id is a
    degenerate "moved after itself" op. Before the fix it PASSED validate_ops
    (both ids individually resolve — they are just the SAME id), then
    edit._move_block raised StopIteration: `dest` excludes the block being
    moved, so `after` can never be found in it once `after == block_id`. That
    exception propagated out of apply_revision's `try` and rolled back the
    WHOLE approved plan — one degenerate model op sinking an otherwise-good
    revision. validate_ops now drops it like any other unresolved op, so the
    rest of the plan still applies and apply_revision never raises."""
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    plan = {"summary": "x", "ops": [
        {"op": "move_lesson", "lesson_id": str(l1.id), "after_lesson_id": str(l1.id),
         "to_module_id": str(m1.id), "reason": "moved after itself — degenerate"},
        {"op": "remove_lesson", "lesson_id": str(l3.id), "reason": "a valid op"}]}

    validated = revise.validate_ops(db, course.id, plan)
    assert [o["op"] for o in validated["ops"]] == ["remove_lesson"]   # self-move dropped

    out = revise.apply_revision(db, course.id, plan)                  # must not raise
    assert out["applied"] == 1
    assert _children(db, m2.id, "lesson") == []


# ---------------------------------------------------------------------------
# update_blueprint — controller op (2026-07-18): writes meta, requeues NOTHING
# ---------------------------------------------------------------------------

def test_update_blueprint_writes_meta_and_requeues_no_lesson(db_and_tree):
    from app.curriculum.blueprint import default_blueprint
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    bp = default_blueprint()
    out = revise.apply_revision(db, course.id, {"summary": "restructure", "ops": [
        {"op": "update_blueprint", "blueprint": bp, "reason": "warm-up first"}]})
    assert out["applied"] == 1
    db.refresh(course)
    assert course.meta["blueprint"]["sections"], "blueprint written to course.meta"
    # NO lesson was requeued — a blueprint change never auto-re-drafts.
    for lesson in (l1, l2, l3):
        db.refresh(lesson)
        assert (lesson.meta or {}).get("draft_status") in (None,), \
            "existing lessons keep their state — re-draft is the opt-in /redraft route"


def test_update_blueprint_with_an_invalid_blueprint_is_a_no_op(db_and_tree):
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    out = revise.apply_revision(db, course.id, {"summary": "s", "ops": [
        {"op": "update_blueprint", "blueprint": {"version": 999, "sections": []}, "reason": "bad"}]})
    assert out["applied"] == 0                          # dropped at re-validation
    db.refresh(course)
    assert "blueprint" not in (course.meta or {})       # nothing written
