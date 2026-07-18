"""D1a — the READ-ONLY revision planner.

Two properties are the whole point and are asserted here:

  * plan_revision is a PURE READ. After it runs, the tree is byte-identical —
    no db.add, no db.commit, no status flip (Global Constraint #1).
  * every *_id in a raw plan is resolved against the live tree, of the right
    kind, under THIS root, and an op that fails resolution is DROPPED, not
    applied (Global Constraint #2).

The provider is stubbed so no live model is hit, and build_curriculum_context is
stubbed to a tiny LibraryContext so the planner never reads the real library.
Mirrors test_agent_tools.py's DB-reachable skip guard + create_all setup.
"""
import copy
import json
import uuid

import pytest
from sqlalchemy import text

from app.db import Base, SessionLocal, engine
from app.models.block import Block
import app.curriculum.revise as revise

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


class _FakeProvider:
    def __init__(self, raw):
        self.raw = raw

    def guided_json(self, messages, schema, role="plan"):
        return copy.deepcopy(self.raw)


def _seed(db):
    course = Block(kind="course", title="Tone 101", is_template=True, language="el",
                   meta={"brief": "for gigging players", "source_ids": None})
    db.add(course)
    db.flush()
    m = Block(kind="module", title="Overdrive", parent_id=course.id, order=0,
              language="el", meta={"objective": "od pedals", "tier": "library"})
    db.add(m)
    db.flush()
    l = Block(kind="lesson", title="Tube Screamer", parent_id=m.id, order=0,
              language="el", body="the TS-808 mid hump", meta={"objective": "ts basics"})
    db.add(l)
    db.commit()
    return course, m, l


def _snapshot(db):
    """(title, order, parent_id, body, meta) per block. `meta` is a JSON-stable
    copy (sort_keys, so key order never causes a false mismatch), not the live
    `dict` reference — plan_revision provably never writes meta today, but this
    is the read-only-contract test, and a reference would silently keep passing
    even if a future op reassigned `block.meta` to an equal-looking dict built
    fresh (Global Constraint #4's whole-dict-reassignment rule means the OLD
    object is never mutated in place, but asserting on a stable serialisation
    is the honest form of "nothing changed", not an artifact of Python identity)."""
    return {
        b.id: (b.title, b.order, b.parent_id, b.body, json.dumps(b.meta, sort_keys=True))
        for b in db.query(Block).all()
    }


def _stub_library(monkeypatch):
    from app.curriculum.corpus import LibraryContext
    monkeypatch.setattr(revise, "build_curriculum_context",
                        lambda db, sids: LibraryContext(text="", token_count=0, fits=True))


def test_plan_revision_returns_validated_ops_and_mutates_nothing(monkeypatch):
    db = SessionLocal()
    course, m, l = _seed(db)
    before = _snapshot(db)
    raw = {"summary": "add a DS-1 lesson",
           "ops": [
               {"op": "insert_lesson", "module_id": str(m.id), "after_lesson_id": str(l.id),
                "title": "DS-1", "objective": "distortion", "reason": "gap after TS"},
               {"op": "insert_lesson", "module_id": str(uuid.uuid4()),  # bogus module
                "title": "ghost", "objective": "x", "reason": "should be dropped"},
           ]}
    monkeypatch.setattr(revise, "get_provider", lambda: _FakeProvider(raw))
    _stub_library(monkeypatch)

    plan = revise.plan_revision(db, course.id, instruction="add a DS-1 lesson")

    assert plan["summary"]
    assert len(plan["ops"]) == 1                      # bogus module op dropped
    assert plan["ops"][0]["module_id"] == str(m.id)
    assert _snapshot(db) == before                    # constraint #1: nothing written
    db.close()


def test_compact_tree_has_ids_titles_objectives_no_bodies():
    db = SessionLocal()
    course, m, l = _seed(db)
    txt = revise.compact_tree_text(db, course)
    assert str(m.id) in txt and str(l.id) in txt and "Tube Screamer" in txt
    assert "the TS-808 mid hump" not in txt           # body excluded (token discipline)
    db.close()


def test_missing_root_and_wrong_kind_raise_reviseerror():
    db = SessionLocal()
    course, m, l = _seed(db)
    with pytest.raises(revise.ReviseError):
        revise.plan_revision(db, uuid.uuid4(), instruction="x")
    with pytest.raises(revise.ReviseError):
        revise.plan_revision(db, m.id, instruction="x")   # a module, not a course
    db.close()


def test_validate_ops_resolves_move_and_remove_against_the_live_tree():
    db = SessionLocal()
    course, m, l = _seed(db)
    m2 = Block(kind="module", title="Distortion", parent_id=course.id, order=1,
               language="el", meta={"objective": "dist pedals"})
    db.add(m2)
    db.commit()
    raw = {"summary": "s", "ops": [
        {"op": "move_lesson", "lesson_id": str(l.id), "to_module_id": str(m2.id), "reason": "r"},
        {"op": "move_lesson", "lesson_id": str(l.id), "to_module_id": str(uuid.uuid4()), "reason": "r"},
        {"op": "remove_lesson", "lesson_id": str(uuid.uuid4()), "reason": "r"},   # bogus
        {"op": "remove_lesson", "lesson_id": str(l.id), "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, raw)
    kept = [(o["op"], o.get("to_module_id")) for o in out["ops"]]
    assert kept == [("move_lesson", str(m2.id)), ("remove_lesson", None)]
    db.close()


# ---------------------------------------------------------------------------
# update_blueprint — the controller-added op (2026-07-18)
# ---------------------------------------------------------------------------

def test_validate_ops_keeps_a_valid_update_blueprint():
    from app.curriculum.blueprint import default_blueprint
    db = SessionLocal()
    course, m, l = _seed(db)
    raw = {"summary": "restructure lessons", "ops": [
        {"op": "update_blueprint", "blueprint": default_blueprint(),
         "reason": "the tutor wants a warm-up first"},
    ]}
    out = revise.validate_ops(db, course.id, raw)
    assert len(out["ops"]) == 1
    assert out["ops"][0]["op"] == "update_blueprint"
    db.close()


def test_validate_ops_drops_an_invalid_update_blueprint():
    db = SessionLocal()
    course, m, l = _seed(db)
    raw = {"summary": "s", "ops": [
        {"op": "update_blueprint", "blueprint": {"version": 999, "sections": []},
         "reason": "bad"},                                  # bad_version + no_sections
        {"op": "update_blueprint", "blueprint": {"not": "a blueprint"}, "reason": "bad"},
    ]}
    out = revise.validate_ops(db, course.id, raw)
    assert out["ops"] == []                                 # both dropped, logged
    db.close()
