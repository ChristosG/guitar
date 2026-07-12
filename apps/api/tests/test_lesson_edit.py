import pytest
from app.lessons.edit import add_session, merge_sessions, split_session
from app.models.block import Block


def _lesson_with_one_long_session(db):
    lesson = Block(kind="lesson", title="Barre Chords", plane="content", order=0)
    db.add(lesson); db.commit()
    s = Block(kind="session", title="Everything", parent_id=lesson.id, order=0, est_minutes=120)
    db.add(s); db.commit()
    for i, mins in enumerate([30, 30, 30, 30]):
        db.add(Block(kind="item", title=f"Item {i+1}", body=f"body {i+1}",
                     parent_id=s.id, order=i, est_minutes=mins))
    db.commit()
    return lesson, s


def test_split_cuts_one_session_into_several_by_minutes_and_keeps_every_item(db):
    lesson, session = _lesson_with_one_long_session(db)

    new_sessions = split_session(db, session.id, session_minutes=60)

    assert len(new_sessions) == 2                 # 4x30min at 60min/session
    assert all(s.kind == "session" for s in new_sessions)
    assert all(s.parent_id == lesson.id for s in new_sessions)
    # NOTHING may be lost in a split — this is the tutor's work
    titles = [i.title for s in new_sessions
              for i in db.query(Block).filter_by(parent_id=s.id).order_by(Block.order)]
    assert titles == ["Item 1", "Item 2", "Item 3", "Item 4"]
    # the original over-long session is gone, replaced by its parts
    assert db.get(Block, session.id) is None
    # ordering is contiguous under the lesson
    orders = [s.order for s in db.query(Block).filter_by(parent_id=lesson.id).order_by(Block.order)]
    assert orders == list(range(len(orders)))


def test_merge_folds_adjacent_sessions_into_one_preserving_item_order(db):
    lesson = Block(kind="lesson", title="L", plane="content", order=0)
    db.add(lesson); db.commit()
    a = Block(kind="session", title="A", parent_id=lesson.id, order=0, est_minutes=30)
    b = Block(kind="session", title="B", parent_id=lesson.id, order=1, est_minutes=45)
    db.add_all([a, b]); db.commit()
    db.add(Block(kind="item", title="a1", parent_id=a.id, order=0))
    db.add(Block(kind="item", title="b1", parent_id=b.id, order=0))
    db.commit()

    merged = merge_sessions(db, [a.id, b.id])

    assert merged.kind == "session"
    assert merged.est_minutes == 75                       # summed
    items = db.query(Block).filter_by(parent_id=merged.id).order_by(Block.order).all()
    assert [i.title for i in items] == ["a1", "b1"]       # order preserved across the seam
    assert db.query(Block).filter_by(parent_id=lesson.id).count() == 1


def test_merging_sessions_from_different_lessons_is_rejected(db):
    l1 = Block(kind="lesson", title="L1", plane="content", order=0)
    l2 = Block(kind="lesson", title="L2", plane="content", order=0)
    db.add_all([l1, l2]); db.commit()
    s1 = Block(kind="session", title="S1", parent_id=l1.id, order=0)
    s2 = Block(kind="session", title="S2", parent_id=l2.id, order=0)
    db.add_all([s1, s2]); db.commit()

    with pytest.raises(ValueError):
        merge_sessions(db, [s1.id, s2.id])


def test_add_session_appends_and_can_insert_after_a_given_session(db):
    lesson, first = _lesson_with_one_long_session(db)

    appended = add_session(db, lesson.id, title="Warm-up", est_minutes=10)
    assert appended.parent_id == lesson.id
    assert appended.order == 1

    inserted = add_session(db, lesson.id, title="Intro", est_minutes=5, after=first.id)
    sessions = db.query(Block).filter_by(parent_id=lesson.id).order_by(Block.order).all()
    assert [s.title for s in sessions] == ["Everything", "Intro", "Warm-up"]
    assert [s.order for s in sessions] == [0, 1, 2]


def test_merging_non_adjacent_sessions_is_rejected(db):
    """Merging sessions that are not contiguous in order must be rejected
    to prevent silent reordering of in-between sessions."""
    lesson = Block(kind="lesson", title="L", plane="content", order=0)
    db.add(lesson); db.commit()

    # Create three sessions at orders 0, 1, 2
    s0 = Block(kind="session", title="S0", parent_id=lesson.id, order=0, est_minutes=30)
    s1 = Block(kind="session", title="S1", parent_id=lesson.id, order=1, est_minutes=30)
    s2 = Block(kind="session", title="S2", parent_id=lesson.id, order=2, est_minutes=30)
    db.add_all([s0, s1, s2]); db.commit()

    # Attempt to merge s0 and s2 (skipping s1) should raise ValueError
    with pytest.raises(ValueError):
        merge_sessions(db, [s0.id, s2.id])

    # Verify s1 is untouched by the rejected merge
    unchanged_s1 = db.get(Block, s1.id)
    assert unchanged_s1 is not None
    assert unchanged_s1.title == "S1"
    assert unchanged_s1.order == 1
