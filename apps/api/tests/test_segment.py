"""Integration test for app.curriculum.segment.segment_block: partitions a
content Block tree into delivery-plane "session" Blocks and persists them.

Builds a synthetic content tree directly via Block rows (course -> module ->
lesson) rather than driving a live guided_json generate_curriculum call - the
brief explicitly prefers this (guided_json generation is 49-179s/call, see
Plan 3 Task 2's report; segment_block itself makes no LLM call at all, so
there is nothing here that needs a live model). Mirrors test_curriculum_
schema.py's/test_curriculum_generate.py's DB-skip-guard + fresh-session
round-trip pattern. Not marked @pytest.mark.integration (that marker means
"hits live vLLM" per pyproject.toml - this module only hits the DB, same as
test_curriculum_schema.py).
"""
import pytest
from sqlalchemy import select, text

from app.curriculum.segment import _collect_leaves, segment_block
from app.db import Base, SessionLocal, engine
from app.models.block import Block
from app.models.student import Student

# Skip cleanly (not error) when no DB is reachable - mirrors test_curriculum_schema.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


# 6 modules x this lesson-length pattern = 1560 minutes total - a realistic
# stand-in for the ~1500-minute Task-2-generated course the brief describes.
_LESSON_MINUTES = [45, 30, 20, 15, 40, 25, 35, 50]
_MODULE_COUNT = 6
_TOTAL_MINUTES = _MODULE_COUNT * sum(_LESSON_MINUTES)


def _build_course(db, *, language: str = "en") -> Block:
    course = Block(
        kind="course", title="Full Guitar Mastery", order=0, language=language, is_template=True,
    )
    db.add(course)
    db.flush()

    for m in range(_MODULE_COUNT):
        module = Block(
            kind="module", title=f"Module {m + 1}", order=m, parent_id=course.id, language=language,
        )
        db.add(module)
        db.flush()
        for lesson_idx, minutes in enumerate(_LESSON_MINUTES):
            db.add(Block(
                kind="lesson", title=f"Module {m + 1} Lesson {lesson_idx + 1}", order=lesson_idx,
                parent_id=module.id, language=language, est_minutes=minutes,
            ))

    db.commit()
    return course


def _children(db, parent_id) -> list[Block]:
    return db.scalars(
        select(Block).where(Block.parent_id == parent_id).order_by(Block.order)
    ).all()


def test_segment_block_partitions_synthetic_course_into_ordered_delivery_sessions():
    assert _TOTAL_MINUTES == 1560  # sanity: matches the pure-unit test's fixture

    db = SessionLocal()
    try:
        course = _build_course(db)
        course_id = course.id

        session_ids = segment_block(db, course_id, session_minutes=50)
    finally:
        db.close()

    assert 24 <= len(session_ids) <= 36, f"expected ~24-36 sessions, got {len(session_ids)}"

    db2 = SessionLocal()
    try:
        sessions = [db2.get(Block, sid) for sid in session_ids]
        assert all(s is not None for s in sessions)
        assert all(s.plane == "delivery" for s in sessions)
        assert all(s.kind == "session" for s in sessions)
        assert all(s.student_id is None for s in sessions)  # unassigned/template plan
        assert [s.order for s in sessions] == list(range(len(sessions)))  # 0..N-1, ordered

        summed = sum(s.est_minutes for s in sessions)
        assert summed == _TOTAL_MINUTES  # exact: every leaf minute assigned exactly once
        assert abs(summed - _TOTAL_MINUTES) <= _TOTAL_MINUTES * 0.2  # brief's stated ±20% bound

        # All sessions hang off one shared delivery_root, itself a child of the course.
        delivery_root_id = sessions[0].parent_id
        assert all(s.parent_id == delivery_root_id for s in sessions)
        on_disk = _children(db2, delivery_root_id)
        assert [s.id for s in on_disk] == session_ids  # returned order matches persisted order

        delivery_root = db2.get(Block, delivery_root_id)
        assert delivery_root.plane == "delivery"
        assert delivery_root.kind == "delivery_root"
        assert delivery_root.parent_id == course_id

        # Sanity on titles/bodies: deterministic, non-empty, reference real content.
        assert all(s.title.startswith(f"Session {i + 1}:") for i, s in enumerate(sessions))
        assert all(s.body for s in sessions)
    finally:
        db2.close()

    # --- Re-segment at a much longer session length: idempotent replace ---
    db3 = SessionLocal()
    try:
        new_session_ids = segment_block(db3, course_id, session_minutes=120)
    finally:
        db3.close()

    assert len(new_session_ids) < len(session_ids), "a larger target must yield fewer sessions"
    assert set(new_session_ids).isdisjoint(session_ids), "re-segmenting must mint fresh session ids"

    db4 = SessionLocal()
    try:
        # The old 50-min-target sessions are gone, not just superseded/orphaned.
        for old_id in session_ids:
            assert db4.get(Block, old_id) is None

        new_sessions = [db4.get(Block, sid) for sid in new_session_ids]
        assert all(s is not None for s in new_sessions)
        assert all(s.plane == "delivery" and s.kind == "session" for s in new_sessions)
        assert [s.order for s in new_sessions] == list(range(len(new_sessions)))
        assert sum(s.est_minutes for s in new_sessions) == _TOTAL_MINUTES

        # Exactly one delivery_root under the course - reused, not duplicated.
        roots = db4.scalars(
            select(Block).where(
                Block.parent_id == course_id, Block.kind == "delivery_root", Block.plane == "delivery",
            )
        ).all()
        assert len(roots) == 1
        assert [c.id for c in _children(db4, roots[0].id)] == new_session_ids
    finally:
        db4.close()


def test_segment_block_scopes_sessions_to_student_and_leaves_template_untouched():
    """segment_block(student_id=...) must not disturb a template
    (student_id=None) delivery plan for the same course - each (course,
    student) combo gets its own delivery_root + session set.
    """
    db = SessionLocal()
    try:
        course = _build_course(db)
        course_id = course.id
        student = Student(name="Segment Test Student", preferred_language="en")
        db.add(student)
        db.commit()
        student_id = student.id

        template_ids = segment_block(db, course_id, session_minutes=50)
        # cadence_per_week is part of the interface but not yet persisted
        # anywhere (see segment.py's docstring) - passing a non-default
        # value here just proves it's accepted and doesn't change the
        # partition, guarding against a signature regression.
        student_ids = segment_block(
            db, course_id, session_minutes=50, cadence_per_week=3, student_id=student_id,
        )
    finally:
        db.close()

    assert set(template_ids).isdisjoint(student_ids)
    assert len(template_ids) == len(student_ids)  # same content/target -> same partition

    db2 = SessionLocal()
    try:
        assert all(db2.get(Block, sid).student_id is None for sid in template_ids)
        assert all(db2.get(Block, sid).student_id == student_id for sid in student_ids)

        # The template plan (student_id=None) must still be intact and
        # untouched by the student-scoped segment_block call above.
        assert all(db2.get(Block, sid) is not None for sid in template_ids)

        roots = db2.scalars(
            select(Block).where(Block.parent_id == course_id, Block.kind == "delivery_root")
        ).all()
        assert len(roots) == 2  # one template root, one for this student
    finally:
        db2.close()


def test_segment_block_never_treats_prior_delivery_sessions_as_new_content_leaves():
    """Regression guard for the exact failure mode _collect_leaves's
    plane=="content" filter exists to prevent: after a first segment_block
    call, the course has a delivery-plane child (delivery_root + sessions)
    sitting alongside its content-plane children. A second call on the same
    block_id must still see only the original ~1560 minutes of *content*,
    not an ever-growing total that also counts the previous run's sessions.
    """
    db = SessionLocal()
    try:
        course = _build_course(db)
        course_id = course.id

        segment_block(db, course_id, session_minutes=50)
        second_ids = segment_block(db, course_id, session_minutes=50)
        third_ids = segment_block(db, course_id, session_minutes=50)
    finally:
        db.close()

    # Same target, same content, called three times in a row -> identical
    # session count and total minutes every time (not growing).
    assert len(second_ids) == len(third_ids)

    db2 = SessionLocal()
    try:
        total = sum(db2.get(Block, sid).est_minutes for sid in third_ids)
        assert total == _TOTAL_MINUTES
    finally:
        db2.close()


def test_collect_leaves_falls_back_to_own_minutes_when_children_contribute_zero():
    """Regression test for the leaf-minute fallback (review fix): a lesson
    that HAS children (segments) whose own est_minutes are all zero/None
    used to contribute nothing anywhere - only a genuinely *childless* node
    was ever treated as a leaf, so a lesson like this (real content, a real
    est_minutes=50) silently vanished from pacing. The fix: a node with
    children is a pacing-leaf (using its OWN est_minutes) whenever its
    subtree contributes zero leaf-minutes; a node with at least one
    minute-bearing descendant still stays a pure container (no double
    counting - proved here by "Normal Lesson" contributing its 20 minutes
    exactly once, not 20 plus some container fallback on top).
    """
    db = SessionLocal()
    try:
        course = Block(
            kind="course", title="Fallback Course", order=0, language="en", is_template=True,
        )
        db.add(course)
        db.flush()

        module = Block(
            kind="module", title="Fallback Module", order=0, parent_id=course.id, language="en",
        )
        db.add(module)
        db.flush()

        # HAS children (two segments), but neither carries any minutes: one
        # explicit 0, one omitted (NULL) - the exact shape the bug dropped.
        lesson = Block(
            kind="lesson", title="Zero-Segment Lesson", order=0, parent_id=module.id,
            language="en", est_minutes=50,
        )
        db.add(lesson)
        db.flush()
        db.add(Block(
            kind="segment", title="Empty Segment A", order=0, parent_id=lesson.id,
            language="en", est_minutes=0,
        ))
        db.add(Block(
            kind="segment", title="Empty Segment B", order=1, parent_id=lesson.id, language="en",
        ))

        # A normal sibling lesson with no children at all - untouched
        # control, proving the ordinary childless-leaf case (and the
        # no-double-counting invariant) is unaffected by the fix.
        db.add(Block(
            kind="lesson", title="Normal Lesson", order=1, parent_id=module.id,
            language="en", est_minutes=20,
        ))
        db.commit()
        course_id = course.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        root = db2.get(Block, course_id)
        leaves = _collect_leaves(db2, root)
    finally:
        db2.close()

    assert {leaf.title: leaf.minutes for leaf in leaves} == {
        "Zero-Segment Lesson": 50,
        "Normal Lesson": 20,
    }
    assert sum(leaf.minutes for leaf in leaves) == 70  # 50 recovered + 20 normal, nothing double-counted

    # End-to-end through the real partitioner/segment_block: the fallback
    # lesson's 50 minutes must show up in the persisted sessions' total.
    db3 = SessionLocal()
    try:
        session_ids = segment_block(db3, course_id, session_minutes=50)
    finally:
        db3.close()

    db4 = SessionLocal()
    try:
        total = sum(db4.get(Block, sid).est_minutes for sid in session_ids)
        assert total == 70
    finally:
        db4.close()
