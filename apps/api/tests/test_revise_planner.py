"""D1a — the READ-ONLY revision planner.

Two properties are the whole point and are asserted here:

  * plan_revision is a PURE READ. After it runs, the tree is byte-identical —
    no db.add, no db.commit, no status flip (Global Constraint #1).
  * every *_id in a raw plan is resolved against the live tree, of the right
    kind, under THIS root, and an op that fails resolution is DROPPED, not
    applied (Global Constraint #2).

The provider is stubbed so no live model is hit, and `ground_topic` — the
targeted BM25/e5 retrieval `plan_revision` grounds through today (it replaced
the whole-library `build_curriculum_context`; see revise.py's module
docstring) — is stubbed to an empty list so the planner never touches the
real search/embedder. Mirrors test_agent_tools.py's DB-reachable skip guard +
create_all setup.

Every test wraps its body in try/finally (`db.close()` in finally): the
autouse `_truncate_all_tables` fixture in conftest.py TRUNCATEs every table
after each test, which needs an ACCESS EXCLUSIVE lock. A session left open
(e.g. by an assertion failure or a monkeypatch.setattr targeting an attribute
that no longer exists) sits idle-in-transaction and holds that lock forever,
hanging the truncate — and every test after it — rather than failing fast.
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


def _stub_ground_topic(monkeypatch):
    """plan_revision grounds via targeted retrieval (`ground_topic`, imported
    into `revise`'s own namespace), scoped to the course's source_ids — NOT
    `build_curriculum_context` (the whole-library path revise.py no longer
    calls; see its module docstring). An empty passage list is a legitimate
    "library is thin here" result the planner's citation-honesty directive
    already handles, so it is enough to keep this test hermetic."""
    monkeypatch.setattr(revise, "ground_topic",
                        lambda db, topic, *, source_ids=None, k=5: [])


def test_plan_revision_returns_validated_ops_and_mutates_nothing(monkeypatch):
    db = SessionLocal()
    try:
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
        _stub_ground_topic(monkeypatch)

        plan = revise.plan_revision(db, course.id, instruction="add a DS-1 lesson")

        assert plan["summary"]
        assert len(plan["ops"]) == 1                      # bogus module op dropped
        assert plan["ops"][0]["module_id"] == str(m.id)
        assert _snapshot(db) == before                    # constraint #1: nothing written
    finally:
        db.close()


def test_compact_tree_has_ids_titles_objectives_no_bodies():
    db = SessionLocal()
    try:
        course, m, l = _seed(db)
        txt = revise.compact_tree_text(db, course)
        assert str(m.id) in txt and str(l.id) in txt and "Tube Screamer" in txt
        assert "the TS-808 mid hump" not in txt           # body excluded (token discipline)
    finally:
        db.close()


def test_missing_root_and_wrong_kind_raise_reviseerror():
    db = SessionLocal()
    try:
        course, m, l = _seed(db)
        with pytest.raises(revise.ReviseError):
            revise.plan_revision(db, uuid.uuid4(), instruction="x")
        with pytest.raises(revise.ReviseError):
            revise.plan_revision(db, m.id, instruction="x")   # a module, not a course
    finally:
        db.close()


def test_validate_ops_resolves_move_and_remove_against_the_live_tree():
    db = SessionLocal()
    try:
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
    finally:
        db.close()


# ---------------------------------------------------------------------------
# update_blueprint — the controller-added op (2026-07-18)
# ---------------------------------------------------------------------------

def test_validate_ops_keeps_a_valid_update_blueprint():
    from app.curriculum.blueprint import default_blueprint
    db = SessionLocal()
    try:
        course, m, l = _seed(db)
        raw = {"summary": "restructure lessons", "ops": [
            {"op": "update_blueprint", "blueprint": default_blueprint(),
             "reason": "the tutor wants a warm-up first"},
        ]}
        out = revise.validate_ops(db, course.id, raw)
        assert len(out["ops"]) == 1
        assert out["ops"][0]["op"] == "update_blueprint"
    finally:
        db.close()


def test_validate_ops_drops_an_invalid_update_blueprint():
    db = SessionLocal()
    try:
        course, m, l = _seed(db)
        raw = {"summary": "s", "ops": [
            {"op": "update_blueprint", "blueprint": {"version": 999, "sections": []},
             "reason": "bad"},                                  # bad_version + no_sections
            {"op": "update_blueprint", "blueprint": {"not": "a blueprint"}, "reason": "bad"},
        ]}
        out = revise.validate_ops(db, course.id, raw)
        assert out["ops"] == []                                 # both dropped, logged
    finally:
        db.close()


# ---------------------------------------------------------------------------
# THE ONE REPAIR PASS (2026-07-20 hotfix) — the incident's fix #2: a plan that
# used to return `{"summary": <rosy>, "ops": []}` with no trace of what broke
# now gets ONE corrective re-prompt before `plan_revision` gives up.
# ---------------------------------------------------------------------------

class _SequentialFakeProvider:
    """Returns a DIFFERENT raw plan on each successive `guided_json` call —
    the first-pass (bad) plan, then the repaired (good) one — so the repair
    re-prompt's effect is observable, not just its trigger condition."""

    def __init__(self, raws):
        self.raws = list(raws)
        self.calls = 0

    def guided_json(self, messages, schema, role="plan"):
        self.calls += 1
        idx = min(self.calls - 1, len(self.raws) - 1)
        return copy.deepcopy(self.raws[idx])


def test_plan_revision_runs_one_repair_pass_and_keeps_the_fixed_plan(monkeypatch):
    """Mirrors the production incident: the first pass names the module by an
    M1-style shorthand (dropped — does not resolve), the repair pass names it
    by the real bracketed uuid and survives. Exactly TWO provider calls, and
    the FINAL plan is the repaired one, non-empty."""
    db = SessionLocal()
    try:
        course, m, l = _seed(db)
        bad_raw = {"summary": "add a DS-1 lesson", "ops": [
            {"op": "insert_lesson", "module_id": "M1", "title": "DS-1",
             "objective": "distortion", "reason": "gap after TS"},
        ]}
        good_raw = {"summary": "add a DS-1 lesson (fixed)", "ops": [
            {"op": "insert_lesson", "module_id": str(m.id), "title": "DS-1",
             "objective": "distortion", "reason": "gap after TS"},
        ]}
        provider = _SequentialFakeProvider([bad_raw, good_raw])
        monkeypatch.setattr(revise, "get_provider", lambda: provider)
        _stub_ground_topic(monkeypatch)

        plan = revise.plan_revision(db, course.id, instruction="add a DS-1 lesson")

        assert provider.calls == 2
        assert len(plan["ops"]) == 1
        assert plan["ops"][0]["module_id"] == str(m.id)
        assert plan["dropped"] == []          # the repaired pass kept everything
    finally:
        db.close()


def test_plan_revision_does_not_repair_when_nothing_was_dropped(monkeypatch):
    db = SessionLocal()
    try:
        course, m, l = _seed(db)
        raw = {"summary": "ok", "ops": [
            {"op": "insert_lesson", "module_id": str(m.id), "title": "DS-1",
             "objective": "distortion", "reason": "r"},
        ]}
        provider = _SequentialFakeProvider([raw])
        monkeypatch.setattr(revise, "get_provider", lambda: provider)
        _stub_ground_topic(monkeypatch)

        plan = revise.plan_revision(db, course.id, instruction="x")

        assert provider.calls == 1             # no repair spent
        assert plan["dropped"] == []
    finally:
        db.close()


def test_plan_revision_repair_diagnostics_reflect_only_the_final_pass(monkeypatch):
    """If the repaired plan is STILL bad, `plan_revision` does not loop — it
    returns whatever the (single) repair pass produced, and `dropped` names
    THAT pass's failure, not the first one's."""
    db = SessionLocal()
    try:
        course, m, l = _seed(db)
        bad_raw = {"summary": "s", "ops": [
            {"op": "insert_lesson", "module_id": "M1", "title": "DS-1",
             "objective": "d", "reason": "r"},
        ]}
        still_bad_raw = {"summary": "s2", "ops": [
            {"op": "insert_lesson", "module_id": "M1-again", "title": "DS-1",
             "objective": "d", "reason": "r"},
        ]}
        provider = _SequentialFakeProvider([bad_raw, still_bad_raw])
        monkeypatch.setattr(revise, "get_provider", lambda: provider)
        _stub_ground_topic(monkeypatch)

        plan = revise.plan_revision(db, course.id, instruction="x")

        assert provider.calls == 2              # exactly one repair, no loop
        assert plan["ops"] == []
        assert len(plan["dropped"]) == 1
        assert plan["dropped"][0]["op"]["module_id"] == "M1-again"   # the SECOND pass, not the first
    finally:
        db.close()
