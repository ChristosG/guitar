"""The ReAct tool-calling loop (Plan 5 Task 2): call `chat_tools` -> dispatch
any tool_calls -> feed results back -> repeat. Mirrors
`/mnt/nvme2TB/vllm_interract/examples/tool_calling_minimal.py` exactly (the
brief's own named reference shape: call -> append the assistant turn WITH
tool_calls -> if none, done, else dispatch each + append a `{"role":"tool",
"tool_call_id",...}` per call -> repeat under a step cap), plus the guards
from `/mnt/nvme2TB/vllm_interract/reference/agentic-gotchas.md`:
  - #4 hallucinated/unknown tool names never crash the loop — guarded into
    an `"ERROR: unknown tool"` tool-result instead.
  - #5 malformed tool-call JSON (`ToolArgsError`) gets a BOUNDED repair
    re-prompt, not an unbounded retry, and the loop itself is always capped
    (`max_steps`).
  - #8 keep the system+tools prefix stable — `SYSTEM_PROMPT` (a plain
    top-level constant in `prompts.py`) and `_tool_schemas()` (deterministic,
    same list every call within a process) never interpolate per-request data.

This task dispatches ONLY "read" tools inline — the whole `TOOLS` registry is
reads-only for now (`app/agent/tools.py`). Task 3 adds "mutation" entries and
teaches this loop to suspend instead of executing them; not built here.
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
    """One agent turn's outcome. `status` is always "answer" out of this
    task (Task 3 adds "awaiting_approval" once mutation tools exist to
    suspend on). `messages` is the FULL updated transcript — the caller's
    input plus every new turn this call appended — ready to persist and pass
    back in as-is on the next turn (mirrors `AssistantTurn`'s own "container,
    not a compared value" rationale for staying a plain, non-frozen dataclass).
    """

    status: str
    content: str | None
    messages: list[dict]


def _tool_schemas() -> list[dict]:
    return [entry.schema for entry in TOOLS.values() if entry.kind == "read"]


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
                return AgentResult(status="answer", content=_GIVEUP_MESSAGE, messages=messages)
            # The broken turn is NOT added to history (no assistant message,
            # no dangling tool_call) — just a corrective user turn, so the
            # model gets a clean shot at a valid call next time.
            messages.append({"role": "user", "content": _REPAIR_MESSAGE})
            continue

        repair_attempts = 0  # a successful call resets the CONSECUTIVE-failure streak
        last_content = turn.content
        messages.append(_wire_assistant_message(turn.content, turn.tool_calls))

        if not turn.tool_calls:
            return AgentResult(status="answer", content=turn.content, messages=messages)

        for call in turn.tool_calls:
            entry = TOOLS.get(call.name)
            if entry is None:
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": f"ERROR: unknown tool '{call.name}'",
                })
                continue
            try:
                result = entry.fn(db, **call.arguments)
            except Exception as exc:
                # A KNOWN tool can still raise mid-dispatch (e.g. valid JSON
                # but a shape the fn doesn't accept) — chat_tools already
                # returned successfully so this never raises ToolArgsError;
                # same "guard, don't crash the whole turn" spirit as the
                # unknown-tool-name guard above, just one layer deeper.
                log.warning("tool %r raised during dispatch", call.name, exc_info=True)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": f"ERROR: tool '{call.name}' failed: {exc}",
                })
                continue
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": _stringify(result),
            })

    log.warning("run_agent_turn: max_steps=%d exhausted without a final answer", max_steps)
    return AgentResult(status="answer", content=last_content or _MAX_STEPS_MESSAGE, messages=messages)
