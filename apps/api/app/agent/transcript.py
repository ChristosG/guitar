"""Lossless round-trip between persisted `Message` rows (`app.models.chat`)
and the OpenAI wire-shape transcript `app.agent.loop.run_agent_turn`
consumes (`messages: list[dict]`) and produces (`AgentResult.messages`).

`run_agent_turn` itself is SESSION-AGNOSTIC — it never persists anything
(see `loop.py`'s own module docstring). This module is the other half: Task
4's chat router (`app.routers.chat`) is what actually owns turning a
session's `Message` rows into the wire list the loop consumes
(`messages_to_wire`), and turning the loop's output back into new rows
(`persist_new_messages`), on every turn and on every HITL resolve/resume.

Neither function ever touches the system prompt: `SYSTEM_PROMPT` (`app.
agent.prompts`) is re-prepended by `run_agent_turn` itself every call
(`_ensure_system_prompt`) — persisting it as a `Message` would duplicate it
turn over turn, so `persist_new_messages` skips a `{"role": "system"}` wire
entry outright, and `messages_to_wire` never emits one for a caller to skip
in the first place (no persisted row ever has `role == "system"`).
"""
import uuid

from app.models.chat import Message


def messages_to_wire(rows: list[Message]) -> list[dict]:
    """Rebuild the OpenAI wire-shape transcript from persisted `Message`
    rows, in the given order (callers are expected to have already ordered
    `rows` correctly — this function does no ordering of its own). The exact
    inverse of `persist_new_messages`.

    - "user" -> `{"role": "user", "content": ...}`.
    - "assistant" -> `{"role": "assistant", "content": ...}`, PLUS a
      `tool_calls` key (the stored JSON, verbatim) only when the row actually
      has one — omitted entirely (not `[]`/`None`) for a plain-text turn,
      mirroring `loop.py`'s own `_wire_assistant_message` contract exactly
      (a different wire shape than an explicit empty list).
    - "tool" -> `{"role": "tool", "tool_call_id": ..., "content": ...}`.

    Any other stored `role` (not expected in practice — `persist_new_messages`
    only ever writes these three) is skipped rather than raising, matching
    this codebase's general "guard, don't crash on unexpected shape"
    convention (e.g. `loop.py`'s unknown-tool-name guard).
    """
    wire: list[dict] = []
    for row in rows:
        if row.role == "user":
            wire.append({"role": "user", "content": row.content})
        elif row.role == "assistant":
            message: dict = {"role": "assistant", "content": row.content}
            if row.tool_calls:
                message["tool_calls"] = row.tool_calls
            wire.append(message)
        elif row.role == "tool":
            wire.append({
                "role": "tool",
                "tool_call_id": row.tool_call_id,
                "content": row.content,
            })
    return wire


def persist_new_messages(
    db, session_id: uuid.UUID, wire_tail: list[dict], *, citations: list[dict] | None = None,
) -> list[Message]:
    """Persist each wire-shape message in `wire_tail`, IN ORDER, as a new
    `Message` row — the exact inverse of `messages_to_wire`. Returns the
    created rows, also in order. A `{"role": "system"}` entry is skipped
    entirely (see this module's own docstring for why).

    `citations` (Plan 11 Task 1, C2) — `AgentResult.citations` for the turn
    this tail belongs to — is attached to the LAST persisted row, and only
    when that row is "assistant": that's the one row a citation chip could
    ever point at (the turn's final answer, or the assistant message that
    proposes a suspended mutation — the pre-hop already ran either way, see
    `loop.py`'s own `AgentResult.citations` docstring for why suspending
    doesn't clear it). Every existing call site that doesn't pass
    `citations` is unaffected — `None` here is a no-op, identical to this
    function's behavior before the parameter existed.

    Commits ONCE PER MESSAGE — deliberately not once for the whole batch.
    `TimestampMixin.created_at` is `server_default=func.now()`, which is
    Postgres's TRANSACTION-START time: every row written inside the SAME
    transaction gets an IDENTICAL `created_at`. `Message` has no dedicated
    sequence/order column (unlike e.g. `Block.order`), so `messages_to_wire`
    callers reload a session's rows via `ORDER BY created_at, id` to
    reconstruct turn order — batching multiple rows into one commit would
    give same-turn rows a tied `created_at`, making their relative order
    effectively random (`id` is a `uuid4`, not time-ordered, so it cannot
    break the tie correctly). Committing per message instead gives each row
    its own transaction-start timestamp, strictly increasing across the loop,
    so a multi-message tail (e.g. an assistant-with-tool_calls turn followed
    by its tool-result) always reloads in the order it was written.
    """
    persisted: list[Message] = []
    for wire_message in wire_tail:
        role = wire_message.get("role")
        if role == "system":
            continue
        row = Message(
            session_id=session_id,
            role=role,
            content=wire_message.get("content"),
            tool_calls=wire_message.get("tool_calls") if role == "assistant" else None,
            tool_call_id=wire_message.get("tool_call_id") if role == "tool" else None,
        )
        db.add(row)
        db.commit()
        persisted.append(row)
    if citations and persisted and persisted[-1].role == "assistant":
        persisted[-1].citations = citations
        db.commit()
    return persisted
