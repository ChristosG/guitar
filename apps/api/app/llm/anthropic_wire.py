"""OpenAI wire-shape <-> Anthropic block-shape. Pure translation, no SDK import.

THE DECISION THIS FILE ENCODES: the persisted format does NOT change.

`models.chat.Message.tool_calls` holds OpenAI-shaped function calls, and
`agent/transcript.py` round-trips them losslessly. We could have migrated that
column to Anthropic's block shape. We deliberately do not:

  - every existing chat row in the tutor's live DB would need a data migration,
    for zero user-visible benefit;
  - `agent/loop.py` (the ReAct loop, the HITL suspend/resume protocol, the
    tablature guard, the forced-retrieval pre-hop) is written against the
    OpenAI shape in ~15 places. Rewriting all of it to swap a vendor is exactly
    the coupling the `LLMProvider` ABC exists to prevent;
  - a provider is supposed to be swappable. If swapping one rewrites the
    database, the seam was in the wrong place.

So translation happens HERE, at the provider boundary, and the app above it
keeps speaking one dialect. `ClaudeProvider` imports these functions; nothing
else does.

THE THREE THINGS THAT ACTUALLY BITE (each has a test):

1. TOOL RESULTS MUST BE COALESCED. OpenAI emits one `{"role": "tool"}` message
   per result. Anthropic requires ALL results for one assistant turn to arrive
   in a SINGLE user message, as parallel `tool_result` blocks. Send them as
   three consecutive user messages and you get a 400 — and the model has, by
   then, been trained by your own transcript to stop making parallel calls.

2. EMPTY TEXT BLOCKS 400. Qwen happily persists `content: ""` on a
   tool-calls-only assistant turn (the OpenAI SDK returns `None`, but a "" can
   reach the DB by other routes). Anthropic rejects a text block whose text is
   empty or whitespace. We drop them.

3. A DANGLING `tool_use` IS A 400, and it is REACHABLE. `loop.py`'s HITL
   suspend deliberately returns a transcript whose last assistant turn has ONE
   unanswered tool call — that is what "awaiting approval" *is*, and it gets
   persisted that way. It is balanced again before the next model call (the
   resolve path appends the tool result first), so in correct operation Claude
   never sees it dangling. But "in correct operation" is not a guarantee, and
   the failure mode is an opaque vendor 400 on the single most important path
   in the app. So `to_anthropic` checks the invariant itself and raises a named
   error naming the unanswered call.
"""
from __future__ import annotations

import json
from typing import Any

from app.llm.tools_types import AssistantTurn, ToolCall


class DanglingToolUseError(RuntimeError):
    """An assistant turn's `tool_use` has no matching `tool_result`.

    Raised instead of letting Anthropic answer with a generic 400. See point 3
    of the module docstring: this is reachable via the HITL suspend/resume path,
    and a vendor error message would send the next person debugging it to the
    wrong place entirely.
    """


def to_anthropic_tools(openai_tools: list[dict]) -> list[dict]:
    """`{"type": "function", "function": {name, description, parameters}}`
    -> `{"name", "description", "input_schema"}`.

    `strict: true` is deliberately NOT set. It would require every tool's schema
    to carry `additionalProperties: false` with every property required — but
    the registry in `agent/tools.py` has genuinely optional params (`k`,
    `page_no`, ...), and forcing them all would change tool semantics to satisfy
    a vendor flag. We do not need it either: `tool_use.input` arrives as an
    already-parsed dict from the SDK, so `ToolArgsError` (a malformed
    arguments STRING — a Qwen failure mode) is structurally impossible on this
    provider. The schema is a hint to the model, and Pydantic/the dispatcher
    validate for real.
    """
    out: list[dict] = []
    for tool in openai_tools:
        fn = tool.get("function", tool)
        out.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return out


def to_anthropic(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """OpenAI transcript -> `(system_prompt, anthropic_messages)`.

    The system prompt is RETURNED SEPARATELY, not left in the list: Anthropic
    takes it as a top-level `system=` parameter, not as a message. `loop.py`
    re-prepends `SYSTEM_PROMPT` on every call (`_ensure_system_prompt`), so it
    is always present at index 0 and never persisted.
    """
    system: str | None = None
    out: list[dict] = []
    # tool_use ids seen in the assistant turn we are currently answering, so a
    # dangling one can be named in the error rather than reported as "a 400".
    open_calls: dict[str, str] = {}

    for msg in messages:
        role = msg.get("role")

        if role == "system":
            system = msg.get("content") or None
            continue

        if role == "user":
            _append(out, "user", _text_blocks(msg.get("content")))
            continue

        if role == "assistant":
            blocks: list[dict] = _text_blocks(msg.get("content"))
            for call in msg.get("tool_calls") or []:
                fn = call["function"]
                # The wire shape stores `arguments` as a JSON STRING (see
                # loop.py::_wire_assistant_message); Anthropic wants a dict.
                raw = fn.get("arguments") or "{}"
                args = json.loads(raw) if isinstance(raw, str) else raw
                blocks.append(
                    {"type": "tool_use", "id": call["id"], "name": fn["name"], "input": args}
                )
                open_calls[call["id"]] = fn["name"]
            # An assistant turn with no text AND no tool calls is not
            # representable — and never produced by loop.py. Skip rather than
            # emit an empty content array (also a 400).
            if blocks:
                _append(out, "assistant", blocks)
            continue

        if role == "tool":
            call_id = msg.get("tool_call_id")
            open_calls.pop(call_id, None)
            block = {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": msg.get("content") or "",
            }
            # COALESCE: if the previous emitted message is already a user turn
            # carrying tool_results, this result joins it rather than starting a
            # new message. See point 1 of the module docstring.
            if out and out[-1]["role"] == "user" and _is_tool_result_turn(out[-1]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

    if open_calls:
        names = ", ".join(f"{cid}({name})" for cid, name in open_calls.items())
        raise DanglingToolUseError(
            f"assistant tool_use with no tool_result: {names}. Anthropic rejects "
            f"this with an opaque 400. This is reachable via the HITL "
            f"suspend/resume path (agent/loop.py leaves exactly one call "
            f"unanswered when it suspends) — the resolve path must append the "
            f"tool result BEFORE the transcript is sent back to the model."
        )
    return system, out


def from_anthropic(response: Any) -> AssistantTurn:
    """An Anthropic `Message` -> the `AssistantTurn` the ReAct loop expects.

    `content` is `None` — never `""` — for a tool-calls-only turn, matching what
    the OpenAI SDK returns and what `loop.py` branches on.
    """
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(block.text)
        elif btype == "tool_use":
            # `input` is already a parsed dict — no JSON string to mis-parse,
            # which is why `ToolArgsError` cannot occur on this provider.
            calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))
    content = "".join(text_parts).strip() or None
    return AssistantTurn(content=content, tool_calls=calls)


# ---------------------------------------------------------------------------


def _text_blocks(content: Any) -> list[dict]:
    """Text content -> blocks, dropping empty/whitespace text (point 2)."""
    if content is None:
        return []
    if isinstance(content, list):  # already blocks; pass through
        return [b for b in content if b]
    text = str(content)
    return [{"type": "text", "text": text}] if text.strip() else []


def _is_tool_result_turn(message: dict) -> bool:
    content = message.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and all(b.get("type") == "tool_result" for b in content)
    )


def _append(out: list[dict], role: str, blocks: list[dict]) -> None:
    """Append, MERGING into the previous message when the role repeats.

    Anthropic rejects two consecutive messages with the same role. The OpenAI
    transcript can legitimately produce them — e.g. `loop.py`'s forced-retrieval
    pre-hop injects a GROUNDING user message immediately before the tutor's own
    user message.
    """
    if not blocks:
        return
    if out and out[-1]["role"] == role and not _is_tool_result_turn(out[-1]):
        out[-1]["content"].extend(blocks)
        return
    out.append({"role": role, "content": blocks})
