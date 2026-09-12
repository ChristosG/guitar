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
        "function": {"name": "list_curricula", "arguments": "{}"},
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


# ---------------------------------------------------------------------------
# window_wire — the context cap (review fix: unbounded history eventually
# overflowed the model window and permanently bricked the session)
# ---------------------------------------------------------------------------

from app.agent.transcript import window_wire  # noqa: E402


def _turn(i: int) -> list[dict]:
    return [
        {"role": "user", "content": f"q{i}"},
        {"role": "assistant", "content": f"a{i}"},
    ]


def test_window_wire_passes_short_transcripts_through_unchanged():
    wire = _turn(1) + _turn(2)
    assert window_wire(wire, limit=60) is wire


def test_window_wire_caps_and_starts_at_a_user_turn():
    wire: list[dict] = []
    for i in range(100):
        wire += _turn(i)
    out = window_wire(wire, limit=60)
    assert len(out) <= 60
    assert out[0]["role"] == "user"
    # The most recent turn always survives.
    assert out[-1]["content"] == "a99"


def test_window_wire_never_orphans_a_tool_result():
    """The window's left edge must not cut between an assistant tool_calls
    message and the tool rows that answer it — that transcript shape raises
    DanglingToolUseError at the Anthropic wire layer."""
    wire: list[dict] = []
    for i in range(30):
        wire += [
            {"role": "user", "content": f"q{i}"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": f"c{i}", "type": "function",
                             "function": {"name": "find_lesson", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": "[]"},
            {"role": "assistant", "content": f"a{i}"},
        ]
    out = window_wire(wire, limit=10)
    assert out[0]["role"] == "user"
    # Every tool row in the window is preceded (somewhere after the window
    # start) by the assistant message carrying its tool_call id.
    seen_calls: set[str] = set()
    for m in out:
        if m["role"] == "assistant" and m.get("tool_calls"):
            seen_calls.update(c["id"] for c in m["tool_calls"])
        if m["role"] == "tool":
            assert m["tool_call_id"] in seen_calls, "orphaned tool result in window"


def test_citations_attach_to_the_last_assistant_row_even_when_tools_follow(db_session=None):
    """The suspend path's tail legitimately ends with tool rows; citations must
    land on the assistant row inside the tail, not be dropped."""
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        tail = [
            {"role": "assistant", "content": "grounded answer",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "find_lesson", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "[]"},
        ]
        cites = [{"source_id": "s1", "source_title": "Book", "page_no": 12}]
        rows = persist_new_messages(db, session.id, tail, citations=cites)
        assert rows[-1].role == "tool"
        assert rows[0].citations == cites
    finally:
        db.close()


from app.agent.transcript import window_wire, MAX_WIRE_CHARS


def _char_turn(i: int, tool_chars: int = 0) -> list[dict]:
    msgs = [{"role": "user", "content": f"ερώτηση {i}"}]
    if tool_chars:
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "get_lesson", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "α" * tool_chars})
    msgs.append({"role": "assistant", "content": f"απάντηση {i}"})
    return msgs


def test_window_drops_old_turns_when_chars_exceed_budget():
    wire = [{"role": "system", "content": "sys"}]
    for i in range(5):
        wire += _char_turn(i, tool_chars=100_000)
    out = window_wire(wire, max_chars=250_000)
    assert out[0]["role"] == "system"
    assert out[1]["role"] == "user"                      # clean left edge
    assert sum(len(m.get("content") or "") for m in out) <= 250_000 + len("sys")
    assert out[-1]["content"] == "απάντηση 4"          # newest kept


def test_window_never_splits_a_tool_call_from_its_result():
    wire = [{"role": "system", "content": "sys"}] + _char_turn(0, tool_chars=10) + _char_turn(1, tool_chars=200_000)
    out = window_wire(wire, max_chars=150_000)
    ids_called = {c["id"] for m in out if m.get("tool_calls") for c in m["tool_calls"]}
    ids_answered = {m["tool_call_id"] for m in out if m.get("role") == "tool"}
    assert ids_called == ids_answered


def test_window_keeps_the_last_user_turn_even_if_alone_over_budget():
    wire = [{"role": "user", "content": "α" * 300_000}]
    assert window_wire(wire, max_chars=10) == wire


def test_default_budget_is_240k():
    assert MAX_WIRE_CHARS == 240_000


def test_window_wire_returns_the_same_object_when_nothing_is_trimmed():
    """Under both caps, `window_wire` must hand back the exact `wire` object
    (not an equal reconstruction) — pinned next to the new char-budget
    behaviour since it now shares the same return path."""
    wire = [{"role": "system", "content": "sys"}] + _char_turn(0)
    assert window_wire(wire) is wire
