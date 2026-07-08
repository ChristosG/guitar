"""Integration tests for the `/students` HTTP routes: CRUD round-trip.

Only hits the DB (no LLM/embed call anywhere in this module) — not marked
`@pytest.mark.integration`, same precedent as `test_segment.py` ("that
marker means 'hits live vLLM' per pyproject.toml"). Mirrors
`test_knowledge_router.py`'s skip-guard + `setup_module` + `TestClient`
pattern.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import Base, engine
from app.main import app

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
