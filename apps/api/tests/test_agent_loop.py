"""Unit tests for `app.agent.loop.run_agent_turn` — the ReAct tool-calling
loop (Plan 5 Task 2). Pure unit tests: NO live LLM (the provider is a
scripted fake, per this task's brief) and NO DB (every dispatched tool in
these tests is a stubbed registry entry that ignores `db` entirely — `db` is
passed through as `None`, which itself proves the loop never dereferences it
directly). Real tool-function behavior (the actual DB queries the registry
wraps) is covered separately in `test_agent_tools.py`; `chat_tools`'s own
response parsing is covered in `test_qwen_chat_tools.py`. This file is only
the loop's own orchestration: dispatch, OpenAI-wire-shape history
reconstruction, the unknown-tool guard, a tool-dispatch-exception guard, the
`ToolArgsError` bounded repair, and the `max_steps` cap.
"""
import json

import app.agent.loop as agent_loop
from app.agent.loop import AgentResult, run_agent_turn
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import TOOLS, ToolEntry
from app.llm.errors import ToolArgsError
from app.llm.tools_types import AssistantTurn, ToolCall


class _FakeProvider:
    """Stand-in for `LLMProvider`: `chat_tools` returns the next scripted
    entry from `turns` each call (an `AssistantTurn`, or an `Exception`
    instance to raise instead of returning). Once `turns` is exhausted, it
    keeps returning/raising the LAST entry — so a test can simulate "the
    model keeps calling a tool forever" (the max_steps test) or "the model
    never recovers" (the ToolArgsError cap test) without scripting one entry
    per step. Records a snapshot of every call's `messages` so a test can
    assert on the transcript/repair-prompt content a given call actually saw.
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


def _stub_tool(monkeypatch, name: str, fn):
    """Replace `TOOLS[name].fn` for one test. `ToolEntry` is frozen (a parsed
    registry entry is a value, same rationale as `ToolCall`), so this swaps
    the whole dict entry via `monkeypatch.setitem` rather than mutating an
    attribute — `monkeypatch` restores the original entry after the test.
    """
    original = TOOLS[name]
    monkeypatch.setitem(TOOLS, name, ToolEntry(schema=original.schema, fn=fn, kind=original.kind))


# ---------------------------------------------------------------------------
# (a) read-tool dispatch -> final answer; wire-shape history reconstruction
# ---------------------------------------------------------------------------

def test_run_agent_turn_dispatches_read_tool_then_returns_final_answer(monkeypatch):
    search_calls = []

    def _fake_search_knowledge(db, *, query, **kwargs):
        assert db is None  # the loop must never dereference db itself
        search_calls.append(query)
        return [{"source": "Pickups 101", "text": "a humbucker cancels hum", "score": 0.9}]

    _stub_tool(monkeypatch, "search_knowledge", _fake_search_knowledge)

    call = ToolCall(id="call_1", name="search_knowledge", arguments={"query": "what cancels hum?"})
    turn1 = AssistantTurn(content=None, tool_calls=[call])
    turn2 = AssistantTurn(content="A humbucker cancels hum.", tool_calls=[])
    fake_provider = _FakeProvider([turn1, turn2])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    messages = [{"role": "user", "content": "what cancels hum?"}]
    result = run_agent_turn(None, messages)

    assert isinstance(result, AgentResult)
    assert result.status == "answer"
    assert result.content == "A humbucker cancels hum."
    assert search_calls == ["what cancels hum?"]
    assert len(fake_provider.calls) == 2  # one round-trip: call -> tool -> call

    # SYSTEM_PROMPT was prepended (the caller's messages didn't have one).
    assert result.messages[0] == {"role": "system", "content": SYSTEM_PROMPT}

    # The assistant turn WITH tool_calls is in history in OpenAI WIRE shape —
    # NOT the parsed AssistantTurn shape chat_tools itself returns.
    assistant_tool_msgs = [m for m in result.messages if m["role"] == "assistant" and m.get("tool_calls")]
    assert len(assistant_tool_msgs) == 1
    wire_call = assistant_tool_msgs[0]["tool_calls"][0]
    assert wire_call == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "search_knowledge", "arguments": json.dumps({"query": "what cancels hum?"})},
    }

    # The final plain-answer assistant turn has NO tool_calls key at all
    # (omitted entirely, not an empty list) — mirrors turn2's empty tool_calls.
    final_msgs = [m for m in result.messages if m["role"] == "assistant" and m.get("content") == "A humbucker cancels hum."]
    assert len(final_msgs) == 1
    assert "tool_calls" not in final_msgs[0]

    # The tool result is paired back to the call via tool_call_id.
    tool_msgs = [m for m in result.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert "humbucker" in tool_msgs[0]["content"]


def test_run_agent_turn_does_not_duplicate_an_existing_system_prompt(monkeypatch):
    fake_provider = _FakeProvider([AssistantTurn(content="hi there", tool_calls=[])])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "hi"}]
    result = run_agent_turn(None, messages)

    system_msgs = [m for m in result.messages if m["role"] == "system"]
    assert len(system_msgs) == 1


# ---------------------------------------------------------------------------
# (b) guards: unknown tool name, and a tool that raises during dispatch
# ---------------------------------------------------------------------------

def test_run_agent_turn_guards_unknown_tool_name_and_continues(monkeypatch):
    bad_call = ToolCall(id="call_1", name="delete_everything", arguments={})
    turn1 = AssistantTurn(content=None, tool_calls=[bad_call])
    turn2 = AssistantTurn(content="done", tool_calls=[])
    fake_provider = _FakeProvider([turn1, turn2])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(None, [{"role": "user", "content": "hi"}])

    assert result.status == "answer"
    assert result.content == "done"
    tool_msgs = [m for m in result.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert "unknown tool" in tool_msgs[0]["content"].lower()
    assert "delete_everything" in tool_msgs[0]["content"]


def test_run_agent_turn_guards_a_tool_dispatch_exception_and_continues(monkeypatch):
    """Beyond the brief's two explicit guards (unknown name, ToolArgsError):
    a KNOWN tool can still raise mid-dispatch (e.g. the model supplies an
    argument shape the fn doesn't accept — valid JSON, wrong keys, so
    `chat_tools` never sees a parse error and no `ToolArgsError` is raised).
    Same "guard, don't crash" spirit as the two guards the brief names
    explicitly — the loop must not let that propagate and kill the whole turn.
    """
    def _boom(db, **kwargs):
        raise TypeError("unexpected keyword argument 'bogus'")

    _stub_tool(monkeypatch, "search_knowledge", _boom)

    bad_call = ToolCall(id="call_1", name="search_knowledge", arguments={"bogus": 1})
    turn1 = AssistantTurn(content=None, tool_calls=[bad_call])
    turn2 = AssistantTurn(content="recovered", tool_calls=[])
    fake_provider = _FakeProvider([turn1, turn2])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(None, [{"role": "user", "content": "hi"}])

    assert result.status == "answer"
    assert result.content == "recovered"
    tool_msgs = [m for m in result.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert "search_knowledge" in tool_msgs[0]["content"]
    assert "failed" in tool_msgs[0]["content"].lower()


# ---------------------------------------------------------------------------
# (c) max_steps cap respected
# ---------------------------------------------------------------------------

def test_run_agent_turn_stops_gracefully_at_max_steps(monkeypatch):
    def _fake_search_knowledge(db, *, query, **kwargs):
        return [{"source": "x", "text": "y", "score": 0.5}]

    _stub_tool(monkeypatch, "search_knowledge", _fake_search_knowledge)

    call = ToolCall(id="call_1", name="search_knowledge", arguments={"query": "q"})
    # The model NEVER stops calling a tool — proves the cap is the loop's own
    # max_steps, not luck / an eventual plain-content response.
    always_calls_tool = AssistantTurn(content=None, tool_calls=[call])
    fake_provider = _FakeProvider([always_calls_tool])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(None, [{"role": "user", "content": "hi"}], max_steps=3)

    assert result.status == "answer"
    assert result.content  # some graceful, non-empty note — never None/a crash
    assert len(fake_provider.calls) == 3  # exactly max_steps, not more


# ---------------------------------------------------------------------------
# (d) ToolArgsError -> bounded repair -> graceful giveup, no dangling tool_call
# ---------------------------------------------------------------------------

def test_run_agent_turn_gives_up_gracefully_after_repeated_tool_args_errors(monkeypatch):
    always_malformed = ToolArgsError(tool_name="search_knowledge", raw="{bad json")
    fake_provider = _FakeProvider([always_malformed])  # every call raises
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(None, [{"role": "user", "content": "hi"}])

    assert result.status == "answer"
    assert result.content  # a graceful message, not None/a crash
    # Bounded: the loop gave up well short of exhausting the default max_steps=6.
    assert len(fake_provider.calls) == 2

    # No dangling tool_call: every attempt raised BEFORE chat_tools returned
    # anything, so no assistant message with tool_calls was ever appended
    # (the brief's "NOT adding the broken turn to history").
    assert not any(m["role"] == "assistant" and m.get("tool_calls") for m in result.messages)
    # Exactly one corrective re-prompt was sent (bounded, not one per failure).
    corrective_msgs = [
        m for m in result.messages
        if m["role"] == "user" and "invalid JSON arguments" in m.get("content", "")
    ]
    assert len(corrective_msgs) == 1


def test_run_agent_turn_repair_counter_resets_after_a_successful_call(monkeypatch):
    """"Consecutive" (per the brief) means a successful call in between two
    failures resets the streak — this is NOT just a lifetime total-failures
    counter. Scripts: fail, succeed (tool call + dispatch), fail, fail — the
    2nd/3rd failures are a fresh streak, so the loop must give up on THEM
    (not treat this as "3 failures total, already over some lifetime cap").
    """
    def _fake_search_knowledge(db, *, query, **kwargs):
        return [{"source": "x", "text": "y", "score": 0.5}]

    _stub_tool(monkeypatch, "search_knowledge", _fake_search_knowledge)

    call = ToolCall(id="call_1", name="search_knowledge", arguments={"query": "q"})
    turns = [
        ToolArgsError(tool_name="search_knowledge", raw="{bad"),          # failure #1 (streak=1)
        AssistantTurn(content=None, tool_calls=[call]),                    # success -> resets streak
        ToolArgsError(tool_name="search_knowledge", raw="{bad"),          # failure #1 of new streak
        ToolArgsError(tool_name="search_knowledge", raw="{bad"),          # failure #2 of new streak -> giveup
    ]
    fake_provider = _FakeProvider(turns)
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(None, [{"role": "user", "content": "hi"}], max_steps=10)

    assert result.status == "answer"
    assert len(fake_provider.calls) == 4  # not stopped after the first 2-failure count would suggest
