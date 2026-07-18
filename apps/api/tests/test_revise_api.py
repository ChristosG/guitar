"""D1c — the runner + `POST /curricula/{root_id}/revise` (202, plan vs apply).

The endpoint enqueues a `curriculum_revise` GenerationJob and returns 202; the
runner reads `job.params` and either PLANS (no "plan" -> store it on
`job.progress`) or APPLIES (a "plan" -> one tx + chain the draft fan-out).

The endpoint tests monkeypatch `app.routers.curriculum.run_curriculum_revise_job`
to a spy — Starlette's TestClient runs BackgroundTasks in-process AFTER the
response, so an unpatched test would fire the real planner (a live library read).
The runner is unit-tested DIRECTLY with `plan_revision`/`apply_revision`/
`run_curriculum_draft_job` stubbed, so no live model is ever hit here.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.jobs.curriculum_revise as revise_job
from app.db import Base, SessionLocal, engine
from app.jobs.curriculum_revise import run_curriculum_revise_job
from app.main import app
from app.models.block import Block
from app.models.generation_job import GenerationJob

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


client = TestClient(app)


def _seed_tree(db):
    """course -> module -> lesson, minimal but real (validate_ops needs live ids)."""
    course = Block(kind="course", title="Tone 101", is_template=True, language="el",
                   meta={"brief": "for gigging players", "source_ids": None})
    db.add(course)
    db.flush()
    m = Block(kind="module", title="Overdrive", parent_id=course.id, order=0,
              language="el", meta={"objective": "od pedals"})
    db.add(m)
    db.flush()
    lesson = Block(kind="lesson", title="Tube Screamer", parent_id=m.id, order=0,
                   language="el", meta={"objective": "ts basics"})
    db.add(lesson)
    db.commit()
    return course, m, lesson


# ---------------------------------------------------------------------------
# The endpoint (202 + job row shape)
# ---------------------------------------------------------------------------

def test_plan_mode_enqueues_a_curriculum_revise_job_with_no_plan(monkeypatch):
    monkeypatch.setattr("app.routers.curriculum.run_curriculum_revise_job", lambda job_id: None)
    db = SessionLocal()
    course, m, lesson = _seed_tree(db)
    try:
        r = client.post(f"/curricula/{course.id}/revise",
                        json={"instruction": "add a DS-1 lesson", "mode": "plan"})
        assert r.status_code == 202, r.text
        job_id = uuid.UUID(r.json()["job_id"])
        job = db.get(GenerationJob, job_id)
        assert job.kind == "curriculum_revise"
        assert job.params["root_id"] == str(course.id)
        assert job.params["instruction"] == "add a DS-1 lesson"
        assert "plan" not in job.params                      # plan mode carries no plan
    finally:
        db.close()


def test_plan_is_the_default_mode(monkeypatch):
    monkeypatch.setattr("app.routers.curriculum.run_curriculum_revise_job", lambda job_id: None)
    db = SessionLocal()
    course, m, lesson = _seed_tree(db)
    try:
        r = client.post(f"/curricula/{course.id}/revise", json={"instruction": "x"})
        assert r.status_code == 202, r.text
        job = db.get(GenerationJob, uuid.UUID(r.json()["job_id"]))
        assert "plan" not in job.params
    finally:
        db.close()


def test_apply_mode_stores_the_VALIDATED_plan_dropping_a_bogus_op(monkeypatch):
    """Approved == applied, EXACT: the plan is validated at the endpoint, BEFORE
    it is stored/applied — a bogus-id op never reaches the job params."""
    monkeypatch.setattr("app.routers.curriculum.run_curriculum_revise_job", lambda job_id: None)
    db = SessionLocal()
    course, m, lesson = _seed_tree(db)
    try:
        plan = {"summary": "add one, drop one", "ops": [
            {"op": "insert_lesson", "module_id": str(m.id), "title": "DS-1",
             "objective": "distortion", "reason": "gap after TS"},
            {"op": "insert_lesson", "module_id": str(uuid.uuid4()),   # bogus module
             "title": "ghost", "objective": "x", "reason": "should be dropped"},
        ]}
        r = client.post(f"/curricula/{course.id}/revise",
                        json={"instruction": "add DS-1", "mode": "apply", "plan": plan})
        assert r.status_code == 202, r.text
        job = db.get(GenerationJob, uuid.UUID(r.json()["job_id"]))
        stored = job.params["plan"]
        assert stored["summary"] == "add one, drop one"
        assert len(stored["ops"]) == 1                        # bogus op dropped at validate
        assert stored["ops"][0]["module_id"] == str(m.id)
    finally:
        db.close()


def test_apply_without_a_plan_is_422(monkeypatch):
    monkeypatch.setattr("app.routers.curriculum.run_curriculum_revise_job", lambda job_id: None)
    db = SessionLocal()
    course, m, lesson = _seed_tree(db)
    try:
        r = client.post(f"/curricula/{course.id}/revise",
                        json={"instruction": "x", "mode": "apply"})
        assert r.status_code == 422, r.text
    finally:
        db.close()


def test_revise_on_a_non_course_block_is_404(monkeypatch):
    monkeypatch.setattr("app.routers.curriculum.run_curriculum_revise_job", lambda job_id: None)
    db = SessionLocal()
    course, m, lesson = _seed_tree(db)
    try:
        r = client.post(f"/curricula/{m.id}/revise", json={"instruction": "x"})
        assert r.status_code == 404, r.text
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The runner (direct, stubbed — no live model)
# ---------------------------------------------------------------------------

def test_runner_plan_mode_stores_the_plan_on_progress(monkeypatch):
    canned = {"summary": "s", "ops": [{"op": "remove_lesson", "lesson_id": "x", "reason": "r"}]}
    monkeypatch.setattr(revise_job, "plan_revision",
                        lambda db, root_id, *, instruction: canned)
    db = SessionLocal()
    try:
        job = GenerationJob(kind="curriculum_revise", status="pending",
                            params={"root_id": str(uuid.uuid4()), "instruction": "go"})
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    run_curriculum_revise_job(job_id)

    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        assert job.status == "succeeded"
        assert job.progress["plan"] == canned
        assert job.progress["phase"] == "done"
    finally:
        db.close()


def test_runner_apply_mode_applies_then_chains_the_draft(monkeypatch):
    root_id = uuid.uuid4()
    apply_calls = []
    draft_calls = []

    def _fake_apply(db, rid, plan):
        apply_calls.append((rid, plan))
        return {"applied": 2, "root_id": str(rid)}

    monkeypatch.setattr(revise_job, "apply_revision", _fake_apply)
    monkeypatch.setattr(revise_job, "run_curriculum_draft_job", lambda jid: draft_calls.append(jid))

    db = SessionLocal()
    try:
        plan = {"summary": "s", "ops": []}
        job = GenerationJob(kind="curriculum_revise", status="pending",
                            params={"root_id": str(root_id), "instruction": "go", "plan": plan})
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    run_curriculum_revise_job(job_id)

    assert len(apply_calls) == 1 and str(apply_calls[0][0]) == str(root_id)

    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        assert job.status == "succeeded"
        assert job.progress == {"phase": "drafting", "applied": 2, "root_id": str(root_id)}
        # A SEPARATE curriculum_draft row was enqueued and its runner invoked.
        assert len(draft_calls) == 1
        draft = db.get(GenerationJob, draft_calls[0])
        assert draft.kind == "curriculum_draft"
        assert draft.params == {"root_id": str(root_id)}
    finally:
        db.close()


def test_runner_missing_job_is_a_noop():
    # No row, no crash — mirrors module_generate's own guard.
    run_curriculum_revise_job(uuid.uuid4())
