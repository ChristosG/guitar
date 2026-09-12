"""The two lesson-panel routes (`/blocks/{lesson_id}/ai/plan|apply`), each a
202 + ONE `lesson_ai` job (`mode` on params — the `curriculum_revise` plan-vs-
apply shape). `plan_lesson_change`/`apply_lesson_change` themselves are
Task 2.1/2.2's; this file is the enqueue/route/busy-guard boundary around
`app/jobs/lesson_ai.py`.
"""
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
from app.models.generation_job import GenerationJob
import app.routers.curriculum as cur_router
import app.jobs.lesson_ai as job_mod

client = TestClient(app)
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


@pytest.fixture
def lesson_id():
    db = SessionLocal()
    course = Block(kind="course", title="Ή", language="el", is_template=True, meta={"shape": {"minutes_per_lesson": 50}})
    db.add(course); db.flush()
    module = Block(kind="module", title="Μ", parent_id=course.id, order=0, language="el", meta={})
    db.add(module); db.flush()
    lesson = Block(kind="lesson", title="Λ", parent_id=module.id, order=0, language="el", meta={"draft_status": "ready"})
    db.add(lesson); db.flush()
    db.add(Block(kind="segment", title="Θ", body="κείμενο", order=0, parent_id=lesson.id, language="el", meta={"section": "theory"}))
    db.commit()
    lid, mid = lesson.id, module.id
    db.close()
    return lid, mid


def test_plan_route_enqueues_and_the_job_stores_the_plan(lesson_id, monkeypatch):
    lid, _ = lesson_id
    monkeypatch.setattr(job_mod, "plan_lesson_change",
                        lambda db, l, *, instruction, note: {"summary": "s", "sections": [], "note_to_tutor": "", "dropped": [], "impact": {"rewrite_count": 0, "est_words": 0}})
    r = client.post(f"/blocks/{lid}/ai/plan", json={"instruction": "κάνε το καλύτερο"})
    assert r.status_code == 202
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["kind"] == "lesson_ai" and job["status"] == "succeeded"
    assert job["progress"]["phase"] == "done" and job["progress"]["plan"]["summary"] == "s"


def test_apply_route_validates_sections_then_runs(lesson_id, monkeypatch):
    lid, _ = lesson_id
    assert client.post(f"/blocks/{lid}/ai/apply", json={"instruction": "x", "sections": []}).status_code == 422
    assert client.post(f"/blocks/{lid}/ai/apply", json={"instruction": "x", "sections": [{"section": "ghost", "brief": ""}]}).status_code == 422
    monkeypatch.setattr(job_mod, "apply_lesson_change",
                        lambda db, l, *, instruction, note, sections: {"rewritten": ["theory"], "word_count": 3})
    r = client.post(f"/blocks/{lid}/ai/apply", json={"instruction": "x", "sections": [{"section": "theory", "brief": "b"}]})
    assert r.status_code == 202
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "succeeded" and job["progress"]["rewritten"] == ["theory"]


def test_plan_on_a_module_is_404_and_busy_is_409(lesson_id, monkeypatch):
    lid, mid = lesson_id
    assert client.post(f"/blocks/{mid}/ai/plan", json={"instruction": "x"}).status_code == 404
    monkeypatch.setattr(cur_router, "run_lesson_ai_job", lambda job_id: None)   # stays pending
    assert client.post(f"/blocks/{lid}/ai/plan", json={"instruction": "x"}).status_code == 202
    r = client.post(f"/blocks/{lid}/ai/plan", json={"instruction": "y"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "lesson_busy"


def test_llm_failure_in_apply_is_a_failed_job_with_kind(lesson_id, monkeypatch):
    from app.llm.errors import LLMError
    lid, _ = lesson_id
    def boom(*a, **k):
        raise LLMError("rate_limit", "429")
    monkeypatch.setattr(job_mod, "apply_lesson_change", boom)
    r = client.post(f"/blocks/{lid}/ai/apply", json={"instruction": "x", "sections": [{"section": "theory", "brief": ""}]})
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "failed" and job["error_kind"] == "rate_limit"
