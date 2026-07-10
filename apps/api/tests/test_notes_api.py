"""Integration tests for the `/notes` HTTP routes: CRUD round-trip, the
`?student_id=` filter, and promote-to-Brain (Task 1, Plan 6).

Only hits the DB (no live LLM/embed call anywhere in this module) — not
marked `@pytest.mark.integration`, same precedent as `test_students_api.py`/
`test_artifacts_api.py`'s own DB-only test modules. `ingest_source` is
stubbed at `app.routers.knowledge.ingest_source` (the name binding `create_
source` itself resolves, NOT `app.brain.ingest.ingest_source` — patching the
origin module would leave that already-bound import untouched) for every
test in this module, same "patch where it's looked up" rule `test_seed.py`
already follows for the identical call chain (`promote_note` -> `create_
source` -> `ingest_source`). `create_source`'s own row-create logic still
runs for real — never a problem here since promote always uses `kind=
"text"`, so the `kind="url"` SSRF guard never triggers.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

import app.routers.knowledge as knowledge_router
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.knowledge import KnowledgeSource
from app.models.student import Student

# Skip cleanly (not error) when no DB is reachable — mirrors test_students_api.py.
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


def _fake_ingest_source(db, source_id, payload):
    """Mirrors test_seed.py's own fake: stands in for the real extract->
    chunk->embed pipeline with a fast, schema-plausible status flip."""
    source = db.get(KnowledgeSource, source_id)
    source.status = "ready"
    source.char_count = len(payload.text or payload.url or "")
    db.commit()


@pytest.fixture(autouse=True)
def _stub_ingest(monkeypatch):
    monkeypatch.setattr(knowledge_router, "ingest_source", _fake_ingest_source)


def _create_student(name: str = "Note Test Student") -> Student:
    db = SessionLocal()
    try:
        student = Student(name=name)
        db.add(student)
        db.commit()
        db.refresh(student)
        return student
    finally:
        db.close()


def _create_note(**overrides) -> dict:
    payload = {
        "title": "Practice reminder",
        "body": "Work on alternate picking for 10 minutes daily.",
    }
    payload.update(overrides)
    r = client.post("/notes", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# --- POST /notes -------------------------------------------------------------

def test_create_note_persists_all_provided_fields():
    student = _create_student()
    body = _create_note(
        title="Barre chord tip",
        body="Keep the thumb centered behind the neck.",
        tags=["technique", "chords"],
        student_id=str(student.id),
    )
    assert body["title"] == "Barre chord tip"
    assert body["body"] == "Keep the thumb centered behind the neck."
    assert body["tags"] == ["technique", "chords"]
    assert body["student_id"] == str(student.id)
    assert body["promoted_to_knowledge"] is False
    assert body["id"]
    assert body["created_at"]
    assert body["updated_at"]


def test_create_note_with_only_title_and_body_applies_defaults():
    body = _create_note()
    assert body["tags"] == []
    assert body["student_id"] is None
    assert body["promoted_to_knowledge"] is False


def test_create_note_without_title_422s():
    r = client.post("/notes", json={"body": "no title here"})
    assert r.status_code == 422


def test_create_note_without_body_422s():
    r = client.post("/notes", json={"title": "no body here"})
    assert r.status_code == 422


def test_create_note_rejects_empty_string_title():
    r = client.post("/notes", json={"title": "", "body": "text"})
    assert r.status_code == 422, r.text


def test_create_note_title_over_column_limit_422s():
    """`Note.title` is `String(300)` — unlike `Artifact.title` (which
    silently clamps an over-long client-supplied title, since an LLM-derived
    title is expected to need normalizing), a human-typed Note title is
    rejected outright via `NoteCreate.title`'s `Field(max_length=300)`
    rather than silently truncated, so a client finds out immediately
    instead of getting back a quietly-shortened title. This also means it
    never reaches Postgres uncaught (`StringDataRightTruncation` -> 500).
    """
    r = client.post("/notes", json={"title": "T" * 301, "body": "text"})
    assert r.status_code == 422, r.text


def test_create_note_with_unknown_student_id_404s():
    r = client.post("/notes", json={
        "title": "Orphan note", "body": "text",
        "student_id": "00000000-0000-0000-0000-000000000000",
    })
    assert r.status_code == 404


# --- GET /notes/{id} ----------------------------------------------------------

def test_get_note_returns_created_note():
    created = _create_note(title="Get me")
    r = client.get(f"/notes/{created['id']}")
    assert r.status_code == 200
    assert r.json() == created


def test_get_unknown_note_404s():
    r = client.get("/notes/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


# --- GET /notes (+ student_id filter) -----------------------------------------

def test_list_notes_includes_created_note():
    created = _create_note(title="Listed note")
    r = client.get("/notes")
    assert r.status_code == 200
    ids = [n["id"] for n in r.json()]
    assert created["id"] in ids


def test_list_notes_filters_by_student_id():
    student_a = _create_student("Student A")
    student_b = _create_student("Student B")
    _create_note(title="For A", student_id=str(student_a.id))
    note_b = _create_note(title="For B", student_id=str(student_b.id))

    r = client.get("/notes", params={"student_id": str(student_a.id)})
    assert r.status_code == 200, r.text
    titles = {n["title"] for n in r.json()}
    assert titles == {"For A"}
    assert note_b["title"] not in titles


# --- PATCH /notes/{id} ---------------------------------------------------------

def test_patch_note_updates_only_provided_fields():
    created = _create_note(title="Patch me", body="Original body", tags=["a"])

    r = client.patch(f"/notes/{created['id']}", json={"body": "Updated body"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["body"] == "Updated body"
    assert body["title"] == "Patch me"          # untouched
    assert body["tags"] == ["a"]                # untouched
    assert body["updated_at"] != created["updated_at"]

    # Genuinely persisted — round-trip via a separate GET.
    r2 = client.get(f"/notes/{created['id']}")
    assert r2.json()["body"] == "Updated body"


def test_patch_note_explicit_null_is_a_noop_not_a_clear():
    """`Note.title`/`Note.body` are NOT NULL columns — same `exclude_unset=
    True, exclude_none=True` "null is always a no-op" rule `routers.
    curriculum.update_block` established for the identical shape (mixing a
    NOT NULL column with a nullable one) applies here, so an explicit
    `{"title": null}`/`{"body": null}` must leave the stored value
    untouched rather than crashing. `student_id` (the one genuinely nullable
    field) is swept up by the same uniform rule, so it also can't be
    cleared back to null via PATCH this way — an accepted tradeoff, see
    routers/notes.py's own docstring.
    """
    student = _create_student()
    created = _create_note(title="Keep me", body="Keep body", student_id=str(student.id))

    r = client.patch(f"/notes/{created['id']}", json={
        "title": None, "body": None, "student_id": None,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Keep me"
    assert body["body"] == "Keep body"
    assert body["student_id"] == str(student.id)


def test_patch_note_rejects_empty_string_title():
    created = _create_note()
    r = client.patch(f"/notes/{created['id']}", json={"title": ""})
    assert r.status_code == 422, r.text


def test_patch_note_title_over_column_limit_422s():
    created = _create_note()
    r = client.patch(f"/notes/{created['id']}", json={"title": "T" * 301})
    assert r.status_code == 422, r.text


def test_patch_unknown_note_404s():
    r = client.patch("/notes/00000000-0000-0000-0000-000000000000", json={"title": "x"})
    assert r.status_code == 404


def test_patch_note_with_unknown_student_id_404s():
    created = _create_note()
    r = client.patch(f"/notes/{created['id']}", json={
        "student_id": "00000000-0000-0000-0000-000000000000",
    })
    assert r.status_code == 404


def test_patch_note_links_to_a_valid_student():
    student = _create_student()
    created = _create_note()
    r = client.patch(f"/notes/{created['id']}", json={"student_id": str(student.id)})
    assert r.status_code == 200, r.text
    assert r.json()["student_id"] == str(student.id)


# --- DELETE /notes/{id} ---------------------------------------------------------

def test_delete_note_removes_it():
    created = _create_note()
    r = client.delete(f"/notes/{created['id']}")
    assert r.status_code == 204
    assert client.get(f"/notes/{created['id']}").status_code == 404


def test_delete_unknown_note_404s():
    r = client.delete("/notes/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


# --- POST /notes/{id}/promote ---------------------------------------------------

def test_promote_note_creates_knowledge_source_and_flips_flag():
    created = _create_note(
        title="Tone tip: SRV bite",
        body="Bridge pickup, heavy strings, and a cranked tube amp for extra bite.",
    )

    r = client.post(f"/notes/{created['id']}/promote")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["promoted_to_knowledge"] is True
    assert body["source_id"]

    db = SessionLocal()
    try:
        source = db.get(KnowledgeSource, body["source_id"])
        assert source is not None
        assert source.title == "Tone tip: SRV bite"
        assert source.type == "text"
        assert source.status == "ready"     # from the stubbed ingest_source
    finally:
        db.close()

    # Genuinely persisted — round-trips via a separate GET.
    r2 = client.get(f"/notes/{created['id']}")
    assert r2.json()["promoted_to_knowledge"] is True


def test_repromote_note_409s_and_does_not_create_a_second_source():
    created = _create_note()
    r1 = client.post(f"/notes/{created['id']}/promote")
    assert r1.status_code == 200, r1.text

    db = SessionLocal()
    try:
        count_before = len(db.scalars(select(KnowledgeSource)).all())
    finally:
        db.close()

    r2 = client.post(f"/notes/{created['id']}/promote")
    assert r2.status_code == 409, r2.text

    db = SessionLocal()
    try:
        count_after = len(db.scalars(select(KnowledgeSource)).all())
    finally:
        db.close()
    assert count_after == count_before   # no duplicate source created


def test_promote_note_with_empty_body_422s():
    created = _create_note(title="Empty note", body="")
    r = client.post(f"/notes/{created['id']}/promote")
    assert r.status_code == 422, r.text


def test_promote_note_with_whitespace_only_body_422s():
    created = _create_note(title="Whitespace note", body="   \n\t  ")
    r = client.post(f"/notes/{created['id']}/promote")
    assert r.status_code == 422, r.text


def test_promote_unknown_note_404s():
    r = client.post("/notes/00000000-0000-0000-0000-000000000000/promote")
    assert r.status_code == 404
