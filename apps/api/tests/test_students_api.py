"""Integration tests for the `/students` HTTP routes: CRUD round-trip, plus
Progress upsert / LessonLog creation / the student-detail aggregate (Plan 6
Task 3) — all live in this one file since all of it is the SAME router
(`routers/students.py`), mirroring `test_curriculum_api.py`'s precedent of
keeping every route group of one router file in one test file, split into
commented sections, rather than one test file per route group.

Only hits the DB (no LLM/embed call anywhere in this module) — not marked
`@pytest.mark.integration`, same precedent as `test_segment.py` ("that
marker means 'hits live vLLM' per pyproject.toml"). Mirrors
`test_knowledge_router.py`'s skip-guard + `setup_module` + `TestClient`
pattern.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
from app.models.curriculum import Assignment, LessonLog, Progress

# Skip cleanly (not error) when no DB is reachable — mirrors test_knowledge_router.py.
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


client = TestClient(app)


def _create_student(**overrides) -> dict:
    payload = {"name": "Alex Doe", "preferred_language": "en"}
    payload.update(overrides)
    r = client.post("/students", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def test_create_student_persists_all_provided_fields():
    body = _create_student(
        name="Nikos Papas",
        birthdate="2012-05-01",
        level="beginner",
        instrument="guitar",
        preferred_language="el",
    )
    assert body["name"] == "Nikos Papas"
    assert body["birthdate"] == "2012-05-01"
    assert body["level"] == "beginner"
    assert body["instrument"] == "guitar"
    assert body["preferred_language"] == "el"
    assert body["status"] == "active"  # model default
    assert body["id"]
    assert body["created_at"]
    assert body["updated_at"]


def test_create_student_with_only_name_applies_defaults():
    r = client.post("/students", json={"name": "Minimal Student"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Minimal Student"
    assert body["birthdate"] is None
    assert body["level"] is None
    assert body["instrument"] is None
    assert body["preferred_language"] == "el"  # model/schema default


def test_create_student_without_name_422s():
    r = client.post("/students", json={"level": "beginner"})
    assert r.status_code == 422


def test_get_student_returns_created_student():
    created = _create_student(name="Get Me")
    r = client.get(f"/students/{created['id']}")
    assert r.status_code == 200
    assert r.json() == created


def test_get_unknown_student_404s():
    r = client.get("/students/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_list_students_includes_created_student():
    created = _create_student(name="Listed Student")
    r = client.get("/students")
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()]
    assert created["id"] in ids


def test_patch_student_updates_only_provided_fields():
    created = _create_student(name="Patch Me", level="beginner", instrument="guitar")

    r = client.patch(f"/students/{created['id']}", json={"level": "advanced"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["level"] == "advanced"
    assert body["name"] == "Patch Me"          # untouched
    assert body["instrument"] == "guitar"      # untouched
    assert body["updated_at"] != created["updated_at"]

    # Genuinely persisted — round-trip via a separate GET.
    r2 = client.get(f"/students/{created['id']}")
    assert r2.json()["level"] == "advanced"


def test_patch_student_explicit_null_clears_nullable_field():
    created = _create_student(name="Clear Me", level="beginner")

    r = client.patch(f"/students/{created['id']}", json={"level": None})
    assert r.status_code == 200, r.text
    assert r.json()["level"] is None


def test_patch_unknown_student_404s():
    r = client.patch(
        "/students/00000000-0000-0000-0000-000000000000", json={"level": "advanced"}
    )
    assert r.status_code == 404


def test_delete_student_removes_it():
    created = _create_student(name="Delete Me")
    r = client.delete(f"/students/{created['id']}")
    assert r.status_code == 204
    assert client.get(f"/students/{created['id']}").status_code == 404


def test_delete_unknown_student_404s():
    r = client.delete("/students/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def _create_block(**overrides) -> uuid.UUID:
    """Insert one standalone Block directly via the ORM. Progress/lesson-log
    routes only need a valid block id to reference (a session/content
    block) — there is no HTTP endpoint that creates a bare Block from
    scratch (blocks come from curriculum generation/segmentation), so this
    mirrors `test_curriculum_api.py`'s own direct-ORM seeding style for the
    same reason. `Block.id` is readable immediately after `db.commit()` —
    no `db.refresh()` needed — same Postgres RETURNING precedent
    `create_student`'s own comment documents.
    """
    payload = {"kind": "lesson", "title": "Barre Chords", "language": "en"}
    payload.update(overrides)
    db = SessionLocal()
    try:
        block = Block(**payload)
        db.add(block)
        db.commit()
        return block.id
    finally:
        db.close()


# --- POST /students/{id}/progress (upsert) ----------------------------------

def test_upsert_progress_creates_new_row():
    student = _create_student(name="Progress Student")
    block_id = _create_block()

    r = client.post(
        f"/students/{student['id']}/progress",
        json={"block_id": str(block_id), "status": "introduced", "notes": "started barre chords"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["student_id"] == student["id"]
    assert body["block_id"] == str(block_id)
    assert body["status"] == "introduced"
    assert body["notes"] == "started barre chords"
    assert body["id"]


def test_upsert_progress_second_call_updates_same_row_not_duplicate():
    """The exact scenario the brief calls out: create, then POST the same
    (student, block) again with a new status — ONE row, updated status, not
    a second row.
    """
    student = _create_student(name="Upsert Student")
    block_id = _create_block()

    r1 = client.post(
        f"/students/{student['id']}/progress",
        json={"block_id": str(block_id), "status": "introduced", "notes": "first note"},
    )
    assert r1.status_code == 200, r1.text
    first_id = r1.json()["id"]

    r2 = client.post(
        f"/students/{student['id']}/progress",
        json={"block_id": str(block_id), "status": "mastered", "notes": "nailed it"},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["id"] == first_id          # SAME row, not a new one
    assert body2["status"] == "mastered"
    assert body2["notes"] == "nailed it"

    # Genuinely one row in the DB, not just an echoed response.
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(Progress).where(
                Progress.student_id == uuid.UUID(student["id"]), Progress.block_id == block_id
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].status == "mastered"
    finally:
        db.close()


def test_upsert_progress_notes_defaults_to_none_when_omitted():
    student = _create_student(name="No Notes Student")
    block_id = _create_block()

    r = client.post(
        f"/students/{student['id']}/progress",
        json={"block_id": str(block_id), "status": "practicing"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["notes"] is None


def test_upsert_progress_unknown_student_404s():
    block_id = _create_block()
    r = client.post(
        "/students/00000000-0000-0000-0000-000000000000/progress",
        json={"block_id": str(block_id), "status": "introduced"},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "student not found"


def test_upsert_progress_unknown_block_404s():
    student = _create_student(name="Progress 404 Student")
    r = client.post(
        f"/students/{student['id']}/progress",
        json={"block_id": "00000000-0000-0000-0000-000000000000", "status": "introduced"},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "block not found"


# --- POST /students/{id}/lessons --------------------------------------------

def test_create_lesson_log_persists_all_fields():
    student = _create_student(name="Lesson Student")
    block_id = _create_block(title="Session 1")

    r = client.post(
        f"/students/{student['id']}/lessons",
        json={
            "session_block_id": str(block_id),
            "date": "2026-01-15",
            "taught": True,
            "notes": "worked on strumming",
            "homework": "practice 15 min/day",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["student_id"] == student["id"]
    assert body["session_block_id"] == str(block_id)
    assert body["date"] == "2026-01-15"
    assert body["taught"] is True
    assert body["notes"] == "worked on strumming"
    assert body["homework"] == "practice 15 min/day"
    assert body["id"]


def test_create_lesson_log_applies_defaults_when_optional_fields_omitted():
    student = _create_student(name="Minimal Lesson Student")
    block_id = _create_block()

    r = client.post(f"/students/{student['id']}/lessons", json={"session_block_id": str(block_id)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["date"] is None
    assert body["taught"] is False
    assert body["notes"] is None
    assert body["homework"] is None


def test_create_lesson_log_unknown_student_404s():
    block_id = _create_block()
    r = client.post(
        "/students/00000000-0000-0000-0000-000000000000/lessons",
        json={"session_block_id": str(block_id)},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "student not found"


def test_create_lesson_log_unknown_block_404s():
    student = _create_student(name="Lesson 404 Student")
    r = client.post(
        f"/students/{student['id']}/lessons",
        json={"session_block_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "block not found"


# --- GET /students/{id}/detail ----------------------------------------------

def test_get_student_detail_returns_assignments_progress_and_recent_lessons():
    student = _create_student(name="Detail Student")
    student_id = student["id"]
    template_block_id = _create_block(title="Rhythm Fundamentals", kind="course")
    session_block_id = _create_block(title="Session 1")

    db = SessionLocal()
    try:
        assignment = Assignment(
            student_id=uuid.UUID(student_id), curriculum_block_id=template_block_id
        )
        db.add(assignment)
        db.commit()
        assignment_id = assignment.id
    finally:
        db.close()

    pr = client.post(
        f"/students/{student_id}/progress",
        json={"block_id": str(session_block_id), "status": "practicing", "notes": "sounding good"},
    )
    assert pr.status_code == 200, pr.text
    lr = client.post(
        f"/students/{student_id}/lessons",
        json={"session_block_id": str(session_block_id), "taught": True, "notes": "good session"},
    )
    assert lr.status_code == 200, lr.text

    r = client.get(f"/students/{student_id}/detail")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["student"]["id"] == student_id
    assert body["assignments"] == [
        {
            "assignment_id": str(assignment_id),
            "curriculum_block_id": str(template_block_id),
            "title": "Rhythm Fundamentals",
        }
    ]
    assert len(body["progress"]) == 1
    assert body["progress"][0]["status"] == "practicing"
    assert body["progress"][0]["notes"] == "sounding good"
    assert len(body["recent_lessons"]) == 1
    assert body["recent_lessons"][0]["notes"] == "good session"
    assert body["recent_lessons"][0]["taught"] is True


def test_get_student_detail_caps_recent_lessons_at_20_newest_first():
    """25 LessonLogs seeded with explicit, strictly-increasing `created_at`
    values (bypassing the server_default) so ordering is deterministic —
    NOT relying on 25 rapid-fire real-clock inserts to naturally land in
    increasing order, which would be a flaky way to pin down "newest first".
    """
    student = _create_student(name="Many Lessons Student")
    student_id = uuid.UUID(student["id"])
    block_id = _create_block()

    db = SessionLocal()
    try:
        base = datetime.now(timezone.utc)
        for i in range(25):
            db.add(LessonLog(
                student_id=student_id, session_block_id=block_id,
                notes=f"lesson {i}", created_at=base + timedelta(minutes=i),
            ))
        db.commit()
    finally:
        db.close()

    r = client.get(f"/students/{student['id']}/detail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["recent_lessons"]) == 20
    assert body["recent_lessons"][0]["notes"] == "lesson 24"    # newest first
    assert body["recent_lessons"][-1]["notes"] == "lesson 5"    # 20th-newest kept; 0-4 dropped


def test_get_student_detail_empty_sections_for_student_with_nothing():
    student = _create_student(name="Empty Detail Student")
    r = client.get(f"/students/{student['id']}/detail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assignments"] == []
    assert body["progress"] == []
    assert body["recent_lessons"] == []


def test_get_student_detail_unknown_student_404s():
    r = client.get("/students/00000000-0000-0000-0000-000000000000/detail")
    assert r.status_code == 404
    assert r.json()["detail"] == "student not found"
