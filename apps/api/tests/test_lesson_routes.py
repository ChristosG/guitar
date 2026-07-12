import uuid

from sqlalchemy import select

from app.models.block import Block


def _lesson_with_one_long_session(db, *, provenance=None):
    lesson = Block(
        kind="lesson", title="Barre Chords", plane="content", order=0,
        target_profile={"provenance": provenance} if provenance else None,
    )
    db.add(lesson); db.commit()
    s = Block(kind="session", title="Everything", parent_id=lesson.id, order=0, est_minutes=120)
    db.add(s); db.commit()
    for i, mins in enumerate([30, 30, 30, 30]):
        db.add(Block(kind="item", title=f"Item {i+1}", body=f"body {i+1}",
                     parent_id=s.id, order=i, est_minutes=mins))
    db.commit()
    db.refresh(lesson)
    db.refresh(s)
    return lesson, s


def test_list_lessons_surfaces_provenance_newest_first(client, db):
    older, _ = _lesson_with_one_long_session(
        db, provenance={"source_id": str(uuid.uuid4()), "page_no": 5},
    )
    newer, _ = _lesson_with_one_long_session(
        db, provenance={"source_id": str(uuid.uuid4()), "page_no": 21},
    )

    r = client.get("/lessons")
    assert r.status_code == 200
    body = r.json()
    ids = [row["id"] for row in body]
    assert ids.index(str(newer.id)) < ids.index(str(older.id))
    newest_row = next(row for row in body if row["id"] == str(newer.id))
    assert newest_row["provenance"]["page_no"] == 21


def test_list_lessons_excludes_curriculum_nested_lesson_nodes(client, db):
    """GET /lessons must surface only standalone authored lessons (root
    Blocks, parent_id IS NULL) — not the internal `Block(kind="lesson")`
    nodes nested inside a generated curriculum's module tree (which share
    the same `kind` and `plane`, but always have a parent). Real-data check
    (Plan 10 Task 4 review): every one of the 52 `kind='lesson'` rows in the
    live app DB has `parent_id IS NOT NULL` with parent kind='module' — i.e.
    they're all curriculum-internal nodes, none are standalone."""
    standalone, _ = _lesson_with_one_long_session(
        db, provenance={"source_id": str(uuid.uuid4()), "page_no": 21},
    )

    course = Block(kind="course", title="Course", plane="content", order=0)
    db.add(course); db.commit()
    module = Block(kind="module", title="Module", parent_id=course.id, plane="content", order=0)
    db.add(module); db.commit()
    nested_lesson = Block(kind="lesson", title="Nested", parent_id=module.id, plane="content", order=0)
    db.add(nested_lesson); db.commit()

    r = client.get("/lessons")
    assert r.status_code == 200
    body = r.json()
    ids = [row["id"] for row in body]
    assert str(standalone.id) in ids
    assert str(nested_lesson.id) not in ids
    # the standalone lesson's provenance still comes through
    standalone_row = next(row for row in body if row["id"] == str(standalone.id))
    assert standalone_row["provenance"]["page_no"] == 21


def test_get_lesson_returns_the_tree(client, db):
    lesson, session = _lesson_with_one_long_session(db)

    r = client.get(f"/lessons/{lesson.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(lesson.id)
    assert body["kind"] == "lesson"
    assert len(body["children"]) == 1
    assert len(body["children"][0]["children"]) == 4


def test_get_unknown_lesson_is_404(client):
    r = client.get(f"/lessons/{uuid.uuid4()}")
    assert r.status_code == 404


def test_split_route_keeps_every_item_and_returns_the_updated_tree(client, db):
    lesson, session = _lesson_with_one_long_session(db)

    r = client.post(
        f"/lessons/{lesson.id}/sessions/{session.id}/split",
        json={"session_minutes": 60},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(lesson.id)
    sessions = body["children"]
    assert len(sessions) == 2
    titles = [item["title"] for s in sessions for item in s["children"]]
    assert titles == ["Item 1", "Item 2", "Item 3", "Item 4"]
    # The route's handler runs on its own DB session (Depends(get_db)); this
    # test's `db` fixture is a SEPARATE session that already cached `session`
    # in its identity map when it created it above, and expire_on_commit is
    # False (app/db.py). `db.get()` would serve the stale identity-mapped row
    # (or, after `expire_all()`, raise ObjectDeletedError trying to refresh a
    # row that's actually gone) — a fresh `select()` bypasses the identity
    # map's cached row entirely and just asks the DB.
    assert db.scalar(select(Block).where(Block.id == session.id)) is None


def test_split_route_404s_when_session_belongs_to_a_different_lesson(client, db):
    lesson1, _ = _lesson_with_one_long_session(db)
    lesson2, session2 = _lesson_with_one_long_session(db)

    r = client.post(
        f"/lessons/{lesson1.id}/sessions/{session2.id}/split",
        json={"session_minutes": 60},
    )
    assert r.status_code == 404


def test_merge_route_folds_adjacent_sessions(client, db):
    lesson = Block(kind="lesson", title="L", plane="content", order=0)
    db.add(lesson); db.commit()
    a = Block(kind="session", title="A", parent_id=lesson.id, order=0, est_minutes=30)
    b = Block(kind="session", title="B", parent_id=lesson.id, order=1, est_minutes=45)
    db.add_all([a, b]); db.commit()
    db.add(Block(kind="item", title="a1", parent_id=a.id, order=0))
    db.add(Block(kind="item", title="b1", parent_id=b.id, order=0))
    db.commit()

    r = client.post(f"/lessons/{lesson.id}/sessions/merge", json={"session_ids": [str(a.id), str(b.id)]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["children"]) == 1
    merged = body["children"][0]
    assert merged["est_minutes"] == 75
    assert [i["title"] for i in merged["children"]] == ["a1", "b1"]


def test_merge_route_rejects_sessions_from_different_lessons(client, db):
    l1 = Block(kind="lesson", title="L1", plane="content", order=0)
    l2 = Block(kind="lesson", title="L2", plane="content", order=0)
    db.add_all([l1, l2]); db.commit()
    s1 = Block(kind="session", title="S1", parent_id=l1.id, order=0)
    s2 = Block(kind="session", title="S2", parent_id=l2.id, order=0)
    db.add_all([s1, s2]); db.commit()

    # s2 does not belong to l1 -> caught by the route's own membership check
    # before merge_sessions is ever called.
    r = client.post(f"/lessons/{l1.id}/sessions/merge", json={"session_ids": [str(s1.id), str(s2.id)]})
    assert r.status_code == 404


def test_add_session_route_appends_and_inserts_after(client, db):
    lesson, first = _lesson_with_one_long_session(db)

    r = client.post(f"/lessons/{lesson.id}/sessions", json={"title": "Warm-up", "est_minutes": 10})
    assert r.status_code == 200
    r2 = client.post(
        f"/lessons/{lesson.id}/sessions",
        json={"title": "Intro", "est_minutes": 5, "after": str(first.id)},
    )
    assert r2.status_code == 200
    titles = [s["title"] for s in r2.json()["children"]]
    assert titles == ["Everything", "Intro", "Warm-up"]


def test_add_session_route_404s_when_after_belongs_to_a_different_lesson(client, db):
    """The route should pre-check that the 'after' session belongs to the
    target lesson and return 404 if not, consistent with split/merge routes."""
    lesson1, session1 = _lesson_with_one_long_session(db)
    lesson2, session2 = _lesson_with_one_long_session(db)

    # Attempt to add a session after session2 (which belongs to lesson2, not lesson1)
    r = client.post(
        f"/lessons/{lesson1.id}/sessions",
        json={"title": "New", "est_minutes": 10, "after": str(session2.id)},
    )
    assert r.status_code == 404
