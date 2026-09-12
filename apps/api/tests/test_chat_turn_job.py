"""The revise drawer's turns run as a job — nothing in front of a job can cut it.

Live evidence (2026-09-11): the planner ran inside the HTTP request, behind
nginx (60/300s) and Cloudflare (~100s); a 6-minute claude -p turn never made it
back to the browser. `jobs/curriculum_revise.py` already had a plan-mode job the
chat never used. This makes the whole turn the job.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agent.loop import AgentResult
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.generation_job import GenerationJob
from app.models.chat import ApprovalRequest, Message
import app.routers.chat as chat_router
import app.jobs.chat_turn as chat_turn_job

client = TestClient(app)

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


def _answer(content: str):
    def fake(db, wire, **kw):
        return AgentResult(status="answer", content=content,
                           messages=[*wire, {"role": "assistant", "content": content}],
                           citations=[])
    return fake


def test_async_flag_returns_202_and_persists_the_user_row(monkeypatch):
    monkeypatch.setattr(chat_router, "run_agent_turn", _answer("ok"))
    # Do not let TestClient run the background task here — we want the 202 shape alone.
    monkeypatch.setattr(chat_router, "run_chat_turn_job", lambda job_id: None)
    session = client.post("/chat", json={"locale": "el"}).json()["session_id"]
    r = client.post(f"/chat/{session}/messages?async=1", json={"content": "γεια"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending" and body["job_id"]
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, uuid.UUID(body["job_id"]))
        assert job.kind == "chat_turn" and job.params["session_id"] == session
        rows = db.query(Message).filter_by(session_id=uuid.UUID(session)).all()
        assert [m.role for m in rows] == ["user"]
    finally:
        db.close()


def test_job_runs_the_turn_and_records_the_answer(monkeypatch):
    monkeypatch.setattr(chat_router, "run_agent_turn", _answer("η απάντηση"))
    session = client.post("/chat", json={"locale": "el"}).json()["session_id"]
    r = client.post(f"/chat/{session}/messages?async=1", json={"content": "γεια"})
    job_id = r.json()["job_id"]          # TestClient ran the background task after the response
    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "succeeded"
    assert job["progress"]["turn"]["status"] == "answer"
    history = client.get(f"/chat/{session}").json()
    assert history[-1]["role"] == "assistant" and history[-1]["content"] == "η απάντηση"


def test_job_turn_can_open_an_approval(monkeypatch):
    def suspend(db, wire, **kw):
        return AgentResult(
            status="awaiting_approval", content="Πρόταση:",
            messages=[*wire, {"role": "assistant", "content": "Πρόταση:", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "generate_curriculum", "arguments": "{}"}}]}],
            pending_tool={"tool_call_id": "c1", "name": "generate_curriculum", "arguments": {}},
            citations=[],
        )
    monkeypatch.setattr(chat_router, "run_agent_turn", suspend)
    session = client.post("/chat", json={"locale": "el"}).json()["session_id"]
    r = client.post(f"/chat/{session}/messages?async=1", json={"content": "φτιάξε"})
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["progress"]["turn"]["status"] == "awaiting_approval"
    pending = client.get(f"/chat/{session}/pending").json()
    assert pending and pending["tool_name"] == "generate_curriculum"


def test_second_async_turn_while_one_runs_is_409(monkeypatch):
    monkeypatch.setattr(chat_router, "run_chat_turn_job", lambda job_id: None)  # stays pending
    session = client.post("/chat", json={"locale": "el"}).json()["session_id"]
    assert client.post(f"/chat/{session}/messages?async=1", json={"content": "α"}).status_code == 202
    r = client.post(f"/chat/{session}/messages?async=1", json={"content": "β"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "turn_running"
    # the sync endpoint refuses too
    r2 = client.post(f"/chat/{session}/messages", json={"content": "γ"})
    assert r2.status_code == 409


def test_llm_error_in_the_job_is_recorded_with_its_kind(monkeypatch):
    from app.llm.errors import LLMError
    def boom(db, wire, **kw):
        raise LLMError("too_long", "prompt is too long")
    monkeypatch.setattr(chat_router, "run_agent_turn", boom)
    session = client.post("/chat", json={"locale": "el"}).json()["session_id"]
    r = client.post(f"/chat/{session}/messages?async=1", json={"content": "γεια"})
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "failed" and job["error_kind"] == "too_long"
