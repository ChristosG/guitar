"""Chat history: list / rename / delete, and the two things the sidebar's
correctness actually rests on (Plan 13 Stage 5.6).

The backend already persisted every turn; nothing ever read it back. These
tests pin the read path, and in particular the two behaviours a naive
implementation gets wrong:

1. **Zero-message sessions must not be listed.** The old UI created a session
   on EVERY mount of the chat page, so both the deployed database and the new
   one (which still creates one when the tutor clicks "new chat" and walks
   away) are full of empty sessions that are not conversations.

2. **A session with a PENDING APPROVAL must be fully reconstructible from the
   read endpoints alone** — `GET /chat/{id}/pending` for the tool call, and
   the TRAILING ASSISTANT ROW of `GET /chat/{id}` for the card's description.
   The frontend has no other source for that description, and no field was
   added to `MessageOut` to carry it (a required field there would
   `ResponseValidationError` on every call — that endpoint returns raw ORM
   rows under `from_attributes`).

Same shape as `test_chat_router.py`: real Postgres, a scripted fake provider,
no live LLM (so no `@pytest.mark.integration`).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.agent.loop as agent_loop
from app.agent.tools import TOOLS, ToolEntry
from app.db import Base, SessionLocal, engine
from app.llm.tools_types import AssistantTurn, ToolCall
from app.main import app
from app.models.chat import ApprovalRequest, ChatSession, Message

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


class _FakeProvider:
    """Mirrors `test_chat_router.py`'s own — one scripted `AssistantTurn` per
    call, repeating the last once exhausted.
    """

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls: list[list[dict]] = []

    def chat_tools(self, messages, tools, *, tool_choice="auto", temperature=0.3):
        self.calls.append([dict(m) for m in messages])
        return self._turns[min(len(self.calls) - 1, len(self._turns) - 1)]


def _use_provider(monkeypatch, turns) -> _FakeProvider:
    fake = _FakeProvider(turns)
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake)
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
    return fake


def _stub_tool(monkeypatch, name: str, fn):
    original = TOOLS[name]
    monkeypatch.setitem(
        TOOLS, name,
        ToolEntry(schema=original.schema, fn=fn, kind=original.kind, async_job=original.async_job),
    )


def _create_session(**payload) -> str:
    r = client.post("/chat", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _seed(session_id: str, turns: list[tuple[str, str | None]]) -> None:
    """Write `(role, content)` message rows directly — the transcript's
    CONTENT is what the sidebar summarizes, and driving every fixture through
    the agent loop would only test the loop again.
    """
    db = SessionLocal()
    try:
        for role, content in turns:
            db.add(Message(session_id=uuid.UUID(session_id), role=role, content=content))
            db.commit()  # one commit per row: `created_at` must strictly increase
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /chat — the sidebar list
# ---------------------------------------------------------------------------

def test_list_excludes_sessions_with_no_messages():
    empty = _create_session()
    used = _create_session()
    _seed(used, [("user", "πώς κουρδίζω;"), ("assistant", "Με κουρδιστήρι.")])

    r = client.get("/chat")
    assert r.status_code == 200, r.text
    ids = [row["id"] for row in r.json()]
    assert ids == [used]
    assert empty not in ids


def test_list_is_ordered_by_last_activity_not_creation():
    older = _create_session()
    newer = _create_session()
    _seed(newer, [("user", "second session")])
    _seed(older, [("user", "first session, but spoken to last")])

    ids = [row["id"] for row in client.get("/chat").json()]
    assert ids == [older, newer]


def test_list_row_carries_count_preview_and_locale():
    session_id = _create_session(locale="en")
    _seed(session_id, [
        ("user", "how do I tune?"),
        ("assistant", "Use a tuner."),
        # A tool row is internal plumbing: it must not be counted, and must
        # not become the preview.
        ("tool", "{\"ok\": true}"),
    ])

    row = client.get("/chat").json()[0]
    assert row["id"] == session_id
    assert row["locale"] == "en"
    assert row["message_count"] == 2
    assert row["preview"] == "Use a tuner."
    assert row["last_message_at"]


def test_list_preview_skips_a_contentless_assistant_row():
    """A suspended mutation's assistant turn can have `content=None` (tool
    calls only). It is the NEWEST visible row, so a naive "last message"
    preview would come back blank.
    """
    session_id = _create_session()
    _seed(session_id, [("user", "add a student"), ("assistant", None)])

    row = client.get("/chat").json()[0]
    assert row["preview"] == "add a student"


# ---------------------------------------------------------------------------
# Title — a truncation of the first user message, no model call
# ---------------------------------------------------------------------------

def test_first_user_message_titles_the_session(monkeypatch):
    _use_provider(monkeypatch, [AssistantTurn(content="Ναι.", tool_calls=[])])
    session_id = _create_session(locale="el")

    client.post(f"/chat/{session_id}/messages", json={"content": "Θέλω πρόγραμμα για αρχάριο"})
    client.post(f"/chat/{session_id}/messages", json={"content": "και για προχωρημένο"})

    row = client.get("/chat").json()[0]
    assert row["title"] == "Θέλω πρόγραμμα για αρχάριο"  # NOT overwritten by the second turn


def test_a_long_first_message_is_truncated_with_an_ellipsis(monkeypatch):
    _use_provider(monkeypatch, [AssistantTurn(content="ok", tool_calls=[])])
    session_id = _create_session()

    client.post(f"/chat/{session_id}/messages", json={"content": "word " * 40})

    title = client.get("/chat").json()[0]["title"]
    assert title.endswith("…")
    assert len(title) <= 61  # 60 chars + the ellipsis


# ---------------------------------------------------------------------------
# PATCH /chat/{id} — rename
# ---------------------------------------------------------------------------

def test_rename_persists():
    session_id = _create_session()
    _seed(session_id, [("user", "hello")])

    r = client.patch(f"/chat/{session_id}", json={"title": "Beginner plan"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Beginner plan"
    assert client.get("/chat").json()[0]["title"] == "Beginner plan"


def test_rename_rejects_a_blank_title():
    session_id = _create_session()
    r = client.patch(f"/chat/{session_id}", json={"title": "   "})
    assert r.status_code == 422, r.text


def test_rename_unknown_session_404s():
    r = client.patch(f"/chat/{uuid.uuid4()}", json={"title": "x"})
    assert r.status_code == 404, r.text


def test_a_later_turn_does_not_overwrite_a_rename(monkeypatch):
    _use_provider(monkeypatch, [AssistantTurn(content="ok", tool_calls=[])])
    session_id = _create_session()
    client.patch(f"/chat/{session_id}", json={"title": "My name for this"})

    client.post(f"/chat/{session_id}/messages", json={"content": "first message ever"})

    assert client.get("/chat").json()[0]["title"] == "My name for this"


# ---------------------------------------------------------------------------
# DELETE /chat/{id} — cascade
# ---------------------------------------------------------------------------

def test_delete_cascades_messages_and_approvals():
    session_id = _create_session()
    _seed(session_id, [("user", "hi"), ("assistant", "hello")])
    db = SessionLocal()
    try:
        db.add(ApprovalRequest(
            session_id=uuid.UUID(session_id), tool_name="update_block",
            tool_args={"block_id": "b1", "title": "Nikos"}, tool_call_id="call_1", status="pending",
        ))
        db.commit()
    finally:
        db.close()

    r = client.delete(f"/chat/{session_id}")
    assert r.status_code == 204, r.text

    db = SessionLocal()
    try:
        sid = uuid.UUID(session_id)
        assert db.get(ChatSession, sid) is None
        assert db.query(Message).filter(Message.session_id == sid).count() == 0
        assert db.query(ApprovalRequest).filter(ApprovalRequest.session_id == sid).count() == 0
    finally:
        db.close()

    assert client.get(f"/chat/{session_id}").status_code == 404
    assert client.get("/chat").json() == []


def test_delete_unknown_session_404s():
    assert client.delete(f"/chat/{uuid.uuid4()}").status_code == 404


# ---------------------------------------------------------------------------
# Resuming a session that is mid-approval — the whole point of the slice
# ---------------------------------------------------------------------------

def test_a_pending_approval_is_fully_reconstructible_from_the_read_endpoints(monkeypatch):
    """The reload case. After a suspend, a browser that knows only the session
    id must be able to rebuild the HITL card exactly as it was: the tool call
    from `GET .../pending`, the description from the trailing assistant row of
    `GET /chat/{id}` — and no new `MessageOut` field to carry it.
    """
    _use_provider(monkeypatch, [
        AssistantTurn(
            content="I'll rename that lesson to Nikos.",
            tool_calls=[ToolCall(id="call_1", name="update_block",
                                 arguments={"block_id": "b1", "title": "Nikos"})],
        ),
    ])
    _stub_tool(monkeypatch, "update_block", lambda db, **kw: {"id": str(uuid.uuid4())})
    session_id = _create_session()

    turn = client.post(f"/chat/{session_id}/messages", json={"content": "rename that lesson"}).json()
    assert turn["status"] == "awaiting_approval"

    # --- a reload: only the session id survives ---
    pending = client.get(f"/chat/{session_id}/pending")
    assert pending.status_code == 200, pending.text
    assert pending.json()["tool_name"] == "update_block"
    assert pending.json()["tool_args"] == {"block_id": "b1", "title": "Nikos"}

    history = client.get(f"/chat/{session_id}")
    assert history.status_code == 200, history.text  # no ResponseValidationError
    rows = history.json()
    assert rows[-1]["role"] == "assistant"
    assert rows[-1]["content"] == turn["description"] == "I'll rename that lesson to Nikos."

    # ...and the resumed session still completes its turn.
    resolved = client.post(
        f"/chat/{session_id}/approvals/{turn['approval_id']}/resolve",
        json={"decision": "approve"},
    )
    assert resolved.status_code == 200, resolved.text


def test_pending_is_null_for_a_session_with_nothing_open():
    session_id = _create_session()
    r = client.get(f"/chat/{session_id}/pending")
    assert r.status_code == 200
    assert r.json() is None
