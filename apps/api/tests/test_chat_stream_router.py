"""Tests for `POST /chat/{session_id}/messages/stream` (Plan 11 Task 3, C4)
and its underlying `app.agent.loop.stream_plain_turn` — the SSE token-
streaming path for the plain-answer case, and (the safety-critical half of
this task) proof that everything it can't handle (a tool/mutation call, a
C3 tablature bluff, a mid-stream error) falls back cleanly — persisting
NOTHING — rather than ever touching the HITL suspend machinery
`test_chat_router.py` already covers exhaustively.

Same posture as `test_chat_router.py`: DB-touching (real Postgres), but NO
live LLM — the provider is a scripted fake implementing `chat_tools_stream`
as a generator, monkeypatched into `app.agent.loop.get_provider` the same
way `test_chat_router.py`'s own `_FakeProvider` is. Not marked
`@pytest.mark.integration`.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.agent.loop as agent_loop
from app.db import Base, engine
from app.llm.tools_types import ToolCall
from app.main import app
from app.models.chat import Message

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )


def setup_module(_):
    Base.metadata.create_all(engine)


client = TestClient(app)


class _FakeStreamingProvider:
    """`chat_tools_stream` yields a scripted sequence of `{"type": ...}`
    events verbatim — mirrors the exact contract `qwen.py`'s real
    implementation produces (content deltas, then one terminal "done" with
    the accumulated `content`/`tool_calls`). `chat_tools` (non-streaming) is
    intentionally NOT implemented — this provider only ever exercises the
    streaming call path these tests are about.
    """

    def __init__(self, events):
        self._events = events
        self.calls = 0

    def chat_tools_stream(self, messages, tools, *, tool_choice="auto", temperature=0.3):
        self.calls += 1
        for event in self._events:
            if isinstance(event, Exception):
                raise event
            yield event


def _use_streaming_provider(monkeypatch, events) -> _FakeStreamingProvider:
    fake = _FakeStreamingProvider(events)
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake)
    return fake


def _create_session() -> str:
    r = client.post("/chat", json={})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _parse_sse(body: str) -> list[tuple[str, str]]:
    """`[(event_name, raw_json_data_line), ...]` in order, from a raw SSE
    response body — good enough for these tests' own assertions without
    pulling in a full SSE-parsing dependency."""
    events = []
    for block in body.strip("\n").split("\n\n"):
        if not block.strip():
            continue
        event_name, data_line = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_line = line[len("data:"):].strip()
        if event_name is not None and data_line is not None:
            events.append((event_name, data_line))
    return events


def _db_messages(session_id) -> list[Message]:
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return list(db.query(Message).filter(Message.session_id == uuid.UUID(session_id)).all())
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The happy path: plain answer, streamed, persisted on "done"
# ---------------------------------------------------------------------------

def test_stream_success_yields_deltas_then_done_and_persists_with_citations(monkeypatch):
    _use_streaming_provider(monkeypatch, [
        {"type": "content", "text": "A humbucker "},
        {"type": "content", "text": "cancels hum."},
        {"type": "done", "content": "A humbucker cancels hum.", "tool_calls": []},
    ])
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
    session_id = _create_session()

    r = client.post(f"/chat/{session_id}/messages/stream", json={"content": "what cancels hum?"})

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(r.text)
    kinds = [name for name, _ in events]
    assert kinds == ["delta", "delta", "done"]
    assert '"A humbucker "' in events[0][1]
    assert '"cancels hum."' in events[1][1]

    history = client.get(f"/chat/{session_id}")
    assert history.status_code == 200, history.text
    contents = [(m["role"], m["content"]) for m in history.json()]
    assert ("user", "what cancels hum?") in contents
    assert ("assistant", "A humbucker cancels hum.") in contents


def test_stream_success_with_citations_attaches_them_to_the_assistant_row(monkeypatch):
    class _Hit:
        def __init__(self):
            self.source_id = uuid.uuid4()
            self.source_title = "Getting Great Guitar Sounds"
            self.page = 21
            self.page_id = uuid.uuid4()
            self.text = "A thicker pick gives more attack."
            self.score = 0.9

    _use_streaming_provider(monkeypatch, [
        {"type": "content", "text": "Thicker picks grip harder."},
        {"type": "done", "content": "Thicker picks grip harder.", "tool_calls": []},
    ])
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [_Hit()])
    session_id = _create_session()

    r = client.post(
        f"/chat/{session_id}/messages/stream",
        json={"content": "what does pick thickness do to my tone?"},
    )
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    assert events[-1][0] == "done"
    assert "Getting Great Guitar Sounds" in events[-1][1]
    assert '"page_no": 21' in events[-1][1]

    rows = _db_messages(session_id)
    assistant_rows = [m for m in rows if m.role == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0].citations
    assert assistant_rows[0].citations[0]["source_title"] == "Getting Great Guitar Sounds"
    assert assistant_rows[0].citations[0]["page_no"] == 21


# ---------------------------------------------------------------------------
# Task 8: the streaming path reorders identically to `run_agent_turn` —
# search the library BEFORE declining a named-song request. Both call sites
# have to behave the same way, or the tutor gets a different answer
# depending on whether the UI happened to stream that turn.
# ---------------------------------------------------------------------------

def test_stream_a_song_the_tutor_owns_is_answered_not_declined(monkeypatch):
    class _Hit:
        def __init__(self):
            self.source_id = uuid.uuid4()
            self.source_title = "Real Book"
            self.page = 42
            self.page_id = uuid.uuid4()
            self.text = "Intro riff: E5 G5 A5"
            self.score = 0.9

    _use_streaming_provider(monkeypatch, [
        {"type": "content", "text": "Here's the intro riff from page 42."},
        {"type": "done", "content": "Here's the intro riff from page 42.", "tool_calls": []},
    ])
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [_Hit()])
    session_id = _create_session()

    r = client.post(
        f"/chat/{session_id}/messages/stream",
        json={"content": "give me the tab for Sweet Child O' Mine"},
    )

    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    assert events[-1][0] == "done", "his own book was refused unread"
    assert "Real Book" in events[-1][1]


def test_stream_a_song_the_tutor_does_not_own_is_still_declined(monkeypatch):
    # The model must never even be called for a genuine miss — the fake
    # provider raises if `chat_tools_stream` is invoked.
    def _must_not_stream(*a, **kw):
        raise AssertionError("the model must never be called for a named-song request")

    fake = _FakeStreamingProvider([])
    monkeypatch.setattr(fake, "chat_tools_stream", _must_not_stream)
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake)
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
    session_id = _create_session()

    r = client.post(
        f"/chat/{session_id}/messages/stream",
        json={"content": "give me the tab for Sweet Child O' Mine"},
    )

    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    # The decline IS the final answer (a plain "done", not a "fallback") —
    # no delta was ever streamed because the model was never called, so the
    # decline text itself is only visible in what got persisted, not in the
    # "done" event's own payload (which carries only `citations`).
    assert events[-1][0] == "done"
    from app.agent.guards import NAMED_SONG_DECLINE_MESSAGE
    rows = _db_messages(session_id)
    assistant_rows = [m for m in rows if m.role == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == NAMED_SONG_DECLINE_MESSAGE


# ---------------------------------------------------------------------------
# The honest simplification: everything else falls back, persisting nothing
# ---------------------------------------------------------------------------

def test_stream_falls_back_and_persists_nothing_when_a_tool_call_is_proposed(monkeypatch):
    _use_streaming_provider(monkeypatch, [
        {"type": "content", "text": "Sure, "},
        {
            "type": "done",
            "content": "Sure, ",
            "tool_calls": [ToolCall(id="call_1", name="create_student", arguments={"name": "Maria"})],
        },
    ])
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
    session_id = _create_session()

    r = client.post(f"/chat/{session_id}/messages/stream", json={"content": "add a student named Maria"})

    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    assert events[-1][0] == "fallback"
    assert '"tool_call"' in events[-1][1]
    # Nothing persisted — a "fallback" must leave the transcript exactly as
    # it was before this call, so a REST retry starts clean.
    assert _db_messages(session_id) == []


def test_stream_falls_back_and_persists_nothing_on_a_tablature_bluff(monkeypatch):
    bluff = "Here you go:\n\ne|-----0-2-4-5-7-8-10-\nB|-----0-2-4-5-7-8-10-\n"
    _use_streaming_provider(monkeypatch, [
        {"type": "content", "text": bluff},
        {"type": "done", "content": bluff, "tool_calls": []},
    ])
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
    session_id = _create_session()

    r = client.post(f"/chat/{session_id}/messages/stream", json={"content": "give me a G major scale tab"})

    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    assert events[-1][0] == "fallback"
    assert '"tablature"' in events[-1][1]
    assert _db_messages(session_id) == []


def test_stream_falls_back_and_persists_nothing_on_a_provider_error(monkeypatch):
    _use_streaming_provider(monkeypatch, [
        {"type": "content", "text": "partial "},
        RuntimeError("boom"),
    ])
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
    session_id = _create_session()

    r = client.post(f"/chat/{session_id}/messages/stream", json={"content": "what cancels hum?"})

    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    assert events[-1][0] == "fallback"
    assert '"error"' in events[-1][1]
    assert _db_messages(session_id) == []


# ---------------------------------------------------------------------------
# REGRESSION: the streaming endpoint respects the SAME 409 pending-approval
# guard as the REST endpoint — it is not a side door around the HITL gate.
# ---------------------------------------------------------------------------

def test_stream_unknown_session_404s():
    r = client.post(f"/chat/{uuid.uuid4()}/messages/stream", json={"content": "hi"})
    assert r.status_code == 404, r.text


def test_stream_409s_while_an_approval_is_pending(monkeypatch):
    from app.agent.tools import TOOLS, ToolEntry

    original = TOOLS["create_student"]
    monkeypatch.setitem(
        TOOLS, "create_student",
        ToolEntry(schema=original.schema, fn=lambda db, **kw: {"id": str(uuid.uuid4())},
                  kind=original.kind, async_job=original.async_job),
    )

    class _MutationProvider:
        def chat_tools(self, messages, tools, *, tool_choice="auto", temperature=0.3):
            from app.llm.tools_types import AssistantTurn
            return AssistantTurn(
                content="I'll add that student.",
                tool_calls=[ToolCall(id="call_1", name="create_student", arguments={"name": "Maria"})],
            )

    monkeypatch.setattr(agent_loop, "get_provider", lambda: _MutationProvider())
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
    session_id = _create_session()

    r = client.post(f"/chat/{session_id}/messages", json={"content": "add a student named Maria"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "awaiting_approval"

    r2 = client.post(f"/chat/{session_id}/messages/stream", json={"content": "and another thing"})
    assert r2.status_code == 409, r2.text
