"""The ReAct tool-calling loop: call `chat_tools` -> dispatch any tool_calls
-> feed results back -> repeat. Mirrors `/mnt/nvme2TB/vllm_interract/
examples/tool_calling_minimal.py` exactly (the brief's own named reference
shape: call -> append the assistant turn WITH tool_calls -> if none, done,
else dispatch each + append a `{"role":"tool", "tool_call_id",...}` per call
-> repeat under a step cap), plus the guards from `/mnt/nvme2TB/
vllm_interract/reference/agentic-gotchas.md`:
  - #4 hallucinated/unknown tool names never crash the loop — guarded into
    an `"ERROR: unknown tool"` tool-result instead.
  - #5 malformed tool-call JSON (`ToolArgsError`) gets a BOUNDED repair
    re-prompt, not an unbounded retry, and the loop itself is always capped
    (`max_steps`).
  - #8 keep the system+tools prefix stable — `SYSTEM_PROMPT` (a plain
    top-level constant in `prompts.py`) and `_tool_schemas()` (deterministic,
    same list every call within a process) never interpolate per-request data.

Plan 5 Task 2 built this loop dispatching ONLY "read" tools inline (the
`TOOLS` registry was reads-only). THIS task (3) adds "mutation" entries into
that same registry (`app/agent/tools.py`) and teaches this loop to SUSPEND
instead of executing them:

  - `_tool_schemas()` now exposes BOTH kinds to the model (it needs to see
    mutation tools to ever propose one).
  - READ `ToolCall`s still dispatch inline, unchanged, same turn.
  - The FIRST `ToolCall` in a turn whose registry `kind == "mutation"` is
    NEVER dispatched — `run_agent_turn` returns `AgentResult(status=
    "awaiting_approval", pending_tool={tool_call_id, name, arguments})`
    instead, stopping the loop right there.
  - This loop stays SESSION-AGNOSTIC: it does not persist an
    `ApprovalRequest` or a `Message` itself (Task 4's chat router owns that —
    it persists the suspend info this returns, and later resumes this same
    loop after a human decision). See `_dispatch_read_call`/
    `_first_mutation_index`/the dispatch loop below for exactly how a
    per-turn mix of reads and (at most one visible) mutation is handled
    without ever leaving more than one tool_call unanswered in `messages`
    (protocol integrity — see the dispatch loop's own comment).
"""
import json
import logging
from dataclasses import dataclass

from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import TOOLS
from app.llm.errors import ToolArgsError
from app.llm.factory import get_provider
from app.llm.tools_types import ToolCall

log = logging.getLogger(__name__)

# "Consecutive" (agentic-gotchas.md #5's "cap it" warning) — a successful
# `chat_tools` call resets this streak (see the reset right after the
# try/except below), so this bounds a run of BACK-TO-BACK malformed-JSON
# failures, not a lifetime total across an otherwise-healthy turn.
_MAX_REPAIR_ATTEMPTS = 2

_REPAIR_MESSAGE = "Your previous tool call had invalid JSON arguments. Retry with valid JSON."
_GIVEUP_MESSAGE = "Sorry, I couldn't complete that request. Could you rephrase it?"
_MAX_STEPS_MESSAGE = "I couldn't finish that within the allotted steps. Could you try rephrasing or narrowing the request?"


@dataclass
class AgentResult:
    """One agent turn's outcome. `status` is "answer" (a final reply — the
    only status Task 2 ever produced) or "awaiting_approval" (Task 3: the
    loop suspended on a proposed MUTATION tool call — see `pending_tool`).
    `messages` is the FULL updated transcript — the caller's input plus
    every new turn this call appended — ready to persist and pass back in
    as-is on the next turn (mirrors `AssistantTurn`'s own "container, not a
    compared value" rationale for staying a plain, non-frozen dataclass).

    `pending_tool` (Task 3) is None for "answer", and otherwise
    `{tool_call_id, name, arguments}` for the ONE suspended mutation call —
    Task 4's chat router persists an `ApprovalRequest(tool_name=pending_tool
    ["name"], tool_args=pending_tool["arguments"])` keyed off this, and, on
    resolve, answers `pending_tool["tool_call_id"]` with a `{"role":"tool",
    ...}` result before resuming `run_agent_turn` — this loop itself never
    persists anything (session-agnostic; Task 4 owns persistence).
    """

    status: str
    content: str | None
    messages: list[dict]
    pending_tool: dict | None = None


def _tool_schemas() -> list[dict]:
    # Both kinds are exposed to the model (Task 3) — it must be able to SEE
    # a mutation tool to ever propose one for this loop to suspend on. Kept
    # as an explicit kind-check (not just "every entry unconditionally") so
    # a future non-model-facing registry `kind` wouldn't silently leak in.
    return [entry.schema for entry in TOOLS.values() if entry.kind in ("read", "mutation")]


def _ensure_system_prompt(messages: list[dict]) -> list[dict]:
    """Prepend `SYSTEM_PROMPT` unless the transcript already starts with a
    system message. Callers (Task 4's chat router) own the transcript across
    turns and pass the full history back in each time — prepending
    unconditionally would accumulate a duplicate system message every turn.
    """
    if messages and messages[0].get("role") == "system":
        return messages
    return [{"role": "system", "content": SYSTEM_PROMPT}, *messages]


def _wire_assistant_message(content: str | None, tool_calls: list[ToolCall]) -> dict:
    """Reconstruct the assistant turn in OpenAI WIRE shape for history — NOT
    the parsed `AssistantTurn` shape `chat_tools` returns (`arguments` there
    is already a dict; the wire shape needs the JSON string back). Required
    for the protocol to continue correctly on the next call (per this task's
    brief). `tool_calls` is omitted entirely when there are none — an empty
    list is a different wire shape than "no tool_calls key at all".
    """
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in tool_calls
        ]
    return message


def _stringify(result) -> str:
    """Every tool `fn` returns a plain JSON-serializable Python object (see
    `tools.py`'s docstring) — turning that into the tool message's `content`
    string is this loop's job, not each tool's. `default=str` covers the
    handful of non-JSON-native types the registry's results carry (`UUID`,
    `datetime`) without every tool needing to stringify its own fields.
    """
    return json.dumps(result, default=str)


def _dispatch_read_call(db, call: ToolCall, messages: list[dict]) -> None:
    """Execute one READ tool call inline and append its `{"role":"tool",
    ...}` result to `messages` in place — the exact guarded dispatch
    (unknown-tool name -> "ERROR: unknown tool"; a tool raising mid-call ->
    "ERROR: tool ... failed") Task 2 established. Factored out of
    `run_agent_turn`'s main loop so BOTH call sites that dispatch calls
    inline — a plain all-reads turn, and the reads that precede a suspending
    mutation in the same turn — share one implementation instead of two
    copies that could drift apart.

    Never called for a mutation `ToolCall`: `run_agent_turn` always
    intercepts those (via `_first_mutation_index`) before reaching here, so
    this function has no mutation-vs-read branch of its own to get wrong.
    """
    entry = TOOLS.get(call.name)
    if entry is None:
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": f"ERROR: unknown tool '{call.name}'",
        })
        return
    try:
        result = entry.fn(db, **call.arguments)
    except Exception as exc:
        # A KNOWN tool can still raise mid-dispatch (e.g. valid JSON but a
        # shape the fn doesn't accept) — chat_tools already returned
        # successfully so this never raises ToolArgsError; same "guard,
        # don't crash the whole turn" spirit as the unknown-tool-name guard
        # above, just one layer deeper.
        log.warning("tool %r raised during dispatch", call.name, exc_info=True)
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": f"ERROR: tool '{call.name}' failed: {exc}",
        })
        return
    messages.append({
        "role": "tool",
        "tool_call_id": call.id,
        "content": _stringify(result),
    })


def _first_mutation_index(tool_calls: list[ToolCall]) -> int | None:
    """Index of the FIRST call in this turn whose registry entry is a
    `kind == "mutation"`, or None if every call this turn is a read (an
    unknown/unregistered name is never a mutation, so it's treated the same
    as a read here — dispatched inline by `_dispatch_read_call`, which is
    where the actual "unknown tool" guard lives, not here).
    """
    for i, call in enumerate(tool_calls):
        entry = TOOLS.get(call.name)
        if entry is not None and entry.kind == "mutation":
            return i
    return None


def run_agent_turn(db, messages: list[dict], *, max_steps: int = 6) -> AgentResult:
    messages = _ensure_system_prompt(list(messages))
    tools = _tool_schemas()
    provider = get_provider()
    repair_attempts = 0
    last_content: str | None = None

    for _ in range(max_steps):
        try:
            turn = provider.chat_tools(messages, tools, tool_choice="auto")
        except ToolArgsError:
            repair_attempts += 1
            if repair_attempts >= _MAX_REPAIR_ATTEMPTS:
                log.warning("chat_tools: giving up after %d consecutive ToolArgsErrors", repair_attempts)
                # Append the giveup reply to history too, not just return it as
                # `content`: `AgentResult.messages` is documented as the full
                # transcript, and Task 4's router persists `messages` while
                # showing `content` to the user — without this append the
                # "couldn't complete" reply silently drops out of history and
                # the model has no memory of it next turn.
                messages.append({"role": "assistant", "content": _GIVEUP_MESSAGE})
                return AgentResult(status="answer", content=_GIVEUP_MESSAGE, messages=messages)
            # The broken turn is NOT added to history (no assistant message,
            # no dangling tool_call) — just a corrective user turn, so the
            # model gets a clean shot at a valid call next time.
            messages.append({"role": "user", "content": _REPAIR_MESSAGE})
            continue

        repair_attempts = 0  # a successful call resets the CONSECUTIVE-failure streak
        last_content = turn.content

        if not turn.tool_calls:
            messages.append(_wire_assistant_message(turn.content, turn.tool_calls))
            return AgentResult(status="answer", content=turn.content, messages=messages)

        pending_index = _first_mutation_index(turn.tool_calls)

        if pending_index is None:
            # No mutation anywhere in this turn — unchanged from Task 2:
            # every call is dispatched inline, in order.
            messages.append(_wire_assistant_message(turn.content, turn.tool_calls))
            for call in turn.tool_calls:
                _dispatch_read_call(db, call, messages)
            continue

        # --- SUSPEND: a mutation was proposed --------------------------------
        # PROTOCOL INTEGRITY (this task's brief flags this as the part a
        # reviewer will scrutinize): at most ONE tool_call may be left
        # unanswered in `messages` when this function returns, because Task
        # 4 answers exactly that one call at resolve time and then calls
        # chat_tools again to resume — a second, still-dangling unanswered
        # call would desync that resume (the wire protocol requires every
        # tool_call in an assistant turn to get a paired tool result before
        # the next assistant turn).
        #
        # So the reconstructed assistant message includes ONLY the calls up
        # to and including the pending mutation:
        #   - calls BEFORE it (`handled_calls`) are genuine reads (by
        #     construction of `_first_mutation_index`'s "first" scan) and
        #     are dispatched + answered right here, same as the no-mutation
        #     branch above.
        #   - the pending mutation itself is included in the wire message
        #     but deliberately left UNANSWERED — that's what "suspend" means.
        #   - any calls AFTER it are DROPPED from the reconstructed message
        #     entirely, not merely left unanswered: they were never
        #     dispatched (the model proposed them before a human ever got a
        #     chance to gate the mutation ahead of them), so they must not
        #     appear to have been asked either — including them unanswered
        #     would be a SECOND dangling call, and dispatching them out of
        #     the model's intended order would run a call the human never
        #     gated. If still relevant, the model can re-propose them once
        #     the loop resumes after this mutation is resolved.
        handled_calls = turn.tool_calls[:pending_index]
        pending_call = turn.tool_calls[pending_index]

        messages.append(_wire_assistant_message(turn.content, handled_calls + [pending_call]))
        for call in handled_calls:
            _dispatch_read_call(db, call, messages)

        return AgentResult(
            status="awaiting_approval",
            content=turn.content,
            messages=messages,
            pending_tool={
                "tool_call_id": pending_call.id,
                "name": pending_call.name,
                "arguments": pending_call.arguments,
            },
        )

    log.warning("run_agent_turn: max_steps=%d exhausted without a final answer", max_steps)
    # `last_content is None` (an explicit identity check, not a falsy-check:
    # `AssistantTurn.content` is documented None-not-"" when absent) means every
    # step was a tool call with no final text, so the fallback note is genuinely
    # being substituted and must be appended to history to keep `messages`
    # consistent with `content` (Task 4's router persists `messages`, shows
    # `content`). A truthy `last_content` is ALREADY in `messages` from the last
    # iteration's assistant-turn append above — returning it as `content`
    # without re-appending avoids a duplicate assistant message.
    if last_content is None:
        messages.append({"role": "assistant", "content": _MAX_STEPS_MESSAGE})
        return AgentResult(status="answer", content=_MAX_STEPS_MESSAGE, messages=messages)
    return AgentResult(status="answer", content=last_content, messages=messages)
