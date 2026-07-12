import uuid

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
