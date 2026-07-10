"""Tests for `app.agent.transcript` (Plan 5 Task 4): the lossless round-trip
between persisted `Message` rows and the OpenAI wire-shape transcript
`run_agent_turn` (`app.agent.loop`) consumes/produces.

DB-touching (real `Message`/`ChatSession` rows) but NO live LLM anywhere in
this module — not marked `@pytest.mark.integration`, same precedent as
`test_chat_models.py`/`test_agent_mutation_tools.py`. Mirrors their
skip-guard + `setup_module` pattern.
"""
import pytest
from sqlalchemy import select, text

from app.db import Base, SessionLocal, engine

# Skip cleanly (not error) when no DB is reachable — mirrors test_chat_models.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )

import app.models  # noqa: F401  register every model's table on Base.metadata
from app.agent.transcript import messages_to_wire, persist_new_messages
from app.models.chat import ChatSession, Message


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


def _make_session() -> "ChatSession":
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        return session
    finally:
        db.close()


def _reload_ordered(session_id) -> list[Message]:
    db = SessionLocal()
    try:
        return db.scalars(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at, Message.id)
        ).all()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# messages_to_wire: Message rows -> wire dicts
# ---------------------------------------------------------------------------

def test_messages_to_wire_reconstructs_a_plain_user_message():
    session = _make_session()
    db = SessionLocal()
    try:
        db.add(Message(session_id=session.id, role="user", content="hi there"))
        db.commit()
    finally:
        db.close()

    rows = _reload_ordered(session.id)
    wire = messages_to_wire(rows)

    assert wire == [{"role": "user", "content": "hi there"}]


def test_messages_to_wire_reconstructs_a_plain_assistant_message_with_no_tool_calls_key():
    """A plain-text assistant reply has NO `tool_calls` key at all (omitted
    entirely, matching `loop.py`'s own `_wire_assistant_message` contract —
    not an empty list, not a null value).
    """
    session = _make_session()
    db = SessionLocal()
    try:
        db.add(Message(session_id=session.id, role="assistant", content="hello!", tool_calls=None))
        db.commit()
    finally:
        db.close()

    wire = messages_to_wire(_reload_ordered(session.id))

    assert wire == [{"role": "assistant", "content": "hello!"}]
    assert "tool_calls" not in wire[0]


def test_messages_to_wire_reconstructs_an_assistant_message_with_tool_calls():
    tool_calls = [{
        "id": "call_1", "type": "function",
        "function": {"name": "search_knowledge", "arguments": '{"query": "tone"}'},
    }]
    session = _make_session()
    db = SessionLocal()
    try:
        db.add(Message(
            session_id=session.id, role="assistant", content=None, tool_calls=tool_calls,
        ))
        db.commit()
    finally:
        db.close()

    wire = messages_to_wire(_reload_ordered(session.id))

    assert wire == [{"role": "assistant", "content": None, "tool_calls": tool_calls}]


def test_messages_to_wire_reconstructs_a_tool_message():
    session = _make_session()
    db = SessionLocal()
    try:
        db.add(Message(
            session_id=session.id, role="tool", content="42", tool_call_id="call_1",
        ))
        db.commit()
    finally:
        db.close()

    wire = messages_to_wire(_reload_ordered(session.id))

    assert wire == [{"role": "tool", "tool_call_id": "call_1", "content": "42"}]


def test_messages_to_wire_preserves_row_order():
    session = _make_session()
    db = SessionLocal()
    try:
        db.add(Message(session_id=session.id, role="user", content="one"))
        db.commit()
        db.add(Message(session_id=session.id, role="assistant", content="two"))
        db.commit()
        db.add(Message(session_id=session.id, role="user", content="three"))
        db.commit()
    finally:
        db.close()

    wire = messages_to_wire(_reload_ordered(session.id))

    assert [m["content"] for m in wire] == ["one", "two", "three"]


def test_messages_to_wire_on_empty_rows_returns_empty_list():
    assert messages_to_wire([]) == []


# ---------------------------------------------------------------------------
# persist_new_messages: wire dicts -> Message rows
# ---------------------------------------------------------------------------

def test_persist_new_messages_persists_a_user_message():
    session = _make_session()

    persist_new_messages(db := SessionLocal(), session.id, [{"role": "user", "content": "hi"}])
    db.close()

    rows = _reload_ordered(session.id)
    assert len(rows) == 1
    assert rows[0].role == "user"
    assert rows[0].content == "hi"
    assert rows[0].tool_calls is None
    assert rows[0].tool_call_id is None


def test_persist_new_messages_persists_an_assistant_message_with_tool_calls():
    session = _make_session()
    tool_calls = [{
        "id": "call_9", "type": "function",
        "function": {"name": "list_students", "arguments": "{}"},
    }]
    db = SessionLocal()
    persist_new_messages(
        db, session.id,
        [{"role": "assistant", "content": None, "tool_calls": tool_calls}],
    )
    db.close()

    rows = _reload_ordered(session.id)
    assert len(rows) == 1
    assert rows[0].role == "assistant"
    assert rows[0].content is None
    assert rows[0].tool_calls == tool_calls
    assert rows[0].tool_call_id is None


def test_persist_new_messages_persists_a_tool_message_with_tool_call_id():
    session = _make_session()
    db = SessionLocal()
    persist_new_messages(
        db, session.id,
        [{"role": "tool", "tool_call_id": "call_9", "content": "[1, 2, 3]"}],
    )
    db.close()

    rows = _reload_ordered(session.id)
    assert len(rows) == 1
    assert rows[0].role == "tool"
    assert rows[0].tool_call_id == "call_9"
    assert rows[0].content == "[1, 2, 3]"


def test_persist_new_messages_skips_a_system_entry():
    """The system prompt is never persisted (`run_agent_turn` re-prepends it
    every call — `loop.py`'s `_ensure_system_prompt`); a `{"role": "system"}`
    wire entry must be silently skipped, not stored as a row.
    """
    session = _make_session()
    db = SessionLocal()
    persist_new_messages(
        db, session.id,
        [
            {"role": "system", "content": "you are a helpful assistant"},
            {"role": "user", "content": "hi"},
        ],
    )
    db.close()

    rows = _reload_ordered(session.id)
    assert len(rows) == 1
    assert rows[0].role == "user"


def test_persist_new_messages_returns_the_created_rows_in_order():
    session = _make_session()
    db = SessionLocal()
    created = persist_new_messages(
        db, session.id,
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
    )
    db.close()

    assert [m.content for m in created] == ["a", "b"]
    assert all(m.id is not None for m in created)  # genuinely persisted, ids assigned


def test_persist_new_messages_preserves_order_across_a_batch():
    """Regression guard for a real footgun: `TimestampMixin.created_at` is
    `server_default=func.now()` — Postgres's TRANSACTION-start time, IDENTICAL
    for every row written in one transaction. If `persist_new_messages`
    committed once for the whole batch instead of once per row, every row in
    this batch would share one `created_at` and reloading `ORDER BY
    created_at, id` would scramble their order (`id` is a random uuid4, not
    time-ordered). This pins that a 3-message batch reloads in the exact
    order it was given.
    """
    session = _make_session()
    db = SessionLocal()
    persist_new_messages(
        db, session.id,
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "tool", "tool_call_id": "call_1", "content": "third"},
        ],
    )
    db.close()

    rows = _reload_ordered(session.id)
    assert [r.content for r in rows] == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# The exact round-trip (this task's headline correctness requirement)
# ---------------------------------------------------------------------------

def test_exact_round_trip_mixed_transcript():
    """persist a mixed transcript (user + assistant-with-tool_calls +
    tool-result) then `messages_to_wire` reproduces the EXACT same wire list
    that was handed to `persist_new_messages` — the round-trip contract this
    task's brief calls out as the thing to test directly.
    """
    session = _make_session()
    tool_calls = [{
        "id": "call_42", "type": "function",
        "function": {"name": "search_knowledge", "arguments": '{"query": "humbucker"}'},
    }]
    original_wire = [
        {"role": "user", "content": "what cancels hum?"},
        {"role": "assistant", "content": None, "tool_calls": tool_calls},
        {"role": "tool", "tool_call_id": "call_42", "content": "a humbucker cancels hum"},
        {"role": "assistant", "content": "A humbucker cancels hum."},
    ]

    db = SessionLocal()
    persist_new_messages(db, session.id, original_wire)
    db.close()

    reloaded_wire = messages_to_wire(_reload_ordered(session.id))

    assert reloaded_wire == original_wire
