"""`prev_segments`: capturing a lesson before the AI replaces it, and putting
it back.

WHY A SNAPSHOT AND NOT `prev_body`. `modify_lesson` does not edit text. It flips
`draft_status` to `queued` and a background worker rewrites EVERY segment from
scratch against the blueprint — different count, different titles, different
sections. There is no single block whose `prev_body` could stand in for that, so
the "before" is the whole segment SET and it has to be captured at apply time or
it is gone.

RESTORE IS A TOGGLE, NOT A DEMOLITION, and that is the property most of this file
is about. Restoring stashes what it replaces, so the tutor flips between the two
versions and neither is ever destroyed — which is what lets the confirm say "the
current version is kept" honestly, and what keeps this consistent with the
standing rule that this app displaces rather than deletes.

Mirrors `test_surgical_revise.py`'s fixture/skip-guard/setup_module conventions.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.curriculum.blueprint import default_blueprint
from app.curriculum.refine import undo_refine
from app.curriculum.restore import restore_lesson_segments
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
import app.curriculum.revise as revise

client = TestClient(app)

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


def _segments(db, lesson_id):
    return db.scalars(
        select(Block)
        .where(Block.parent_id == lesson_id, Block.kind == "segment")
        .order_by(Block.order)
    ).all()


@pytest.fixture
def tree():
    """course -> module -> lesson -> 2 segments with real Greek prose."""
    db = SessionLocal()
    course = Block(kind="course", title="Ήχος", is_template=True, language="el",
                   meta={"brief": None, "source_ids": None,
                         "gap_policy": "general_knowledge", "blueprint": default_blueprint()})
    db.add(course); db.flush()
    module = Block(kind="module", title="Ενότητα 1", parent_id=course.id, order=0,
                   language="el", meta={"objective": "m1"})
    db.add(module); db.flush()
    lesson = Block(kind="lesson", title="Μάθημα 1", parent_id=module.id, order=0,
                   language="el", meta={"objective": "l1", "draft_status": "ready"})
    db.add(lesson); db.flush()
    seg1 = Block(kind="segment", title="Ζέσταμα", body="Ξεκίνα με ανοιχτές χορδές.",
                 order=0, parent_id=lesson.id, language="el", meta={"section": "theory"})
    seg2 = Block(kind="segment", title="Θεωρία", body="Ο ενισχυτής χρωματίζει τον ήχο.",
                 order=1, parent_id=lesson.id, language="el", meta={"section": "exercises"})
    db.add_all([seg1, seg2]); db.commit()
    yield db, course, lesson, seg1, seg2
    db.close()


def _modify(db, course, lesson, instruction="πιο αναλυτικά"):
    revise.apply_revision(db, course.id, {"summary": "s", "ops": [
        {"op": "modify_lesson", "lesson_id": str(lesson.id),
         "instruction": instruction, "reason": "r"},
    ]})


# ---- capture ---------------------------------------------------------------

def test_modify_lesson_snapshots_the_live_segments_before_requeueing(tree):
    db, course, lesson, seg1, seg2 = tree
    _modify(db, course, lesson)
    db.expire_all()

    fresh = db.get(Block, lesson.id)
    assert fresh.meta["draft_status"] == "queued"
    snap = fresh.meta["prev_segments"]
    assert [s["title"] for s in snap] == ["Ζέσταμα", "Θεωρία"]
    assert snap[0]["body"] == "Ξεκίνα με ανοιχτές χορδές."
    assert snap[1]["body"] == "Ο ενισχυτής χρωματίζει τον ήχο."
    assert snap[0]["section"] == "theory"


def test_the_snapshot_is_one_level_deep_and_the_second_rewrite_overwrites_it(tree):
    """Same rule `prev_body` follows. Two levels is version control, which is a
    different feature with a different UI."""
    db, course, lesson, seg1, seg2 = tree
    _modify(db, course, lesson)
    db.expire_all()
    first = db.get(Block, lesson.id).meta["prev_segments"]

    # The worker has since rewritten the segments; simulate that, then revise again.
    for seg in _segments(db, lesson.id):
        seg.body = "Ξαναγραμμένο κείμενο."
    db.commit()
    _modify(db, course, lesson, "άλλαξέ το ξανά")
    db.expire_all()

    second = db.get(Block, lesson.id).meta["prev_segments"]
    assert len(second) == 2
    assert second[0]["body"] == "Ξαναγραμμένο κείμενο."
    assert second != first


def test_a_lesson_with_no_segments_snapshots_an_empty_list_not_a_crash(tree):
    db, course, lesson, seg1, seg2 = tree
    for seg in _segments(db, lesson.id):
        db.delete(seg)
    db.commit()

    _modify(db, course, lesson)
    db.expire_all()
    assert db.get(Block, lesson.id).meta["prev_segments"] == []


def test_prev_segments_does_not_disturb_the_per_block_undo(tree):
    """`undo_refine` restores a BODY. A lesson's body is not its segments, and
    the two mechanisms must not see each other."""
    db, course, lesson, seg1, seg2 = tree
    _modify(db, course, lesson)
    db.expire_all()

    fresh = db.get(Block, lesson.id)
    assert undo_refine(fresh) is False          # nothing stashed for the body path
    assert "prev_segments" in fresh.meta        # ...and the snapshot survived it


# ---- restore ---------------------------------------------------------------

def test_restore_puts_the_previous_lesson_back(tree):
    db, course, lesson, seg1, seg2 = tree
    _modify(db, course, lesson)
    db.commit()

    # The worker rewrites: different count, different titles — the real shape.
    for seg in _segments(db, lesson.id):
        db.delete(seg)
    db.flush()
    db.add(Block(kind="segment", title="Νέο", body="Εντελώς νέο κείμενο.", order=0,
                 parent_id=lesson.id, language="el", meta={"segment_status": "ready"}))
    db.commit()

    assert restore_lesson_segments(db, db.get(Block, lesson.id)) is True
    db.commit()
    db.expire_all()

    back = _segments(db, lesson.id)
    assert [s.title for s in back] == ["Ζέσταμα", "Θεωρία"]
    assert back[0].body == "Ξεκίνα με ανοιχτές χορδές."
    assert [s.order for s in back] == [0, 1]


def test_restore_is_a_toggle_and_never_destroys_either_version(tree):
    """THE PROPERTY THE WHOLE DESIGN RESTS ON. Restore stashes what it replaced,
    so a second restore returns the AI's version. Flip as often as you like;
    nothing is lost, which is why the confirm can honestly say so."""
    db, course, lesson, seg1, seg2 = tree
    _modify(db, course, lesson)
    db.commit()

    for seg in _segments(db, lesson.id):
        db.delete(seg)
    db.flush()
    db.add(Block(kind="segment", title="Νέο", body="Η έκδοση του AI.", order=0,
                 parent_id=lesson.id, language="el", meta={"segment_status": "ready"}))
    db.commit()

    restore_lesson_segments(db, db.get(Block, lesson.id)); db.commit(); db.expire_all()
    assert [s.title for s in _segments(db, lesson.id)] == ["Ζέσταμα", "Θεωρία"]

    restore_lesson_segments(db, db.get(Block, lesson.id)); db.commit(); db.expire_all()
    assert [s.title for s in _segments(db, lesson.id)] == ["Νέο"]
    assert _segments(db, lesson.id)[0].body == "Η έκδοση του AI."

    restore_lesson_segments(db, db.get(Block, lesson.id)); db.commit(); db.expire_all()
    assert [s.title for s in _segments(db, lesson.id)] == ["Ζέσταμα", "Θεωρία"]


def test_restored_segments_come_back_ready_not_queued(tree):
    """A restored segment marked `queued` would be rewritten by the very next
    drain — the tutor would restore, and the AI would immediately undo him."""
    db, course, lesson, seg1, seg2 = tree
    _modify(db, course, lesson)
    db.commit()
    for seg in _segments(db, lesson.id):
        db.delete(seg)
    db.commit()

    restore_lesson_segments(db, db.get(Block, lesson.id)); db.commit(); db.expire_all()
    assert all((s.meta or {}).get("segment_status") == "ready" for s in _segments(db, lesson.id))


def test_restore_recomputes_the_word_count(tree):
    db, course, lesson, seg1, seg2 = tree
    _modify(db, course, lesson)
    db.commit()
    for seg in _segments(db, lesson.id):
        db.delete(seg)
    db.flush()
    db.add(Block(kind="segment", title="Μικρό", body="Μία.", order=0,
                 parent_id=lesson.id, language="el", meta={"segment_status": "ready"}))
    db.commit()

    restore_lesson_segments(db, db.get(Block, lesson.id)); db.commit(); db.expire_all()
    assert db.get(Block, lesson.id).meta.get("word_count", 0) > 1


def test_restore_returns_false_when_there_is_nothing_stashed(tree):
    db, course, lesson, seg1, seg2 = tree
    assert restore_lesson_segments(db, db.get(Block, lesson.id)) is False


# ---- the route -------------------------------------------------------------

def test_the_route_restores_and_returns_the_lesson_tree(tree):
    db, course, lesson, seg1, seg2 = tree
    _modify(db, course, lesson)
    db.commit()
    for seg in _segments(db, lesson.id):
        db.delete(seg)
    db.flush()
    db.add(Block(kind="segment", title="Νέο", body="Η έκδοση του AI.", order=0,
                 parent_id=lesson.id, language="el", meta={"segment_status": "ready"}))
    db.commit()

    r = client.post(f"/blocks/{lesson.id}/restore-segments")
    assert r.status_code == 200
    assert [c["title"] for c in r.json()["children"]] == ["Ζέσταμα", "Θεωρία"]


def test_the_route_422s_when_nothing_was_ever_stashed(tree):
    """Not a 500 and not a silent no-op — the button should not have been
    offered, and if it was, the answer says why."""
    db, course, lesson, seg1, seg2 = tree
    r = client.post(f"/blocks/{lesson.id}/restore-segments")
    assert r.status_code == 422


def test_the_route_404s_on_anything_that_is_not_a_lesson(tree):
    """A segment id here would be a request to restore a thing that has no
    segment set. The generic /blocks routes handle segments; this door is
    lessons only."""
    db, course, lesson, seg1, seg2 = tree
    assert client.post(f"/blocks/{seg1.id}/restore-segments").status_code == 404
    assert client.post(f"/blocks/{course.id}/restore-segments").status_code == 404
    assert client.post(
        "/blocks/00000000-0000-0000-0000-000000000000/restore-segments"
    ).status_code == 404
