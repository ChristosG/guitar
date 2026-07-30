"""The CORE_DECISIONS.md §3 fix: a streamed turn that turns out to want a
TOOL (or trips the C3 tablature guard) used to DISCARD its fully-billed
first model response and make the browser re-send the identical turn through
REST — which re-ran the identical first call. Tool-calling turns were billed
~2x, a leftover from the free-vLLM era.

The fix under test (option (b) of the doc's sketch — "the loop needs to
accept a pre-computed first response"):
  - `app.agent.handoff.FirstTurnHandoff` — a single-use, TTL'd, per-session
    stash of the streamed first `AssistantTurn`.
  - `stream_plain_turn`'s "tool_call"/"tablature" fallback events now carry
    that turn (server-internal only — the SSE wire payload is unchanged).
  - `POST .../messages/stream` stashes it; `POST .../messages` claims it and
    hands it to `run_agent_turn(precomputed_first=...)`, whose FIRST loop
    iteration consumes it in place of a `provider.chat_tools` call.

Three layers, mirroring the repo's own conventions:
  - pure unit tests for the store (no DB, no provider);
  - pure unit tests for `run_agent_turn(precomputed_first=...)` (scripted
    fake provider, `db=None`, same shape as `test_agent_loop.py`);
  - DB-backed router round-trips through BOTH endpoints with a spy provider
    counting every model call (same shape as `test_chat_stream_router.py`),
    proving the whole streamed-fallback→REST turn bills exactly ONE first
    call — including that HITL approval still works after the handoff.
"""
import uuid

import pytest

import app.agent.handoff as handoff_mod
import app.agent.loop as agent_loop
from app.agent.handoff import FIRST_TURN_HANDOFF, FirstTurnHandoff
from app.agent.loop import run_agent_turn
from app.agent.tools import TOOLS, ToolEntry
from app.llm.tools_types import AssistantTurn, ToolCall


def _turn(content="ok", tool_calls=()):
    return AssistantTurn(content=content, tool_calls=list(tool_calls))


# ---------------------------------------------------------------------------
# The handoff store itself (no DB, no provider)
# ---------------------------------------------------------------------------

def test_handoff_claim_returns_the_stashed_turn_exactly_once():
    store = FirstTurnHandoff()
    sid = uuid.uuid4()
    stashed = _turn("θα καλέσω εργαλείο")
    store.stash(sid, "χώρισε το μάθημα", stashed)

    assert store.claim(sid, "χώρισε το μάθημα") is stashed
    # SINGLE-USE: the same retry arriving twice (double-click, duplicate
    # request) must never replay the same response twice — the loser pays
    # for a fresh call instead.
    assert store.claim(sid, "χώρισε το μάθημα") is None


def test_handoff_content_mismatch_returns_none_AND_consumes_the_slot():
    store = FirstTurnHandoff()
    sid = uuid.uuid4()
    store.stash(sid, "original message", _turn())

    # A DIFFERENT message on the session: no replay...
    assert store.claim(sid, "a different message") is None
    # ...and the stale slot is gone too — it must not survive to a LATER
    # retry of the original content against a transcript that moved on.
    assert store.claim(sid, "original message") is None


def test_handoff_never_leaks_across_sessions():
    store = FirstTurnHandoff()
    sid_a, sid_b = uuid.uuid4(), uuid.uuid4()
    turn_a = _turn("for session A")
    store.stash(sid_a, "same content", turn_a)

    assert store.claim(sid_b, "same content") is None
    # Session B's miss must not have consumed session A's slot.
    assert store.claim(sid_a, "same content") is turn_a


def test_handoff_expires_after_ttl_and_survives_within_it(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(handoff_mod.time, "monotonic", lambda: now[0])
    sid = uuid.uuid4()

    store = FirstTurnHandoff(ttl_seconds=300.0)
    stashed = _turn()
    store.stash(sid, "hi", stashed)
    now[0] += 299.0
    assert store.claim(sid, "hi") is stashed  # within TTL: replayable

    store.stash(sid, "hi", stashed)
    now[0] += 301.0
    assert store.claim(sid, "hi") is None  # past TTL: pruned, fresh call


def test_handoff_prunes_expired_slots_even_if_the_client_never_returns(monkeypatch):
    """TTL CLEANUP: a tab closed right after the fallback leaves a slot
    behind; any later store activity (here: another session's stash) prunes
    it, so abandonment never accumulates memory."""
    now = [1000.0]
    monkeypatch.setattr(handoff_mod.time, "monotonic", lambda: now[0])
    store = FirstTurnHandoff(ttl_seconds=300.0)
    abandoned = uuid.uuid4()
    store.stash(abandoned, "never resent", _turn())

    now[0] += 301.0
    store.stash(uuid.uuid4(), "unrelated", _turn())
    assert str(abandoned) not in store._slots


def test_handoff_discard_drops_the_slot():
    store = FirstTurnHandoff()
    sid = uuid.uuid4()
    store.stash(sid, "hi", _turn())
    store.discard(sid)
    assert store.claim(sid, "hi") is None
    store.discard(sid)  # idempotent on an empty slot


# ---------------------------------------------------------------------------
# `run_agent_turn(precomputed_first=...)` — the loop consumes the paid-for
# first response and bills only what comes AFTER it. Same conventions as
# `test_agent_loop.py`: scripted fake provider, stubbed tools, db=None.
# ---------------------------------------------------------------------------

class _FakeProvider:
    """Mirrors `test_agent_loop.py`'s `_FakeProvider` (see that module's own
    docstring for the duplication rationale): `chat_tools` returns the next
    scripted turn, repeating the last once exhausted, and snapshots every
    call's `messages` so a test can assert what a given call actually saw.
    """

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls: list[list[dict]] = []

    def chat_tools(self, messages, tools, *, tool_choice="auto", temperature=0.3):
        self.calls.append([dict(m) for m in messages])
        assert self._turns, "the provider was called but this test scripted no turns"
        return self._turns[min(len(self.calls) - 1, len(self._turns) - 1)]


def _stub_tool(monkeypatch, name: str, fn):
    """Mirrors `test_agent_loop.py`'s `_stub_tool`: `ToolEntry` is frozen, so
    swap the whole dict entry; `monkeypatch` restores it after the test."""
    original = TOOLS[name]
    monkeypatch.setitem(
        TOOLS, name,
        ToolEntry(schema=original.schema, fn=fn, kind=original.kind, async_job=original.async_job),
    )


def _wire(user_text: str) -> list[dict]:
    return [{"role": "user", "content": user_text}]


def test_precomputed_plain_answer_makes_zero_provider_calls(monkeypatch):
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
    provider = _FakeProvider([])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: provider)

    result = run_agent_turn(
        None, _wire("τι είναι το vibrato;"),
        precomputed_first=_turn("Το vibrato είναι ταλάντωση του τόνου."),
    )

    assert result.status == "answer"
    assert result.content == "Το vibrato είναι ταλάντωση του τόνου."
    assert provider.calls == []  # the whole turn: zero fresh model calls
    assert result.messages[-1] == {
        "role": "assistant", "content": "Το vibrato είναι ταλάντωση του τόνου.",
    }


def test_precomputed_mutation_turn_suspends_with_zero_provider_calls(monkeypatch):
    """THE HEADLINE CASE at loop level: the streamed first call proposed a
    mutation; the REST re-run must reach `awaiting_approval` without paying
    for a single model call — the streamed call was the turn's only bill."""
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
    provider = _FakeProvider([])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: provider)

    result = run_agent_turn(
        None, _wire("χώρισε το μάθημα 2 σε δύο μέρη"),
        precomputed_first=_turn(
            "Θα το χωρίσω.",
            [ToolCall(id="call_1", name="update_block", arguments={"block_id": "b1", "title": "Μέρος 1"})],
        ),
    )

    assert result.status == "awaiting_approval"
    assert result.pending_tool["name"] == "update_block"
    assert result.pending_tool["tool_call_id"] == "call_1"
    assert provider.calls == []
    # Protocol integrity is unchanged: the suspended call is IN the wire
    # transcript (unanswered), exactly as a fresh first call would leave it.
    assert result.messages[-1]["role"] == "assistant"
    assert result.messages[-1]["tool_calls"][0]["id"] == "call_1"


def test_precomputed_read_turn_dispatches_the_read_and_bills_only_the_continuation(monkeypatch):
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
    _stub_tool(
        monkeypatch, "search_knowledge",
        lambda db, **kw: [{"source": "Pickups 101", "text": "a humbucker cancels hum"}],
    )
    provider = _FakeProvider([_turn("Ο humbucker ακυρώνει το βουητό.")])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: provider)

    result = run_agent_turn(
        None, _wire("τι ακυρώνει το βουητό;"),
        precomputed_first=_turn(
            None,
            [ToolCall(id="call_r", name="search_knowledge", arguments={"query": "hum"})],
        ),
    )

    assert result.status == "answer"
    assert result.content == "Ο humbucker ακυρώνει το βουητό."
    # Exactly ONE provider call — the continuation AFTER the read dispatch,
    # never a re-run of the already-billed first call.
    assert len(provider.calls) == 1
    seen = provider.calls[0]
    # ...and that one call already saw the precomputed assistant tool-call
    # turn AND its dispatched tool result in history — proof the loop resumed
    # mid-turn instead of starting over.
    assert any(m["role"] == "assistant" and m.get("tool_calls") for m in seen)
    tool_results = [m for m in seen if m["role"] == "tool"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_call_id"] == "call_r"
    assert "humbucker" in tool_results[0]["content"]


def test_precomputed_tablature_bluff_is_still_suppressed_and_reprompted_once(monkeypatch):
    """C3 keeps its teeth through the handoff: a stashed bluff is treated
    exactly like a fresh model bluff — never surfaced, re-prompted once —
    and the recovery call is the turn's SECOND bill instead of its third."""
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
    bluff = "Ορίστε:\n\ne|-----0-2-4-5-7-8-10-\nB|-----0-2-4-5-7-8-10-\n"
    provider = _FakeProvider([_turn("Θα το φτιάξω σωστά ως artifact.")])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: provider)

    result = run_agent_turn(
        None, _wire("δώσε μου τη μείζονα κλίμακα Σολ"),
        precomputed_first=_turn(bluff),
    )

    assert result.status == "answer"
    assert result.content == "Θα το φτιάξω σωστά ως artifact."
    assert len(provider.calls) == 1  # only the bounded C3 recovery call
    # The bluff never reached the transcript; the corrective re-prompt did.
    assert all(bluff not in (m.get("content") or "") for m in result.messages)
    assert any(
        m["role"] == "user" and m["content"] == agent_loop._TAB_BLUFF_REPROMPT_MESSAGE
        for m in provider.calls[0]
    )


def test_precomputed_first_is_consumed_only_by_the_first_iteration(monkeypatch):
    """A precomputed READ turn followed by the model calling ANOTHER read on
    its next hop: the second hop must come from the provider, not from any
    lingering precomputed state — i.e. consumption is strictly first-iteration."""
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])
    _stub_tool(monkeypatch, "search_knowledge", lambda db, **kw: [{"text": "hit"}])
    provider = _FakeProvider([
        _turn(None, [ToolCall(id="call_2", name="search_knowledge", arguments={"query": "deeper"})]),
        _turn("Τελική απάντηση."),
    ])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: provider)

    result = run_agent_turn(
        None, _wire("πες μου για τα μαγνητάκια"),
        precomputed_first=_turn(None, [ToolCall(id="call_1", name="search_knowledge", arguments={"query": "pickups"})]),
    )

    assert result.status == "answer"
    assert result.content == "Τελική απάντηση."
    assert len(provider.calls) == 2  # hop 2 and hop 3 — hop 1 was pre-paid
    answered_ids = [m["tool_call_id"] for m in result.messages if m.get("role") == "tool"]
    assert answered_ids == ["call_1", "call_2"]  # both hops answered, in order


# ---------------------------------------------------------------------------
# End-to-end through both endpoints (DB-backed, spy provider) — the actual
# billing proof, plus HITL-still-works and the client contract staying put.
# Same setup/skip conventions as `test_chat_stream_router.py`.
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import Base, engine  # noqa: E402

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
    _DB_UP = True
except Exception:
    _DB_UP = False

if _DB_UP:
    Base.metadata.create_all(engine)
    from app.main import app
    from app.models.chat import Message

    client = TestClient(app)

needs_db = pytest.mark.skipif(
    not _DB_UP, reason="database not reachable — set DATABASE_URL to a running Postgres"
)


class _SpyProvider:
    """Implements BOTH transports with per-method counters — the billing
    ledger these tests assert on. `chat_tools_stream` replays scripted
    events; `chat_tools` returns scripted turns (repeating the last) and
    snapshots each call's messages."""

    def __init__(self, stream_events, rest_turns=()):
        self._stream_events = list(stream_events)
        self._rest_turns = list(rest_turns)
        self.stream_calls = 0
        self.chat_tools_seen: list[list[dict]] = []

    @property
    def chat_tools_calls(self) -> int:
        return len(self.chat_tools_seen)

    def chat_tools_stream(self, messages, tools, *, tool_choice="auto", temperature=0.3):
        self.stream_calls += 1
        for event in self._stream_events:
            yield event

    def chat_tools(self, messages, tools, *, tool_choice="auto", temperature=0.3):
        self.chat_tools_seen.append([dict(m) for m in messages])
        assert self._rest_turns, "chat_tools was called but this test scripted no REST turns"
        return self._rest_turns[min(self.chat_tools_calls - 1, len(self._rest_turns) - 1)]


def _use_provider(monkeypatch, provider):
    monkeypatch.setattr(agent_loop, "get_provider", lambda: provider)
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [])


def _create_session() -> str:
    r = client.post("/chat", json={})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _parse_sse(body: str) -> list[tuple[str, str]]:
    """Mirrors `test_chat_stream_router.py`'s helper (file-private there)."""
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


@needs_db
def test_streamed_mutation_turn_bills_one_first_call_and_hitl_still_works(monkeypatch):
    """THE §3 SCENARIO, end to end: stream → tool_call fallback → REST resend
    → approval card, with the model's first call billed EXACTLY ONCE — and
    the HITL approve/resume afterwards behaving exactly as before the fix."""
    fn_ran = []
    _stub_tool(monkeypatch, "update_block", lambda db, **kw: fn_ran.append(kw) or {"id": str(uuid.uuid4())})
    provider = _SpyProvider(
        stream_events=[
            {"type": "content", "text": "Θα το χωρίσω."},
            {
                "type": "done", "content": "Θα το χωρίσω.",
                "tool_calls": [ToolCall(id="call_1", name="update_block",
                                        arguments={"block_id": "b1", "title": "Μέρος 1"})],
            },
        ],
        rest_turns=[AssistantTurn(content="Έγινε η αλλαγή.", tool_calls=[])],
    )
    _use_provider(monkeypatch, provider)
    session_id = _create_session()
    content = "χώρισε το μάθημα 2 σε δύο μέρη"

    # 1) The stream leg: full first model call, then fallback.
    r = client.post(f"/chat/{session_id}/messages/stream", json={"content": content})
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    assert events[-1][0] == "fallback"
    # CLIENT CONTRACT UNCHANGED: the SSE payload is exactly the old shape —
    # the billed turn rides server-side only, never onto the wire.
    assert events[-1][1] == '{"reason": "tool_call"}'
    assert provider.stream_calls == 1
    assert provider.chat_tools_calls == 0

    # 2) The REST resend (what the browser does on "fallback"): must consume
    # the stashed first response — ZERO fresh model calls to reach the card.
    r2 = client.post(f"/chat/{session_id}/messages", json={"content": content})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "awaiting_approval"
    assert body["tool_name"] == "update_block"
    assert provider.chat_tools_calls == 0, (
        "the REST resend re-billed the first model call — the §3 double-bill is back"
    )
    assert fn_ran == []  # HITL gate intact: nothing executed pre-approval

    # 3) Approve: the mutation runs and the loop RESUMES with a genuinely new
    # model call (hop 2 — correctly billed, it never ran before).
    r3 = client.post(
        f"/chat/{session_id}/approvals/{body['approval_id']}/resolve",
        json={"decision": "approve"},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "answer"
    assert r3.json()["content"] == "Έγινε η αλλαγή."
    assert len(fn_ran) == 1
    assert provider.chat_tools_calls == 1  # the resume hop only

    # Whole turn, tool included: 1 streamed call + 1 resume call. Before the
    # fix this was 2 identical first calls + the resume.
    history = client.get(f"/chat/{session_id}")
    roles = [m["role"] for m in history.json()]
    assert roles.count("user") == 1  # the fallback resend didn't duplicate the turn


@needs_db
def test_streamed_read_tool_turn_rest_followup_consumes_the_precomputed_turn(monkeypatch):
    _stub_tool(
        monkeypatch, "search_knowledge",
        lambda db, **kw: [{"source": "Pickups 101", "text": "a humbucker cancels hum"}],
    )
    provider = _SpyProvider(
        stream_events=[
            {
                "type": "done", "content": None,
                "tool_calls": [ToolCall(id="call_r", name="search_knowledge",
                                        arguments={"query": "hum"})],
            },
        ],
        rest_turns=[AssistantTurn(content="Ο humbucker ακυρώνει το βουητό.", tool_calls=[])],
    )
    _use_provider(monkeypatch, provider)
    session_id = _create_session()
    content = "τι ακυρώνει το βουητό;"

    r = client.post(f"/chat/{session_id}/messages/stream", json={"content": content})
    assert _parse_sse(r.text)[-1][0] == "fallback"
    assert provider.chat_tools_calls == 0

    r2 = client.post(f"/chat/{session_id}/messages", json={"content": content})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "answer"
    assert r2.json()["content"] == "Ο humbucker ακυρώνει το βουητό."
    # ONE billed call for the whole REST leg — the continuation after the
    # read dispatch — and it already saw the precomputed call's tool result.
    assert provider.chat_tools_calls == 1
    seen = provider.chat_tools_seen[0]
    assert any(m["role"] == "tool" and m.get("tool_call_id") == "call_r" for m in seen)

    # SINGLE-USE: sending the same content AGAIN is a brand-new turn — the
    # stash is spent, so the model is called fresh (no stale replay).
    r3 = client.post(f"/chat/{session_id}/messages", json={"content": content})
    assert r3.status_code == 200, r3.text
    assert provider.chat_tools_calls == 2


@needs_db
def test_rest_with_different_content_never_replays_the_stash(monkeypatch):
    """MISMATCH INVALIDATION: the stream leg stashed a MUTATION turn for
    content A; the tutor instead sends content B. A replay would surface as
    `awaiting_approval` with zero model calls — the correct outcome is a
    fresh first call answering B, and A's stash consumed (gone for good)."""
    provider = _SpyProvider(
        stream_events=[
            {
                "type": "done", "content": "Θα το χωρίσω.",
                "tool_calls": [ToolCall(id="call_1", name="update_block",
                                        arguments={"block_id": "b1", "title": "X"})],
            },
        ],
        rest_turns=[AssistantTurn(content="Απλή απάντηση.", tool_calls=[])],
    )
    _use_provider(monkeypatch, provider)
    session_id = _create_session()

    r = client.post(f"/chat/{session_id}/messages/stream", json={"content": "χώρισε το μάθημα"})
    assert _parse_sse(r.text)[-1][0] == "fallback"

    r2 = client.post(f"/chat/{session_id}/messages", json={"content": "κάτι εντελώς άλλο;"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "answer", "a stale streamed turn was replayed for different content"
    assert provider.chat_tools_calls == 1

    # The mismatched claim CONSUMED the stash: retrying the ORIGINAL content
    # later is also a fresh call (answer), never a replay of the old proposal.
    r3 = client.post(f"/chat/{session_id}/messages", json={"content": "χώρισε το μάθημα"})
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "answer"
    assert provider.chat_tools_calls == 2


@needs_db
def test_streamed_tablature_fallback_rest_followup_bills_only_the_recovery_call(monkeypatch):
    bluff = "Ορίστε:\n\ne|-----0-2-4-5-7-8-10-\nB|-----0-2-4-5-7-8-10-\n"
    provider = _SpyProvider(
        stream_events=[
            {"type": "content", "text": bluff},
            {"type": "done", "content": bluff, "tool_calls": []},
        ],
        rest_turns=[AssistantTurn(content="Θα το φτιάξω σωστά ως artifact.", tool_calls=[])],
    )
    _use_provider(monkeypatch, provider)
    session_id = _create_session()
    content = "δώσε μου τη μείζονα κλίμακα Σολ"

    r = client.post(f"/chat/{session_id}/messages/stream", json={"content": content})
    events = _parse_sse(r.text)
    assert events[-1][0] == "fallback"
    assert events[-1][1] == '{"reason": "tablature"}'

    r2 = client.post(f"/chat/{session_id}/messages", json={"content": content})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "answer"
    assert r2.json()["content"] == "Θα το φτιάξω σωστά ως artifact."
    # The bluff was the (billed, stashed) first call; REST paid only for the
    # bounded C3 recovery — and the bluff never reached the transcript.
    assert provider.chat_tools_calls == 1
    history = client.get(f"/chat/{session_id}")
    assert all(bluff not in (m["content"] or "") for m in history.json())


@needs_db
def test_plain_streamed_turn_is_unaffected_and_stashes_nothing(monkeypatch):
    provider = _SpyProvider(
        stream_events=[
            {"type": "content", "text": "Ο humbucker ακυρώνει το βουητό."},
            {"type": "done", "content": "Ο humbucker ακυρώνει το βουητό.", "tool_calls": []},
        ],
    )
    _use_provider(monkeypatch, provider)
    session_id = _create_session()
    content = "τι ακυρώνει το βουητό;"

    r = client.post(f"/chat/{session_id}/messages/stream", json={"content": content})
    assert r.status_code == 200, r.text
    assert _parse_sse(r.text)[-1][0] == "done"
    assert provider.stream_calls == 1
    assert provider.chat_tools_calls == 0

    # Nothing stashed for a turn that finished over the stream — a later REST
    # message on this session has nothing to (wrongly) consume.
    assert FIRST_TURN_HANDOFF.claim(session_id, content) is None

    history = client.get(f"/chat/{session_id}")
    contents = [(m["role"], m["content"]) for m in history.json()]
    assert ("user", content) in contents
    assert ("assistant", "Ο humbucker ακυρώνει το βουητό.") in contents


@needs_db
def test_the_409_pending_approval_guard_discards_a_stale_stash(monkeypatch):
    """If an approval opened between the stream fallback and the REST resend
    (another tab resolved a turn), the resend 409s as before — AND the stash
    dies with it, so resolving the approval and retrying later can never
    replay a first response computed against the pre-approval transcript."""
    _stub_tool(monkeypatch, "update_block", lambda db, **kw: {"id": str(uuid.uuid4())})
    provider = _SpyProvider(
        stream_events=[
            {
                "type": "done", "content": "Θα το αλλάξω.",
                "tool_calls": [ToolCall(id="call_1", name="update_block",
                                        arguments={"block_id": "b1", "title": "X"})],
            },
        ],
        rest_turns=[AssistantTurn(content="Απάντηση.", tool_calls=[])],
    )
    _use_provider(monkeypatch, provider)
    session_id = _create_session()
    content = "άλλαξε τον τίτλο του μαθήματος"

    # Stream falls back and stashes...
    r = client.post(f"/chat/{session_id}/messages/stream", json={"content": content})
    assert _parse_sse(r.text)[-1][0] == "fallback"
    # ...but before the resend, a REST turn (e.g. from another tab) opens an
    # approval: claim its own path first so the session has a pending card.
    r_other = client.post(f"/chat/{session_id}/messages", json={"content": content})
    assert r_other.json()["status"] == "awaiting_approval"

    # A new stream fallback now can't even start (409 guard, pre-existing)…
    r_stream = client.post(f"/chat/{session_id}/messages/stream", json={"content": content})
    assert r_stream.status_code == 409

    # …and a REST resend 409s AND clears whatever was stashed.
    FIRST_TURN_HANDOFF.stash(session_id, content, AssistantTurn(content=None, tool_calls=[]))
    r_rest = client.post(f"/chat/{session_id}/messages", json={"content": content})
    assert r_rest.status_code == 409
    assert FIRST_TURN_HANDOFF.claim(session_id, content) is None
