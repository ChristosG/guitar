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


def _lesson_with_three_sessions(db):
    """[A(order=0), B(order=1), C(order=2)] — B has 2 items (60min total,
    splittable into 2 parts at 30min/session), A and C have 1 item each."""
    lesson = Block(kind="lesson", title="L", plane="content", order=0)
    db.add(lesson); db.commit()
    a = Block(kind="session", title="A", parent_id=lesson.id, order=0, est_minutes=30)
    b = Block(kind="session", title="B", parent_id=lesson.id, order=1, est_minutes=60)
    c = Block(kind="session", title="C", parent_id=lesson.id, order=2, est_minutes=30)
    db.add_all([a, b, c]); db.commit()
    db.add(Block(kind="item", title="a1", parent_id=a.id, order=0, est_minutes=30))
    db.add(Block(kind="item", title="b1", parent_id=b.id, order=0, est_minutes=30))
    db.add(Block(kind="item", title="b2", parent_id=b.id, order=1, est_minutes=30))
    db.add(Block(kind="item", title="c1", parent_id=c.id, order=0, est_minutes=30))
    db.commit()
    return lesson, a, b, c


def test_split_middle_session_inserts_parts_between_siblings_no_ties(db):
    """Splitting the MIDDLE session of [A,B,C] must yield [A, B1, B2, C] in
    that exact order, with contiguous orders 0,1,2,3 and NO order ties.
    Regression for Plan 10 Task 4 review: the old code assigned new sessions
    `order = original_order + i` without shifting C out of the way, so a new
    part and C could collide on the same `order` value — and since
    `_renormalise_order`'s tie-broken-by-DB-row-order re-sort isn't
    guaranteed to preserve C last, C could silently move before the new
    parts, reordering the tutor's untouched work."""
    lesson, a, b, c = _lesson_with_three_sessions(db)

    new_sessions = split_session(db, b.id, session_minutes=30)
    assert len(new_sessions) == 2  # b1, b2

    siblings = db.query(Block).filter_by(parent_id=lesson.id).order_by(Block.order).all()
    titles = [s.title for s in siblings]
    orders = [s.order for s in siblings]

    assert titles == ["A"] + [s.title for s in new_sessions] + ["C"]
    assert orders == [0, 1, 2, 3]
    assert len(orders) == len(set(orders))  # no ties
    assert titles[-1] == "C"  # C must stay LAST

    # no-work-lost invariant: re-query every item from the DB under its
    # (possibly new) parent
    item_titles = [
        i.title for s in siblings
        for i in db.query(Block).filter_by(parent_id=s.id).order_by(Block.order)
    ]
    assert item_titles == ["a1", "b1", "b2", "c1"]


def test_split_last_session_still_works(db):
    """Regression guard: splitting the LAST session ([A,B,C], split C) must
    still work exactly as before (nothing to shift after it)."""
    lesson, a, b, c = _lesson_with_three_sessions(db)
    # give C a second item so it's splittable into 2 parts
    db.add(Block(kind="item", title="c2", parent_id=c.id, order=1, est_minutes=30))
    db.commit()

    new_sessions = split_session(db, c.id, session_minutes=30)
    assert len(new_sessions) == 2  # c1, c2

    siblings = db.query(Block).filter_by(parent_id=lesson.id).order_by(Block.order).all()
    titles = [s.title for s in siblings]
    orders = [s.order for s in siblings]

    assert titles == ["A", "B"] + [s.title for s in new_sessions]
    assert orders == [0, 1, 2, 3]
    assert len(orders) == len(set(orders))

    item_titles = [
        i.title for s in siblings
        for i in db.query(Block).filter_by(parent_id=s.id).order_by(Block.order)
    ]
    assert item_titles == ["a1", "b1", "b2", "c1", "c2"]


def test_split_first_session_keeps_the_others_after_it(db):
    """Splitting the FIRST session ([A,B,C], split A) must keep B and C
    after it, in order, with no ties."""
    lesson, a, b, c = _lesson_with_three_sessions(db)
    # give A a second item so it's splittable into 2 parts
    db.add(Block(kind="item", title="a2", parent_id=a.id, order=1, est_minutes=30))
    db.commit()

    new_sessions = split_session(db, a.id, session_minutes=30)
    assert len(new_sessions) == 2  # a1, a2

    siblings = db.query(Block).filter_by(parent_id=lesson.id).order_by(Block.order).all()
    titles = [s.title for s in siblings]
    orders = [s.order for s in siblings]

    assert titles == [s.title for s in new_sessions] + ["B", "C"]
    assert orders == [0, 1, 2, 3]
    assert len(orders) == len(set(orders))

    item_titles = [
        i.title for s in siblings
        for i in db.query(Block).filter_by(parent_id=s.id).order_by(Block.order)
    ]
    assert item_titles == ["a1", "a2", "b1", "b2", "c1"]


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
