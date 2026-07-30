"""Tests for `/chat` routes (Plan 5 Task 4) — the chat router that persists
conversation state and drives approve/reject/resume + the async-generation
compose. THE integration crux of the chat agent: session create, turn-taking
through `run_agent_turn`, and the HITL approve/reject/resume flow tying
together the ReAct loop (Task 2), the suspend-on-mutation HITL models
(Task 3), and Plan 8's enqueue/poll pattern for the one async mutation
(`generate_curriculum`).

DB-touching (real Postgres, real `Block`/`GenerationJob`/chat-model rows)
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
from app.models.block import Block
from app.models.chat import ApprovalRequest, Message
from app.models.generation_job import GenerationJob

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


def _seed_block(title="Old Title") -> str:
    """A real Block row for the sync-mutation (`update_block`) approve tests —
    the surviving cheap sync mutation now that the desktop build removed the
    student/note tools this suite used to exercise the approve path with."""
    db = SessionLocal()
    try:
        block = Block(kind="lesson", title=title, language="en")
        db.add(block)
        db.commit()
        return str(block.id)
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
    # "what cancels hum?" is CONTENT-BEARING (Plan 11 Task 1, C1) — stub the
    # loop's forced-retrieval pre-hop to no-hits so it doesn't reach the real
    # embedding provider; this test's own concern is history persistence,
    # not grounding (see `test_agent_grounding.py` for that).
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
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

    _stub_tool(monkeypatch, "update_block", _spy)
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll rename that lesson.",
            tool_calls=[ToolCall(id="call_1", name="update_block", arguments={"block_id": "b1", "title": "New Title"})],
        ),
    ])
    session_id = _create_session()

    r = client.post(f"/chat/{session_id}/messages", json={"content": "rename that lesson to New Title"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "awaiting_approval"
    assert body["tool_name"] == "update_block"
    assert body["tool_args"] == {"block_id": "b1", "title": "New Title"}
    assert body["approval_id"]
    assert body["description"]  # non-empty
    assert calls_made == []  # never invoked

    approval = _db_approval(body["approval_id"])
    assert approval is not None
    assert approval.status == "pending"
    assert approval.tool_name == "update_block"
    assert approval.tool_args == {"block_id": "b1", "title": "New Title"}
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
            content="I'll rename that lesson.",
            tool_calls=[ToolCall(id="call_1", name="update_block", arguments={"block_id": "b1", "title": "New"})],
        ),
    ])
    owning_session_id = _create_session()
    propose = client.post(f"/chat/{owning_session_id}/messages", json={"content": "rename that lesson"})
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
# resolve approve — SYNC mutation (update_block, real fn)
# ---------------------------------------------------------------------------

def test_resolve_approve_sync_mutation_runs_the_real_fn_and_resumes(monkeypatch):
    block_id = _seed_block(title="Old Title")
    fake_provider = _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll rename that lesson.",
            tool_calls=[ToolCall(id="call_1", name="update_block", arguments={"block_id": block_id, "title": "Renamed Lesson"})],
        ),
        AssistantTurn(content="Renamed it to Renamed Lesson.", tool_calls=[]),
    ])
    session_id = _create_session()

    propose = client.post(f"/chat/{session_id}/messages", json={"content": "rename that lesson"})
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "approve"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "answer"
    assert body["content"] == "Renamed it to Renamed Lesson."
    assert len(fake_provider.calls) == 2  # the loop genuinely resumed

    # The REAL fn ran: the Block row is renamed.
    db = SessionLocal()
    try:
        assert db.get(Block, uuid.UUID(block_id)).title == "Renamed Lesson"
    finally:
        db.close()

    approval = _db_approval(approval_id)
    assert approval.status == "approved"
    assert approval.result_ref == block_id
    assert approval.resolved_at is not None

    # A tool message answering the pending call_id is now in history.
    rows = _db_messages(session_id)
    tool_msgs = [m for m in rows if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_1"
    assert block_id in tool_msgs[0].content

    # No longer pending.
    assert client.get(f"/chat/{session_id}/pending").json() is None


def test_resolve_approve_uses_edited_args_when_given(monkeypatch):
    block_id = _seed_block(title="Old Title")
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll rename that lesson.",
            tool_calls=[ToolCall(id="call_1", name="update_block", arguments={"block_id": block_id, "title": "Wrong Name"})],
        ),
        AssistantTurn(content="Renamed it to Right Name.", tool_calls=[]),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "rename to Wrong Name"})
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve",
        json={"decision": "approve", "edited_args": {"block_id": block_id, "title": "Right Name"}},
    )

    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        assert db.get(Block, uuid.UUID(block_id)).title == "Right Name"
    finally:
        db.close()

    approval = _db_approval(approval_id)
    assert approval.edited_args == {"block_id": block_id, "title": "Right Name"}


# ---------------------------------------------------------------------------
# resolve approve — mutation fn RAISES: no 500, graceful narration
# ---------------------------------------------------------------------------

def test_resolve_approve_when_fn_raises_records_error_and_narrates_gracefully(monkeypatch):
    def _boom(db, **kwargs):
        raise ValueError("boom: something went wrong")

    _stub_tool(monkeypatch, "update_block", _boom)
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll rename that lesson.",
            tool_calls=[ToolCall(id="call_1", name="update_block", arguments={"block_id": "b1", "title": "New"})],
        ),
        AssistantTurn(content="Sorry, I couldn't rename that lesson.", tool_calls=[]),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "rename that lesson"})
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "approve"},
    )

    assert r.status_code != 500
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "answer"
    assert body["content"] == "Sorry, I couldn't rename that lesson."

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

    _stub_tool(monkeypatch, "update_block", _spy)
    fake_provider = _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll rename that lesson.",
            tool_calls=[ToolCall(id="call_1", name="update_block", arguments={"block_id": "b1", "title": "New"})],
        ),
        AssistantTurn(content="No problem, I won't rename it.", tool_calls=[]),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "rename that lesson"})
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "reject"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "answer"
    assert body["content"] == "No problem, I won't rename it."
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
            content="I'll rename that lesson.",
            tool_calls=[ToolCall(id="call_1", name="update_block", arguments={"block_id": "b1", "title": "New"})],
        ),
        AssistantTurn(content="No problem.", tool_calls=[]),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "rename that lesson"})
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

    # The model still *tries* to pick a language (an old transcript, a
    # hallucinated arg — the parameter is gone from the schema, not from the
    # universe). The session's locale overrides it, at suspend AND at resolve
    # (Plan 13, Stage 5.4): the enqueued job must carry `el`, not the "en" the
    # model asked for, or a Greek tutor gets an English curriculum.
    args = {"title": "Test Course", "language": "en", "profile": {"level": "beginner"}}
    expected_params = {**args, "language": "el"}
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
        assert job.params == expected_params
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


# ---------------------------------------------------------------------------
# resolve approve — ASYNC draft_lesson_from_selection: job_pending, no resume
#
# Review fix (found during Plan 10 Task 3): `resolve_approval`'s async-job
# branch used to be hardcoded for `generate_curriculum` only — ANY
# `async_job=True` tool got `kind="curriculum"` + `run_curriculum_job`, so
# approving a `draft_lesson_from_selection` proposal enqueued the WRONG job
# kind/runner (a curriculum job run against lesson params -> TypeError ->
# job status="failed", no crash but a misleading result). This is the
# regression test proving the second async tool now gets its OWN job kind
# and OWN runner.
# ---------------------------------------------------------------------------

def test_resolve_approve_async_draft_lesson_enqueues_a_lesson_job_and_does_not_resume(monkeypatch):
    curriculum_calls = []
    lesson_calls = []  # (job_id, "was the job row already committed/visible from a fresh session")

    def _fake_curriculum_runner(job_id):
        curriculum_calls.append(job_id)

    def _fake_lesson_runner(job_id):
        # Mirrors the REAL run_lesson_job's own session ownership (it opens
        # its OWN SessionLocal() — see app/jobs/runner.py's docstring): if
        # resolve_approval scheduled this background task BEFORE committing
        # the GenerationJob row, this fresh session would see nothing.
        fresh_db = SessionLocal()
        try:
            lesson_calls.append((job_id, fresh_db.get(GenerationJob, job_id) is not None))
        finally:
            fresh_db.close()

    monkeypatch.setattr(chat_router, "run_curriculum_job", _fake_curriculum_runner)
    monkeypatch.setattr(chat_router, "run_lesson_job", _fake_lesson_runner)

    args = {"source_id": str(uuid.uuid4()), "page_no": 3, "text": "Some selected passage."}
    # Same locale injection as the curriculum case above — the lesson job's
    # params carry the session's language even though the model never sent one
    # (the tool schema no longer offers it).
    expected_params = {**args, "language": "el"}
    fake_provider = _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll draft that lesson.",
            tool_calls=[ToolCall(id="call_1", name="draft_lesson_from_selection", arguments=args)],
        ),
    ])
    session_id = _create_session()
    propose = client.post(
        f"/chat/{session_id}/messages", json={"content": "draft a lesson from that passage"},
    )
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "approve"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "job_pending"
    job_id = uuid.UUID(body["job_id"])

    # The LESSON runner ran, exactly once, with the job already committed —
    # the CURRICULUM runner must never have been touched by this approval.
    assert lesson_calls == [(job_id, True)]
    assert curriculum_calls == []
    assert len(fake_provider.calls) == 1  # NO resume — still just the original propose call

    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        assert job.kind == "lesson"
        assert job.status == "pending"
        assert job.params == expected_params
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

    poll = client.get(f"/jobs/{job_id}")
    assert poll.status_code == 200
    assert poll.json()["status"] == "pending"


# ---------------------------------------------------------------------------
# post_message while an approval is still pending -> 409 (review Important)
# ---------------------------------------------------------------------------

def test_post_message_while_an_approval_is_pending_409s_then_ok_after_resolve(monkeypatch):
    """A second `POST .../messages` while an `ApprovalRequest` is still
    `pending` must 409, not run another turn: the transcript ends with an
    UNANSWERED assistant tool-calls turn (the suspended mutation), so
    persisting a new `user` row after it and handing `...assistant(tool_calls),
    user(new)` to `chat_tools` is an out-of-protocol shape (an assistant
    tool-calls turn must be answered by its `tool` messages before any user
    turn) — vLLM would 500 or silently degrade, and whatever it returned
    would get PERSISTED, durably corrupting the transcript. The user must
    resolve the pending approval first.
    """
    def _spy(db, **kwargs):
        raise AssertionError("mutation fn must not be called")

    _stub_tool(monkeypatch, "update_block", _spy)
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll rename that lesson.",
            tool_calls=[ToolCall(id="call_1", name="update_block", arguments={"block_id": "b1", "title": "New"})],
        ),
        AssistantTurn(content="Okay, I won't rename it.", tool_calls=[]),
    ])
    session_id = _create_session()

    first = client.post(f"/chat/{session_id}/messages", json={"content": "rename that lesson"})
    assert first.json()["status"] == "awaiting_approval"
    approval_id = first.json()["approval_id"]

    blocked = client.post(f"/chat/{session_id}/messages", json={"content": "actually, wait"})
    assert blocked.status_code == 409, blocked.text

    # The blocked message was NOT persisted (we bailed before writing it).
    contents = [m.content for m in _db_messages(session_id)]
    assert "actually, wait" not in contents

    # Resolving the pending approval unblocks the next message.
    resolve = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "reject"},
    )
    assert resolve.status_code == 200, resolve.text

    ok = client.post(f"/chat/{session_id}/messages", json={"content": "hello again"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "answer"


# ---------------------------------------------------------------------------
# resolve approve — SYNC mutation fn RETURNS a graceful {"error": ...} dict
# (review Minor: audit-status parity with the raise path)
# ---------------------------------------------------------------------------

def test_resolve_approve_when_fn_returns_error_dict_records_error_status(monkeypatch):
    """A mutation fn can RETURN a graceful `{"error": ...}` dict (bad/
    hallucinated UUID, not-found, empty-title — tools.py's `{"error"}` paths)
    instead of raising. That is a failed action too, so the approval must be
    recorded `status="error"` (same value the raise path uses), not
    "approved" with a misleading `result_ref=None` that's indistinguishable
    from a real no-id success. The tool message still carries the error dict
    so the model narrates it.
    """
    def _returns_error(db, **kwargs):
        return {"error": "block not found"}

    _stub_tool(monkeypatch, "update_block", _returns_error)
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll update that lesson.",
            tool_calls=[ToolCall(
                id="call_1", name="update_block",
                arguments={"block_id": "00000000-0000-0000-0000-000000000000", "title": "New"},
            )],
        ),
        AssistantTurn(content="I couldn't find that lesson.", tool_calls=[]),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "update that lesson"})
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "approve"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "answer"

    approval = _db_approval(approval_id)
    assert approval.status == "error"
    assert approval.result_ref is None

    rows = _db_messages(session_id)
    tool_msgs = [m for m in rows if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_1"
    assert "block not found" in tool_msgs[0].content  # model sees + narrates it


# ---------------------------------------------------------------------------
# coverage for the two previously-unexercised paths (review Optional)
# ---------------------------------------------------------------------------

def test_resolve_approve_when_fn_flushes_then_raises_rolls_back_partial_writes(monkeypatch):
    """Exercises the actual mid-flush-rollback purpose of the resolve
    endpoint's `db.rollback()` + re-fetch: a mutation fn that FLUSHES a row
    into the session and THEN raises (unlike the raise-before-any-write case
    the other error test covers). The flushed-but-uncommitted row must be
    rolled back (not leaked), AND the error tool-message + `status="error"`
    must still land afterward (proving the post-rollback re-fetch works).
    """
    def _flush_then_raise(db, **kwargs):
        db.add(Block(kind="lesson", title="Partial Ghost", language="en"))
        db.flush()  # partial, uncommitted write now in the session
        raise ValueError("boom after flush")

    _stub_tool(monkeypatch, "update_block", _flush_then_raise)
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll update that lesson.",
            tool_calls=[ToolCall(id="call_1", name="update_block", arguments={"block_id": "b1", "title": "Partial Ghost"})],
        ),
        AssistantTurn(content="Sorry, that failed.", tool_calls=[]),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "update that lesson"})
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "approve"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "answer"

    # The flushed-but-uncommitted Block was rolled back — not leaked.
    db = SessionLocal()
    try:
        assert db.query(Block).filter(Block.title == "Partial Ghost").count() == 0
    finally:
        db.close()

    approval = _db_approval(approval_id)
    assert approval.status == "error"
    assert approval.resolved_at is not None

    rows = _db_messages(session_id)
    tool_msgs = [m for m in rows if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content.startswith("ERROR:")


def test_resume_after_approve_can_propose_a_second_mutation_creating_a_new_pending(monkeypatch):
    """A RESUMED turn (after a sync approve) can itself immediately propose
    ANOTHER mutation — `_respond_to_turn` must react the same way as the
    original suspend: persist the new assistant tool-call turn and create a
    FRESH `ApprovalRequest`, so the second mutation is trackable/resolvable
    via `GET .../pending` rather than left as an orphan unanswered tool_call.
    """
    block_id = _seed_block(title="Chained Lesson")
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll rename that lesson.",
            tool_calls=[ToolCall(id="call_1", name="update_block", arguments={"block_id": block_id, "title": "Chained Kid"})],
        ),
        AssistantTurn(
            content="Now I'll segment that block.",
            tool_calls=[ToolCall(
                id="call_2", name="segment_block",
                arguments={"block_id": "b1", "session_minutes": 30},
            )],
        ),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "rename that lesson then segment"})
    first_approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{first_approval_id}/resolve", json={"decision": "approve"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "awaiting_approval"
    assert body["tool_name"] == "segment_block"
    second_approval_id = body["approval_id"]
    assert second_approval_id != first_approval_id

    # First approved (its real fn ran — the Block is renamed); second pending.
    assert _db_approval(first_approval_id).status == "approved"
    db = SessionLocal()
    try:
        assert db.get(Block, uuid.UUID(block_id)).title == "Chained Kid"
    finally:
        db.close()

    second = _db_approval(second_approval_id)
    assert second.status == "pending"
    assert second.tool_call_id == "call_2"

    # GET /pending now surfaces the SECOND (most-recent) pending approval.
    pending = client.get(f"/chat/{session_id}/pending")
    assert pending.json()["id"] == second_approval_id


# ---------------------------------------------------------------------------
# resolve approve — segment_block with many sessions must not overflow
# ApprovalRequest.result_ref (String(255)) → 500 + bricked session (review Critical)
# ---------------------------------------------------------------------------

def test_resolve_approve_segment_block_with_many_sessions_does_not_overflow_result_ref(monkeypatch):
    """Regression: `segment_block` commonly yields many session ids — a 3.5h
    curriculum at 30min/session = 7 ids. `_result_ref` must NOT join them all
    into `ApprovalRequest.result_ref`: 7 × 36-char UUID + ", " separators =
    264 chars, over the column's VARCHAR(255) cap. That assignment is
    committed by `persist_new_messages` OUTSIDE the fn-error try/except (the fn
    already succeeded), so the overflow raises `DataError` as a raw 500 AND
    rolls back the approval status-flip + the tool-result persist — leaving a
    dangling unanswered tool_call + a forever-`pending` approval that
    `post_message` now 409s behind = a bricked session with no in-app escape.
    `_result_ref` must record a BOUNDED summary instead (the full ids are
    already in the tool-message content; result_ref is best-effort opaque
    bookkeeping).
    """
    fake_session_ids = [str(uuid.uuid4()) for _ in range(7)]  # 7 × 36 + separators = 264 > 255

    def _fake_segment(db, **kwargs):
        return {"session_ids": fake_session_ids}

    _stub_tool(monkeypatch, "segment_block", _fake_segment)
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll segment that block into sessions.",
            tool_calls=[ToolCall(
                id="call_1", name="segment_block",
                arguments={"block_id": "b1", "session_minutes": 30},
            )],
        ),
        AssistantTurn(content="Done — I split it into 7 sessions.", tool_calls=[]),
    ])
    session_id = _create_session()
    propose = client.post(f"/chat/{session_id}/messages", json={"content": "segment that block at 30 min"})
    approval_id = propose.json()["approval_id"]

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve", json={"decision": "approve"},
    )

    assert r.status_code == 200, r.text  # NOT a 500 from a VARCHAR(255) overflow
    assert r.json()["status"] == "answer"

    approval = _db_approval(approval_id)
    assert approval.status == "approved"  # flip committed (not rolled back)
    assert approval.result_ref is not None
    assert len(approval.result_ref) <= 255  # bounded, fits the column

    # The pending tool_call was answered + persisted (transcript not dangling).
    rows = _db_messages(session_id)
    tool_msgs = [m for m in rows if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_1"

    # And the session is not bricked: no longer pending.
    assert client.get(f"/chat/{session_id}/pending").json() is None
