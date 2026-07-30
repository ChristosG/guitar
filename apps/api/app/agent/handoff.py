"""Single-use handoff of a streamed turn's already-billed first model
response to the REST retry that follows it — the fix for CORE_DECISIONS.md
§3 ("Streaming chat still double-bills tool-calling turns").

THE BUG THIS CLOSES. `POST /chat/{id}/messages/stream` runs a full
`chat_tools_stream` call; when that response turns out to carry tool_calls
(or a C3 tablature bluff), `stream_plain_turn` cannot finish the turn over
SSE and emits a `"fallback"` event, and the browser re-sends the identical
`content` through `POST /chat/{id}/messages` — whose `run_agent_turn` then
re-ran the IDENTICAL first model call. Every tool-calling turn was billed
~2x. This dates from the free-vLLM era, when the second call cost nothing.

THE FIX'S SHAPE (option (b) of the two the doc sketches — "the loop needs
to accept a pre-computed first response"). The client's fallback→REST
protocol is kept byte-identical (the SSE `fallback` event still carries only
`{"reason"}`; `apps/web` is untouched). Server-side, the streaming router
stashes the parsed `AssistantTurn` it already paid for HERE, keyed by
session, and the REST endpoint claims it and hands it to `run_agent_turn(...,
precomputed_first=...)`, which consumes it IN PLACE OF its first
`provider.chat_tools` call — every subsequent hop (tool dispatch, HITL
suspend, the continuation call after a read tool) runs through the existing,
unchanged loop. Option (a) — continuing the agentic loop inside the stream
request — was rejected: the HITL approval gate (C6) suspends the REST loop
and is resolved by a separate endpoint, so a mid-stream mutation would need
the approval-card contract re-implemented over SSE, duplicating the single
most safety-critical mechanism in the app for no billing benefit this
handoff doesn't already deliver.

WHY IN-MEMORY, NOT A DB ROW. The whole app is one uvicorn process (CPU-only
single-user deployment — see docs), the browser's fallback→REST retry is
immediate and same-process, and the stash is a pure COST optimization: losing
it (restart, TTL, eviction) degrades to exactly yesterday's behavior — one
extra billed call — never to a wrong or corrupted transcript. A DB row would
add a migration and a cleanup job for state that is worthless after minutes.

SAFETY RULES (each pinned by `tests/test_chat_tool_turn_billing.py`):
  - SINGLE-USE, ATOMIC: `claim` pops under a lock before validating, so two
    concurrent identical retries can never both replay the same response
    (the loser gets None and pays for a fresh call — correct, just not free).
  - NO CROSS-SESSION LEAKAGE: keyed by session id; a claim for session B can
    never see session A's turn.
  - NO STALE REPLAY: `claim` only returns the turn when the incoming REST
    `content` is byte-equal to the content that produced it; a mismatch still
    CONSUMES the entry (a different message on the session makes the old
    first response contextually stale, so it must not survive to a later
    retry either).
  - TTL: entries expire after `TTL_SECONDS` and expired entries are pruned
    on every stash/claim/discard, so a client that never comes back (tab
    closed after the fallback) leaves nothing behind permanently.
"""
import threading
import time

from app.llm.tools_types import AssistantTurn

# "A few minutes" (the fix brief's own number): long enough for the browser's
# immediate fallback→REST resend even over a bad connection, short enough
# that a replayed first response can never be meaningfully out of date.
TTL_SECONDS = 300.0


class FirstTurnHandoff:
    """A tiny thread-safe {session_id -> (user_content, turn, deadline)}
    store. One slot per session — a newer streamed turn's stash simply
    overwrites an older one (the older response is stale by definition once
    the tutor has moved on to another message).
    """

    def __init__(self, ttl_seconds: float = TTL_SECONDS):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._slots: dict[str, tuple[str, AssistantTurn, float]] = {}

    def stash(self, session_id, user_content: str, turn: AssistantTurn) -> None:
        """Record the already-billed first response for `session_id`'s
        in-flight turn. Called by the streaming router at the moment it emits
        a `"fallback"` the client will answer with a REST resend.
        """
        with self._lock:
            self._prune_locked()
            self._slots[str(session_id)] = (
                user_content, turn, time.monotonic() + self._ttl,
            )

    def claim(self, session_id, user_content: str) -> AssistantTurn | None:
        """Pop-and-validate, atomically. Returns the stashed turn only when
        the session has an unexpired slot AND `user_content` is exactly the
        content that produced it; every call CONSUMES the slot regardless
        (see the module docstring's no-stale-replay rule). None means "call
        the model yourself" — the always-correct degradation.
        """
        with self._lock:
            self._prune_locked()
            slot = self._slots.pop(str(session_id), None)
        if slot is None:
            return None
        stashed_content, turn, _deadline = slot
        if stashed_content != user_content:
            return None
        return turn

    def discard(self, session_id) -> None:
        """Drop the session's slot, if any. Called when the session moves on
        without the designed immediate resend (a streamed turn completed, or
        a resend was refused 409 because an approval appeared in between) —
        whatever is stashed at that point is contextually stale.
        """
        with self._lock:
            self._prune_locked()
            self._slots.pop(str(session_id), None)

    def _prune_locked(self) -> None:
        """Drop expired slots. Caller holds `self._lock`. Runs on every
        public call, which bounds the store to sessions active within one
        TTL — no background sweeper needed for a store this small.
        """
        now = time.monotonic()
        for key in [k for k, (_, _, deadline) in self._slots.items() if deadline <= now]:
            del self._slots[key]


# The one process-wide instance both chat endpoints share (`app/routers/
# chat.py`). Module-level for the same reason `TOOLS` is: one uvicorn
# process, one chat state machine.
FIRST_TURN_HANDOFF = FirstTurnHandoff()
