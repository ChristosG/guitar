"""Unit tests for the HITL suspend-on-mutation behavior (Plan 5 Task 3):
`app.agent.tools.TOOLS`'s new mutation entries, and `app.agent.loop.
run_agent_turn`'s change to SUSPEND (never execute) the first dispatched
MUTATION `ToolCall` in a turn, returning `AgentResult(status=
"awaiting_approval", pending_tool=...)`.

Pure unit tests, NO live LLM and NO DB (every dispatched tool in these tests
is a stubbed registry entry that ignores `db` entirely — `db` is passed
through as `None`) — same "scripted fake provider" pattern as
`test_agent_loop.py` (Task 2). That module's `_FakeProvider`/`_stub_tool`
helpers are file-private there (no `__all__`, `_`-prefixed), so they're
duplicated here rather than cross-imported — mirrors this codebase's own
established "small deliberate duplication over reaching into another
module's `_`-prefixed helper" precedent (e.g. `routers/artifacts.py`'s own
`_get_block_or_404` docstring).

PROTOCOL INTEGRITY is the crux of this file's later tests: the pending
mutation's tool_call must be the ONLY unanswered tool_call in the returned
`messages` transcript, because Task 4 answers exactly that one call at
resolve time and then resumes the loop — a second, still-dangling unanswered
call would desync that resume.
"""
import app.agent.loop as agent_loop
from app.agent.loop import AgentResult, run_agent_turn
from app.agent.tools import TOOLS, ToolEntry
from app.llm.tools_types import AssistantTurn, ToolCall


class _FakeProvider:
    """Mirrors `test_agent_loop.py`'s `_FakeProvider` exactly (see that
    module's own docstring for the full rationale): `chat_tools` returns the
    next scripted `turns` entry each call, repeating the last one once
    exhausted; records a snapshot of every call's `messages`.
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
    """Mirrors `test_agent_loop.py`'s `_stub_tool`: `ToolEntry` is frozen (a
    registered entry is a value), so this swaps the whole dict entry via
    `monkeypatch.setitem` — `monkeypatch` restores the original after the
    test. Carries `async_job` through too (Task 3's new field) so stubbing a
    tool never accidentally resets it to the dataclass default.
    """
    original = TOOLS[name]
    monkeypatch.setitem(
        TOOLS, name,
        ToolEntry(schema=original.schema, fn=fn, kind=original.kind, async_job=original.async_job),
    )


# ---------------------------------------------------------------------------
# Registry shape: the mutation tools registered so far
# ---------------------------------------------------------------------------

def test_registry_has_exactly_the_nine_mutation_tools_registered_so_far():
    """Task 3 (Plan 5) registered the first mutations; Plan 10 Task 3 wires in
    the four Lesson Authoring tools (`draft_lesson_from_selection`,
    `split_session`, `merge_sessions`, `add_session`); Unit D adds
    `apply_curriculum_revision` (the async, approval-gated apply of a revision
    plan) — same registry, same "kind" convention. (The desktop build REMOVED
    the student/note mutations — create_student, update_student,
    assign_curriculum, add_note, promote_note_to_knowledge, log_progress.)
    """
    mutation_names = {name for name, entry in TOOLS.items() if entry.kind == "mutation"}
    assert mutation_names == {
        "segment_block", "update_block",
        "generate_artifact", "generate_curriculum",
        "draft_lesson_from_selection", "split_session", "merge_sessions", "add_session",
        "apply_curriculum_revision",
    }
    for name in mutation_names:
        entry = TOOLS[name]
        assert entry.schema["type"] == "function"
        fn_schema = entry.schema["function"]
        assert fn_schema["name"] == name
        assert fn_schema["description"]  # non-empty — the model reads this
        assert fn_schema["parameters"]["type"] == "object"


def test_exactly_generate_curriculum_draft_lesson_and_apply_revision_are_marked_async_job():
    """`generate_curriculum` (Plan 5 Task 3), `draft_lesson_from_selection`
    (Plan 10 Task 3) and `apply_curriculum_revision` (Unit D) are the tools too
    slow / heavy for a synchronous resolve-time dispatch — apply runs its one
    transaction and chains a background draft fan-out, so `resolve_approval`
    enqueues a `curriculum_revise` job for it rather than calling the fn inline.
    Every other mutation defaults False.
    """
    async_job_names = {name for name, entry in TOOLS.items() if entry.async_job}
    assert async_job_names == {
        "generate_curriculum", "draft_lesson_from_selection", "apply_curriculum_revision",
    }


def test_tool_schemas_now_exposes_both_read_and_mutation_tools_to_the_model():
    """`_tool_schemas()` (fed to `chat_tools` as the model's tool list) must
    now include mutation tools too — Task 2 filtered to `kind == "read"`
    only; this task lifts that so the model can actually propose a mutation
    for the loop to suspend on.
    """
    schemas = agent_loop._tool_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert names == set(TOOLS.keys())
    # 8 read (5 Plan 5 T2 + find_lesson, Plan 11 T2/C5 + search_concepts, C8 +
    # propose_curriculum_revision, Unit D) + 9 mutation (4 Plan 5 T3 +
    # 4 Plan 10 T3 + apply_curriculum_revision, Unit D) — the desktop build
    # removed the 1 student read + 6 student/note mutations.
    assert len(schemas) == 17


# ---------------------------------------------------------------------------
# (a) a lone mutation call suspends the turn; the fn is never invoked
# ---------------------------------------------------------------------------

def test_mutation_tool_call_suspends_and_never_invokes_the_fn(monkeypatch):
    calls_made = []

    def _spy(db, **kwargs):
        calls_made.append(kwargs)
        raise AssertionError("mutation fn must never be called by run_agent_turn")

    _stub_tool(monkeypatch, "generate_artifact", _spy)

    call = ToolCall(
        id="call_1", name="generate_artifact",
        arguments={"kind": "chord_diagram", "prompt": "G major open chord"},
    )
    turn1 = AssistantTurn(content="Sure, I'll draft that chord diagram.", tool_calls=[call])
    fake_provider = _FakeProvider([turn1])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(None, [{"role": "user", "content": "make me a G chord diagram"}])

    assert isinstance(result, AgentResult)
    assert result.status == "awaiting_approval"
    assert calls_made == []  # never invoked — the spy would have raised otherwise
    assert result.content == "Sure, I'll draft that chord diagram."
    assert result.pending_tool == {
        "tool_call_id": "call_1",
        "name": "generate_artifact",
        # The session's locale is INJECTED into the proposed args at suspend
        # time (Plan 13, Stage 5.4) — the model never picks it, and the approval
        # card, the wire transcript and the eventual dispatch all see the same
        # dict. `el` is the app default `run_agent_turn` falls back to here.
        "arguments": {
            "kind": "chord_diagram", "prompt": "G major open chord", "locale": "el",
        },
    }
    assert len(fake_provider.calls) == 1  # suspended on the FIRST turn — no round-trip

    # The assistant's own turn (narration + the proposed call) is in history,
    # in OpenAI wire shape.
    assistant_msgs = [m for m in result.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "Sure, I'll draft that chord diagram."
    assert [tc["id"] for tc in assistant_msgs[0]["tool_calls"]] == ["call_1"]

    # ...but there is NO paired tool-result message: the whole point of
    # suspending is that this call is NOT answered yet (Task 4 answers it at
    # resolve time, then resumes the loop).
    assert not any(m["role"] == "tool" for m in result.messages)


def test_awaiting_approval_result_content_is_none_when_the_model_gave_no_narration(monkeypatch):
    """A tool-calls-only turn commonly has `content=None` (mirrors
    `AssistantTurn.content`'s own documented contract) — the suspend path
    must preserve that faithfully rather than substituting a placeholder.
    """
    def _spy(db, **kwargs):
        raise AssertionError("must never be called")

    _stub_tool(monkeypatch, "update_block", _spy)

    call = ToolCall(id="call_1", name="update_block", arguments={"block_id": "b1", "title": "New"})
    turn1 = AssistantTurn(content=None, tool_calls=[call])
    fake_provider = _FakeProvider([turn1])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(None, [{"role": "user", "content": "rename that lesson to New"}])

    assert result.status == "awaiting_approval"
    assert result.content is None


# ---------------------------------------------------------------------------
# (b) a read, THEN (next turn) a mutation — across two separate turns
# ---------------------------------------------------------------------------

def test_read_then_mutation_across_two_turns_read_executes_mutation_suspends(monkeypatch):
    search_calls = []

    def _fake_search_knowledge(db, *, query, **kwargs):
        assert db is None  # the loop must never dereference db itself
        search_calls.append(query)
        return [{"source": "x", "text": "a humbucker cancels hum", "score": 0.9}]

    mutation_calls = []

    def _spy_update_block(db, **kwargs):
        mutation_calls.append(kwargs)
        raise AssertionError("must never be called")

    _stub_tool(monkeypatch, "search_knowledge", _fake_search_knowledge)
    _stub_tool(monkeypatch, "update_block", _spy_update_block)

    read_call = ToolCall(id="call_1", name="search_knowledge", arguments={"query": "hum cancelling"})
    mutation_call = ToolCall(id="call_2", name="update_block", arguments={"block_id": "b1", "title": "New"})
    turn1 = AssistantTurn(content=None, tool_calls=[read_call])
    turn2 = AssistantTurn(content="I'll rename that lesson.", tool_calls=[mutation_call])
    fake_provider = _FakeProvider([turn1, turn2])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(None, [{"role": "user", "content": "look something up then rename a lesson"}])

    assert search_calls == ["hum cancelling"]  # the read genuinely executed
    assert mutation_calls == []  # the mutation did not
    assert result.status == "awaiting_approval"
    assert result.pending_tool["name"] == "update_block"
    assert result.pending_tool["tool_call_id"] == "call_2"
    assert len(fake_provider.calls) == 2  # one full read round-trip, then the suspending turn

    tool_msgs = [m for m in result.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"  # only the READ got answered
    assert "humbucker" in tool_msgs[0]["content"]


# ---------------------------------------------------------------------------
# (c) a read AND a mutation together, in the SAME turn
# ---------------------------------------------------------------------------

def test_read_and_mutation_in_the_same_turn_read_answered_mutation_suspends(monkeypatch):
    list_calls = []

    def _fake_list_curricula(db, **kwargs):
        list_calls.append("list_curricula")
        return [{"id": "c1", "title": "Rhythm"}]

    def _spy_generate_artifact(db, **kwargs):
        raise AssertionError("must never be called")

    _stub_tool(monkeypatch, "list_curricula", _fake_list_curricula)
    _stub_tool(monkeypatch, "generate_artifact", _spy_generate_artifact)

    read_call = ToolCall(id="call_a", name="list_curricula", arguments={})
    mutation_call = ToolCall(
        id="call_b", name="generate_artifact", arguments={"kind": "tab", "prompt": "riff"},
    )
    turn1 = AssistantTurn(content=None, tool_calls=[read_call, mutation_call])
    fake_provider = _FakeProvider([turn1])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(None, [{"role": "user", "content": "list curricula and make a tab"}])

    assert list_calls == ["list_curricula"]
    assert result.status == "awaiting_approval"
    assert result.pending_tool["tool_call_id"] == "call_b"
    assert result.pending_tool["name"] == "generate_artifact"
    assert len(fake_provider.calls) == 1  # suspended on the first turn

    # Exactly one tool result (the read) — the mutation is deliberately
    # UNANSWERED (that IS what "suspend" means).
    tool_msgs = [m for m in result.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_a"]

    # The assistant message's own tool_calls carries BOTH ids...
    assistant_msgs = [m for m in result.messages if m["role"] == "assistant" and m.get("tool_calls")]
    assert len(assistant_msgs) == 1
    assert [tc["id"] for tc in assistant_msgs[0]["tool_calls"]] == ["call_a", "call_b"]

    # ...and the ONE unanswered tool_call across the whole transcript is
    # exactly the pending mutation (protocol integrity: Task 4 must find
    # exactly one call left to answer at resolve time).
    all_call_ids = {
        tc["id"] for m in result.messages if m["role"] == "assistant" for tc in m.get("tool_calls", [])
    }
    answered_ids = {m["tool_call_id"] for m in result.messages if m["role"] == "tool"}
    assert all_call_ids - answered_ids == {"call_b"}


def test_a_call_after_the_pending_mutation_in_the_same_turn_is_dropped_entirely(monkeypatch):
    """3 calls in one turn: [read, mutation, read2]. The mutation suspends at
    index 1; `read2` (index 2, AFTER the pending mutation) must not be
    dispatched (it comes after the human hasn't yet gated the mutation) AND
    must not appear anywhere in the transcript either — not as a tool_call,
    not as a tool result. Leaving it in as an unanswered tool_call would be a
    SECOND dangling call (breaking protocol integrity); dispatching it out of
    turn would run a call the human never got a chance to gate.
    """
    def _fake_list_artifacts(db, **kwargs):
        return [{"id": "a1"}]

    def _must_not_run(db, **kwargs):
        raise AssertionError("must not be dispatched — it comes AFTER the pending mutation")

    def _spy_segment_block(db, **kwargs):
        raise AssertionError("must never be called")

    _stub_tool(monkeypatch, "list_artifacts", _fake_list_artifacts)
    _stub_tool(monkeypatch, "list_curricula", _must_not_run)
    _stub_tool(monkeypatch, "segment_block", _spy_segment_block)

    read_call = ToolCall(id="call_a", name="list_artifacts", arguments={})
    mutation_call = ToolCall(
        id="call_b", name="segment_block", arguments={"block_id": "x", "session_minutes": 30},
    )
    read2_call = ToolCall(id="call_c", name="list_curricula", arguments={})
    turn1 = AssistantTurn(content=None, tool_calls=[read_call, mutation_call, read2_call])
    fake_provider = _FakeProvider([turn1])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(None, [{"role": "user", "content": "do three things"}])

    assert result.status == "awaiting_approval"
    assert result.pending_tool["tool_call_id"] == "call_b"

    all_call_ids = [
        tc["id"] for m in result.messages if m["role"] == "assistant" for tc in m.get("tool_calls", [])
    ]
    assert all_call_ids == ["call_a", "call_b"]  # call_c never appears at all

    tool_msgs = [m for m in result.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_a"]


def test_two_mutations_in_the_same_turn_suspends_on_the_first_only(monkeypatch):
    def _spy_update_block(db, **kwargs):
        raise AssertionError("must never be called")

    def _spy_segment_block(db, **kwargs):
        raise AssertionError("must never be called")

    _stub_tool(monkeypatch, "update_block", _spy_update_block)
    _stub_tool(monkeypatch, "segment_block", _spy_segment_block)

    mutation1 = ToolCall(id="call_1", name="update_block", arguments={"block_id": "b1", "title": "A"})
    mutation2 = ToolCall(id="call_2", name="segment_block", arguments={"block_id": "b1", "session_minutes": 30})
    turn1 = AssistantTurn(content=None, tool_calls=[mutation1, mutation2])
    fake_provider = _FakeProvider([turn1])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(None, [{"role": "user", "content": "rename a lesson then segment it"}])

    assert result.status == "awaiting_approval"
    assert result.pending_tool["tool_call_id"] == "call_1"
    assert result.pending_tool["name"] == "update_block"

    all_call_ids = [
        tc["id"] for m in result.messages if m["role"] == "assistant" for tc in m.get("tool_calls", [])
    ]
    assert all_call_ids == ["call_1"]  # call_2 dropped entirely, same as any post-mutation call
    assert not any(m["role"] == "tool" for m in result.messages)


# ---------------------------------------------------------------------------
# (d) unknown tool BEFORE a mutation in the same turn is still guarded
# ---------------------------------------------------------------------------

def test_unknown_tool_before_a_mutation_is_guarded_then_the_mutation_suspends(monkeypatch):
    def _spy_generate_artifact(db, **kwargs):
        raise AssertionError("must never be called")

    _stub_tool(monkeypatch, "generate_artifact", _spy_generate_artifact)

    unknown_call = ToolCall(id="call_a", name="delete_everything", arguments={})
    mutation_call = ToolCall(id="call_b", name="generate_artifact", arguments={"kind": "tab", "prompt": "x"})
    turn1 = AssistantTurn(content=None, tool_calls=[unknown_call, mutation_call])
    fake_provider = _FakeProvider([turn1])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(None, [{"role": "user", "content": "hi"}])

    assert result.status == "awaiting_approval"
    assert result.pending_tool["tool_call_id"] == "call_b"

    tool_msgs = [m for m in result.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_a"
    assert "unknown tool" in tool_msgs[0]["content"].lower()


# ---------------------------------------------------------------------------
# (f) Plan 10 Task 3's four Lesson Authoring tools suspend the same way —
# same "one-test-each confirmation" role as (e) above: the suspend mechanism
# is generic over `kind == "mutation"`, so registering these new entries
# needed no loop.py change at all (B5 — every lesson tool is kind="mutation"
# and therefore HITL-gated by construction).
# ---------------------------------------------------------------------------

def test_draft_lesson_from_selection_call_suspends_and_never_invokes_the_fn(monkeypatch):
    def _spy(db, **kwargs):
        raise AssertionError("mutation fn must never be called by run_agent_turn")

    _stub_tool(monkeypatch, "draft_lesson_from_selection", _spy)

    call = ToolCall(
        id="call_1", name="draft_lesson_from_selection",
        arguments={"source_id": "src-1", "page_no": 21, "text": "Open position chords..."},
    )
    turn1 = AssistantTurn(content="I'll draft that lesson.", tool_calls=[call])
    fake_provider = _FakeProvider([turn1])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(
        None, [{"role": "user", "content": "draft a lesson from that passage"}],
    )

    assert result.status == "awaiting_approval"
    assert result.pending_tool["tool_call_id"] == "call_1"
    assert result.pending_tool["name"] == "draft_lesson_from_selection"
    assert not any(m["role"] == "tool" for m in result.messages)


def test_split_session_call_suspends_and_never_invokes_the_fn(monkeypatch):
    def _spy(db, **kwargs):
        raise AssertionError("mutation fn must never be called by run_agent_turn")

    _stub_tool(monkeypatch, "split_session", _spy)

    call = ToolCall(
        id="call_1", name="split_session",
        arguments={"session_id": "sess-2", "session_minutes": 30},
    )
    turn1 = AssistantTurn(content="I'll split that session.", tool_calls=[call])
    fake_provider = _FakeProvider([turn1])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(
        None, [{"role": "user", "content": "split session 2, it's too long"}],
    )

    assert result.status == "awaiting_approval"
    assert result.pending_tool["tool_call_id"] == "call_1"
    assert result.pending_tool["name"] == "split_session"
    assert not any(m["role"] == "tool" for m in result.messages)


def test_merge_sessions_call_suspends_and_never_invokes_the_fn(monkeypatch):
    def _spy(db, **kwargs):
        raise AssertionError("mutation fn must never be called by run_agent_turn")

    _stub_tool(monkeypatch, "merge_sessions", _spy)

    call = ToolCall(
        id="call_1", name="merge_sessions",
        arguments={"session_ids": ["sess-1", "sess-2"]},
    )
    turn1 = AssistantTurn(content="I'll merge those sessions.", tool_calls=[call])
    fake_provider = _FakeProvider([turn1])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(
        None, [{"role": "user", "content": "merge sessions 1 and 2"}],
    )

    assert result.status == "awaiting_approval"
    assert result.pending_tool["tool_call_id"] == "call_1"
    assert result.pending_tool["name"] == "merge_sessions"
    assert not any(m["role"] == "tool" for m in result.messages)


def test_add_session_call_suspends_and_never_invokes_the_fn(monkeypatch):
    def _spy(db, **kwargs):
        raise AssertionError("mutation fn must never be called by run_agent_turn")

    _stub_tool(monkeypatch, "add_session", _spy)

    call = ToolCall(
        id="call_1", name="add_session",
        arguments={"lesson_id": "lesson-1", "title": "Extra practice"},
    )
    turn1 = AssistantTurn(content="I'll add that session.", tool_calls=[call])
    fake_provider = _FakeProvider([turn1])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(
        None, [{"role": "user", "content": "add another session to that lesson"}],
    )

    assert result.status == "awaiting_approval"
    assert result.pending_tool["tool_call_id"] == "call_1"
    assert result.pending_tool["name"] == "add_session"
    assert not any(m["role"] == "tool" for m in result.messages)
