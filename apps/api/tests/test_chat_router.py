"""Tests for `/chat` routes (Plan 5 Task 4) — the chat router that persists
conversation state and drives approve/reject/resume + the async-generation
compose. THE integration crux of the chat agent: session create, turn-taking
through `run_agent_turn`, and the HITL approve/reject/resume flow tying
together the ReAct loop (Task 2), the suspend-on-mutation HITL models
(Task 3), and Plan 8's enqueue/poll pattern for the one async mutation
(`generate_curriculum`).

DB-touching (real Postgres, real `Student`/`GenerationJob`/chat-model rows)
but NO live LLM anywhere — the provider is a scripted fake, monkeypatched
into `app.agent.loop.get_provider` exactly like `test_agent_hitl.py`/
`test_agent_loop.py` (duplicated here rather than cross-imported, per this
codebase's own "small deliberate duplication over reaching into a
`_`-prefixed test helper" precedent) — so this is NOT marked
`@pytest.mark.integration` (that marker means "hits live vLLM" per
pyproject.toml). Mirrors `test_curriculum_generate_enqueue.py`'s skip-guard +
`setup_module` + `TestClient` pattern, including its critical gotcha:
`run_curriculum_job` is monkeypatched on `app.routers.chat`'s own bound name
(not the origin `app.jobs.runner` module) since Starlette's `TestClient` runs
`BackgroundTasks` in-process, after the response — an unpatched test would
schedule a real 49-179s LLM call.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.agent.loop as agent_loop
import app.routers.chat as chat_router
from app.agent.tools import TOOLS, ToolEntry
from app.db import Base, SessionLocal, engine
from app.llm.tools_types import AssistantTurn, ToolCall
from app.main import app
from app.models.chat import ApprovalRequest, Message
from app.models.generation_job import GenerationJob
from app.models.student import Student

# Skip cleanly (not error) when no DB is reachable — mirrors test_curriculum_generate_enqueue.py.
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


class _FakeProvider:
    """Mirrors `test_agent_hitl.py`'s `_FakeProvider` exactly: `chat_tools`
    returns the next scripted `turns` entry each call, repeating the last one
    once exhausted; records a snapshot of every call's `messages`.
    """

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls: list[list[dict]] = []

    def chat_tools(self, messages, tools, *, tool_choice="auto", temperature=0.3):
        self.calls.append([dict(m) for m in messages])
        turn = self._turns[min(len(self.calls) - 1, len(self._turns) - 1)]
        if isinstance(turn, Exception):
            raise turn
        return turn


def _use_provider(monkeypatch, turns) -> _FakeProvider:
    fake_provider = _FakeProvider(turns)
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)
    return fake_provider


def _stub_tool(monkeypatch, name: str, fn):
    """Mirrors `test_agent_hitl.py`'s `_stub_tool`: `ToolEntry` is frozen, so
    this swaps the whole dict entry via `monkeypatch.setitem`.
    """
    original = TOOLS[name]
    monkeypatch.setitem(
        TOOLS, name,
        ToolEntry(schema=original.schema, fn=fn, kind=original.kind, async_job=original.async_job),
    )


def _create_session(**overrides) -> str:
    r = client.post("/chat", json=overrides)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _db_approval(approval_id) -> ApprovalRequest:
    db = SessionLocal()
    try:
        return db.get(ApprovalRequest, uuid.UUID(approval_id))
    finally:
        db.close()


def _db_messages(session_id) -> list[Message]:
    db = SessionLocal()
    try:
        return list(db.query(Message).filter(Message.session_id == uuid.UUID(session_id)).all())
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /chat — session creation
# ---------------------------------------------------------------------------

def test_create_chat_session_returns_a_session_id():
    r = client.post("/chat", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert uuid.UUID(body["session_id"])  # a real uuid


def test_create_chat_session_accepts_an_optional_student_id():
    student_id = str(uuid.uuid4())
    r = client.post("/chat", json={"student_id": student_id})
    assert r.status_code == 200, r.text
    assert uuid.UUID(r.json()["session_id"])


# ---------------------------------------------------------------------------
# POST /chat/{session_id}/messages — plain answer
# ---------------------------------------------------------------------------

def test_post_message_unknown_session_404s():
    r = client.post(f"/chat/{uuid.uuid4()}/messages", json={"content": "hi"})
    assert r.status_code == 404, r.text


def test_post_message_plain_answer_returns_content_and_persists_history(monkeypatch):
    _use_provider(monkeypatch, [
        AssistantTurn(content="A humbucker cancels hum.", tool_calls=[]),
    ])
    session_id = _create_session()

    r = client.post(f"/chat/{session_id}/messages", json={"content": "what cancels hum?"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "answer"
    assert body["content"] == "A humbucker cancels hum."

    history = client.get(f"/chat/{session_id}")
    assert history.status_code == 200, history.text
    contents = [(m["role"], m["content"]) for m in history.json()]
    assert ("user", "what cancels hum?") in contents
    assert ("assistant", "A humbucker cancels hum.") in contents


# ---------------------------------------------------------------------------
# POST /chat/{session_id}/messages — mutation intent -> awaiting_approval
# ---------------------------------------------------------------------------

def test_post_message_mutation_intent_returns_awaiting_approval_and_does_not_call_the_fn(monkeypatch):
    calls_made = []

    def _spy(db, **kwargs):
        calls_made.append(kwargs)
        raise AssertionError("mutation fn must not be called before approval")

    _stub_tool(monkeypatch, "create_student", _spy)
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll add that student.",
            tool_calls=[ToolCall(id="call_1", name="create_student", arguments={"name": "New Kid"})],
        ),
    ])
    session_id = _create_session()

    r = client.post(f"/chat/{session_id}/messages", json={"content": "add a student named New Kid"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "awaiting_approval"
    assert body["tool_name"] == "create_student"
    assert body["tool_args"] == {"name": "New Kid"}
    assert body["approval_id"]
    assert body["description"]  # non-empty
    assert calls_made == []  # never invoked

    approval = _db_approval(body["approval_id"])
    assert approval is not None
    assert approval.status == "pending"
    assert approval.tool_name == "create_student"
    assert approval.tool_args == {"name": "New Kid"}
    assert approval.tool_call_id == "call_1"

    pending = client.get(f"/chat/{session_id}/pending")
    assert pending.status_code == 200
    assert pending.json()["id"] == body["approval_id"]


def test_get_pending_returns_null_when_none_pending(monkeypatch):
    _use_provider(monkeypatch, [AssistantTurn(content="hi", tool_calls=[])])
    session_id = _create_session()
    client.post(f"/chat/{session_id}/messages", json={"content": "hi"})

    r = client.get(f"/chat/{session_id}/pending")
    assert r.status_code == 200
    assert r.json() is None


def test_get_pending_unknown_session_404s():
    r = client.get(f"/chat/{uuid.uuid4()}/pending")
    assert r.status_code == 404


def test_get_history_unknown_session_404s():
    r = client.get(f"/chat/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# resolve: unknown session/approval -> 404
# ---------------------------------------------------------------------------

def test_resolve_unknown_session_404s():
    r = client.post(
        f"/chat/{uuid.uuid4()}/approvals/{uuid.uuid4()}/resolve", json={"decision": "approve"},
    )
    assert r.status_code == 404


def test_resolve_unknown_approval_404s(monkeypatch):
    _use_provider(monkeypatch, [AssistantTurn(content="hi", tool_calls=[])])
    session_id = _create_session()
    client.post(f"/chat/{session_id}/messages", json={"content": "hi"})

    r = client.post(
        f"/chat/{session_id}/approvals/{uuid.uuid4()}/resolve", json={"decision": "approve"},
    )
    assert r.status_code == 404


def test_resolve_approval_belonging_to_a_different_session_404s(monkeypatch):
    """A real, currently-pending `ApprovalRequest` id, but requested through a
    DIFFERENT session's URL — must 404 (not found IN THIS SESSION), not
    silently resolve someone else's session's approval by id collision.
    """
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll add that student.",
            tool_calls=[ToolCall(id="call_1", name="create_student", arguments={"name": "New Kid"})],
        ),
    ])
    owning_session_id = _create_session()
    propose = client.post(f"/chat/{owning_session_id}/messages", json={"content": "add New Kid"})
    approval_id = propose.json()["approval_id"]

    other_session_id = _create_session()

    r = client.post(
        f"/chat/{other_session_id}/approvals/{approval_id}/resolve", json={"decision": "approve"},
    )
    assert r.status_code == 404, r.text

    # Untouched: still pending under its real, owning session.
    approval = _db_approval(approval_id)
    assert approval.status == "pending"


# ---------------------------------------------------------------------------
# resolve approve — SYNC mutation (create_student, real fn)
# ---------------------------------------------------------------------------

def test_resolve_approve_sync_mutation_runs_the_real_fn_and_resumes(monkeypatch):
    fake_provider = _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll add that student.",
            tool_calls=[ToolCall(id="call_1", name="create_student", arguments={"name": "New Kid"})],
        ),
        AssistantTurn(content="Added New Kid to your roster.", tool_calls=[]),
    ])
    session_id = _create_session()

    propose = client.post(f"/chat/{session_id}/messages", json={"content": "add New Kid"})
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "approve"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "answer"
    assert body["content"] == "Added New Kid to your roster."
    assert len(fake_provider.calls) == 2  # the loop genuinely resumed

    # The REAL fn ran: a Student row now exists.
    db = SessionLocal()
    try:
        students = db.query(Student).filter(Student.name == "New Kid").all()
        assert len(students) == 1
        created_student_id = str(students[0].id)
    finally:
        db.close()

    approval = _db_approval(approval_id)
    assert approval.status == "approved"
    assert approval.result_ref == created_student_id
    assert approval.resolved_at is not None

    # A tool message answering the pending call_id is now in history.
    rows = _db_messages(session_id)
    tool_msgs = [m for m in rows if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_1"
    assert created_student_id in tool_msgs[0].content

    # No longer pending.
    assert client.get(f"/chat/{session_id}/pending").json() is None


def test_resolve_approve_uses_edited_args_when_given(monkeypatch):
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll add that student.",
            tool_calls=[ToolCall(id="call_1", name="create_student", arguments={"name": "Wrong Name"})],
        ),
        AssistantTurn(content="Added Right Name to your roster.", tool_calls=[]),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "add Wrong Name"})
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve",
        json={"decision": "approve", "edited_args": {"name": "Right Name"}},
    )

    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        assert db.query(Student).filter(Student.name == "Right Name").count() == 1
        assert db.query(Student).filter(Student.name == "Wrong Name").count() == 0
    finally:
        db.close()

    approval = _db_approval(approval_id)
    assert approval.edited_args == {"name": "Right Name"}


# ---------------------------------------------------------------------------
# resolve approve — mutation fn RAISES: no 500, graceful narration
# ---------------------------------------------------------------------------

def test_resolve_approve_when_fn_raises_records_error_and_narrates_gracefully(monkeypatch):
    def _boom(db, **kwargs):
        raise ValueError("boom: something went wrong")

    _stub_tool(monkeypatch, "create_student", _boom)
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll add that student.",
            tool_calls=[ToolCall(id="call_1", name="create_student", arguments={"name": "New Kid"})],
        ),
        AssistantTurn(content="Sorry, I couldn't add that student.", tool_calls=[]),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "add New Kid"})
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "approve"},
    )

    assert r.status_code != 500
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "answer"
    assert body["content"] == "Sorry, I couldn't add that student."

    approval = _db_approval(approval_id)
    assert approval.status == "error"
    assert approval.resolved_at is not None

    rows = _db_messages(session_id)
    tool_msgs = [m for m in rows if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content.startswith("ERROR:")
    assert "boom" in tool_msgs[0].content


# ---------------------------------------------------------------------------
# resolve reject
# ---------------------------------------------------------------------------

def test_resolve_reject_marks_rejected_and_resumes(monkeypatch):
    calls_made = []

    def _spy(db, **kwargs):
        calls_made.append(kwargs)
        raise AssertionError("rejected mutation fn must never be called")

    _stub_tool(monkeypatch, "create_student", _spy)
    fake_provider = _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll add that student.",
            tool_calls=[ToolCall(id="call_1", name="create_student", arguments={"name": "New Kid"})],
        ),
        AssistantTurn(content="No problem, I won't add them.", tool_calls=[]),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "add New Kid"})
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "reject"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "answer"
    assert body["content"] == "No problem, I won't add them."
    assert calls_made == []
    assert len(fake_provider.calls) == 2  # resumed

    approval = _db_approval(approval_id)
    assert approval.status == "rejected"
    assert approval.resolved_at is not None

    rows = _db_messages(session_id)
    tool_msgs = [m for m in rows if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_1"
    assert tool_msgs[0].content == "User rejected this action."


# ---------------------------------------------------------------------------
# resolve a non-pending approval -> 409
# ---------------------------------------------------------------------------

def test_resolve_already_resolved_approval_409s(monkeypatch):
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll add that student.",
            tool_calls=[ToolCall(id="call_1", name="create_student", arguments={"name": "New Kid"})],
        ),
        AssistantTurn(content="No problem.", tool_calls=[]),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "add New Kid"})
    approval_id = propose.json()["approval_id"]

    first = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "reject"},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "reject"},
    )
    assert second.status_code == 409, second.text


# ---------------------------------------------------------------------------
# resolve approve — ASYNC generate_curriculum: job_pending, no resume
# ---------------------------------------------------------------------------

def test_resolve_approve_async_generate_curriculum_enqueues_a_job_and_does_not_resume(monkeypatch):
    scheduled_job_ids = []
    monkeypatch.setattr(chat_router, "run_curriculum_job", scheduled_job_ids.append)

    args = {"title": "Test Course", "language": "en", "profile": {"level": "beginner"}}
    fake_provider = _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll generate that curriculum.",
            tool_calls=[ToolCall(id="call_1", name="generate_curriculum", arguments=args)],
        ),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "generate a beginner course"})
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "approve"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "job_pending"
    job_id = uuid.UUID(body["job_id"])

    assert scheduled_job_ids == [job_id]  # BackgroundTasks scheduled with this exact id
    assert len(fake_provider.calls) == 1  # NO resume — still just the original propose call

    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        assert job.kind == "curriculum"
        assert job.status == "pending"
        assert job.params == args
    finally:
        db.close()

    approval = _db_approval(approval_id)
    assert approval.status == "approved"
    assert approval.result_ref == str(job_id)
    assert approval.resolved_at is not None

    rows = _db_messages(session_id)
    tool_msgs = [m for m in rows if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_1"
    assert str(job_id) in tool_msgs[0].content

    # GET /jobs/{id} (Plan 8) already serves this job.
    poll = client.get(f"/jobs/{job_id}")
    assert poll.status_code == 200
    assert poll.json()["status"] == "pending"
