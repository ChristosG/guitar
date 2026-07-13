import uuid

from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource, Page


def test_selection_enqueues_a_lesson_drafting_job(client, db, monkeypatch):
    # A "grounded" lesson is a blocking guided-JSON LLM call (Plan 10 Task 1,
    # B4 — mirrors /curricula/generate exactly), too slow for a synchronous
    # response. monkeypatch run_lesson_job: Starlette's TestClient runs
    # BackgroundTasks in-process AFTER the response, so an unpatched test
    # would fire a real, multi-minute LLM call (KEY TEST GOTCHA).
    monkeypatch.setattr("app.routers.lessons.run_lesson_job", lambda job_id: None)

    src = KnowledgeSource(type="pdf", title="Getting Great Guitar Sounds", status="ready")
    db.add(src); db.commit()
    db.add(Page(source_id=src.id, page_no=47, status="ready", text="The Tube Screamer..."))
    db.commit()

    r = client.post("/lessons/from-selection", json={
        "source_id": str(src.id), "page_no": 47,
        "text": "The Tube Screamer is not really a distortion box",
    })

    assert r.status_code == 202
    body = r.json()
    assert uuid.UUID(body["job_id"])
    assert body["status"] == "pending"


def test_selection_from_an_unknown_source_is_404(client):
    import uuid
    r = client.post("/lessons/from-selection", json={
        "source_id": str(uuid.uuid4()), "page_no": 1, "text": "x"})
    assert r.status_code == 404


def test_empty_selection_is_rejected(client, db):
    src = KnowledgeSource(type="pdf", title="Book", status="ready")
    db.add(src); db.commit()
    r = client.post("/lessons/from-selection", json={
        "source_id": str(src.id), "page_no": 1, "text": "   "})
    assert r.status_code == 422


def test_selection_accepts_a_page_range(client, db, monkeypatch):
    """G4 (Plan 12 Task 4): the continuous-scroll Reader's selection can
    span pages, so `page_from`/`page_to` is the current shape."""
    monkeypatch.setattr("app.routers.lessons.run_lesson_job", lambda job_id: None)

    src = KnowledgeSource(type="pdf", title="Getting Great Guitar Sounds", status="ready")
    db.add(src); db.commit()

    r = client.post("/lessons/from-selection", json={
        "source_id": str(src.id), "page_from": 21, "page_to": 23,
        "text": "A passage that spans three pages.",
    })

    assert r.status_code == 202
    body = r.json()
    assert uuid.UUID(body["job_id"])
    assert body["status"] == "pending"

    job = db.get(GenerationJob, uuid.UUID(body["job_id"]))
    assert job.params["page_from"] == 21
    assert job.params["page_to"] == 23


def test_selection_still_accepts_legacy_page_no(client, db, monkeypatch):
    # Backward compat: existing citation chips / callers post `page_no`
    # alone. Must still 202, not 422.
    monkeypatch.setattr("app.routers.lessons.run_lesson_job", lambda job_id: None)

    src = KnowledgeSource(type="pdf", title="Getting Great Guitar Sounds", status="ready")
    db.add(src); db.commit()

    r = client.post("/lessons/from-selection", json={
        "source_id": str(src.id), "page_no": 47, "text": "A single-page selection.",
    })

    assert r.status_code == 202


def test_selection_with_page_to_before_page_from_is_rejected(client, db):
    src = KnowledgeSource(type="pdf", title="Book", status="ready")
    db.add(src); db.commit()
    r = client.post("/lessons/from-selection", json={
        "source_id": str(src.id), "page_from": 23, "page_to": 21, "text": "x",
    })
    assert r.status_code == 422


def test_selection_with_no_page_info_at_all_is_rejected(client, db):
    src = KnowledgeSource(type="pdf", title="Book", status="ready")
    db.add(src); db.commit()
    r = client.post("/lessons/from-selection", json={
        "source_id": str(src.id), "text": "x",
    })
    assert r.status_code == 422
