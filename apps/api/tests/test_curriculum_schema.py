"""Schema round-trip tests for the curriculum plane: Block's new
target_profile/student_id/plane columns, and the new Assignment/Progress/
LessonLog models.

Mirrors test_models_roundtrip.py's pattern: write + commit in one session,
then read back in a FRESH session so it's a genuine DB round-trip (not the
identity-map object reused under expire_on_commit=False).
"""
import pytest
from sqlalchemy import text

from app.db import Base, SessionLocal, engine
from app.models.block import Block
from app.models.curriculum import Assignment, LessonLog, Progress
from app.models.student import Student

# Skip cleanly (not error) when no DB is reachable — mirrors test_models_roundtrip.py.
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


def test_curriculum_round_trip_fks_resolve_and_plane_defaults_to_content():
    db = SessionLocal()
    try:
        course = Block(
            kind="course",
            title="Beginner Guitar",
            order=0,
            language="en",
            is_template=True,
            target_profile={"level": "beginner", "age": 10},
        )
        db.add(course)
        db.flush()

        module = Block(
            kind="module", title="Open Chords", order=0, parent_id=course.id, language="en",
        )
        db.add(module)
        db.flush()

        student = Student(name="Alex Doe", preferred_language="en")
        db.add(student)
        db.flush()

        assignment = Assignment(student_id=student.id, curriculum_block_id=course.id)
        db.add(assignment)

        progress = Progress(student_id=student.id, block_id=module.id, status="practicing")
        db.add(progress)

        lesson = LessonLog(
            student_id=student.id,
            session_block_id=module.id,
            taught=True,
            notes="Covered E minor and G major.",
            homework="Practice chord changes daily.",
        )
        db.add(lesson)

        db.commit()
        course_id, module_id = course.id, module.id
        student_id = student.id
        assignment_id, progress_id, lesson_id = assignment.id, progress.id, lesson.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got_course = db2.get(Block, course_id)
        assert got_course is not None
        assert got_course.target_profile == {"level": "beginner", "age": 10}
        assert got_course.plane == "content"  # default, never set explicitly above
        assert got_course.student_id is None  # template course, not assigned to anyone

        got_module = db2.get(Block, module_id)
        assert got_module is not None
        assert got_module.parent_id == course_id  # recursive tree still works
        assert got_module.plane == "content"

        got_assignment = db2.get(Assignment, assignment_id)
        assert got_assignment is not None
        assert got_assignment.student_id == student_id
        assert got_assignment.curriculum_block_id == course_id
        assert got_assignment.created_at is not None

        got_progress = db2.get(Progress, progress_id)
        assert got_progress is not None
        assert got_progress.student_id == student_id
        assert got_progress.block_id == module_id
        assert got_progress.status == "practicing"
        assert got_progress.updated_at is not None

        got_lesson = db2.get(LessonLog, lesson_id)
        assert got_lesson is not None
        assert got_lesson.student_id == student_id
        assert got_lesson.session_block_id == module_id
        assert got_lesson.taught is True
        assert got_lesson.date is None
        assert got_lesson.notes == "Covered E minor and G major."
        assert got_lesson.homework == "Practice chord changes daily."
    finally:
        db2.close()


def test_block_plane_and_curriculum_columns_default_without_explicit_values():
    db = SessionLocal()
    try:
        b = Block(kind="topic", title="Plane-default check", order=0, language="en")
        db.add(b)
        db.commit()
        block_id = b.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(Block, block_id)
        assert got.plane == "content"
        assert got.target_profile is None
        assert got.student_id is None
    finally:
        db2.close()


def test_progress_status_defaults_to_not_started():
    db = SessionLocal()
    try:
        course = Block(kind="course", title="Status default check", order=0, language="en")
        db.add(course)
        db.flush()
        student = Student(name="Status Default Student")
        db.add(student)
        db.flush()
        progress = Progress(student_id=student.id, block_id=course.id)
        db.add(progress)
        db.commit()
        progress_id = progress.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(Progress, progress_id)
        assert got.status == "not_started"
    finally:
        db2.close()
