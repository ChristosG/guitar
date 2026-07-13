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

Plan 11 Task 1 (C1/C2 — "forced retrieval, not requested") adds a pre-hop
BEFORE the main loop below ever calls the model: on a fresh, CONTENT-BEARING
user turn (see `_is_content_bearing`'s own docstring for the exact rule and
its defence), `run_agent_turn` itself calls `app.brain.retrieve.search` —
not a tool the model may or may not decide to invoke — and appends the hits
as a `GROUNDING` block (`_grounding_block`) onto the END of that SAME user
turn's own content, before the first `chat_tools` call (NOT a second system
message — the live vLLM chat template 400s on a system message that isn't
the very first one, and a mid-transcript system message would defeat
agentic-gotchas.md #8's prefix-caching invariant regardless; see
`_grounding_block`'s own docstring). The model never gets a turn where it
could have skipped retrieval. Every hit is also surfaced as a citation
(`_to_citation`) on `AgentResult.citations`, persisted onto the `Message` row
by `app/routers/chat.py` via `app.agent.transcript.persist_new_messages` —
this is what lets the UI show a citation chip back to a real page. A turn
with NO hits (or none clearing `_RELEVANCE_FLOOR`) still gets a GROUNDING
block — one that instructs the model to say his library doesn't cover this
and label the rest of the answer as general knowledge, rather than silently
falling back to unlabelled pretrained knowledge. This pre-hop runs BEFORE
the mutation-suspend logic below and does not touch it in any way: it only
ever rewrites the trailing user message's own content before the loop
starts, and only ever adds `citations` to the `AgentResult` this function
already returns from every branch — C6 (the approval gate) is unchanged.

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

Plan 11 Task 2 (C3) adds a POST-TURN guard on top of all of the above: when a
turn ends with a plain answer (no tool_calls), and that answer's `content`
trips `app.agent.guards.looks_like_tablature` (a free-typed ASCII tab —
Chris's exact bug, see that module's own docstring), the bluff is NEVER
appended to `messages` and NEVER returned as `content`. The loop re-prompts
the model ONCE with a corrective user turn naming the tool it should have
called (`_TAB_BLUFF_REPROMPT_MESSAGE`), bounded by `_MAX_TAB_BLUFF_ATTEMPTS`
the same "bounded, not unbounded" way `_MAX_REPAIR_ATTEMPTS` bounds the
ToolArgsError repair loop above; if the model bluffs again anyway, an honest
fallback (`_TAB_BLUFF_FALLBACK_MESSAGE`) is substituted instead — see the
comment above those constants for why re-prompting (not auto-proposing a
`generate_artifact` call the guard itself would have to guess the args for)
is the right recovery. This is entirely independent of the mutation-suspend
machinery above: it only ever fires on the "no tool_calls" branch, so it
cannot interact with — and does not touch — the HITL suspend path at all.
"""
import json
import logging
import re
from dataclasses import dataclass, field

from app.agent.guards import looks_like_tablature
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import TOOLS
from app.brain.retrieve import search
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

# --- C3: no free-typed tablature ------------------------------------------
#
# Chris's exact bug: asked for a G major scale tab, the model typed ASCII
# into a code fence (also WRONG — see `app/agent/guards.py`'s docstring)
# instead of calling `generate_artifact`. `looks_like_tablature` (imported
# above) detects that shape; this bounded counter/message pair is this
# loop's RECOVERY once it does — same "bounded, not unbounded" shape as
# `_MAX_REPAIR_ATTEMPTS`'s ToolArgsError repair above, deliberately reusing
# that precedent rather than inventing a new retry idiom.
#
# RECOVERY CHOICE (the brief asks for this to be defended): option (a) —
# suppress the bluff and re-prompt the model ONCE, telling it plainly what
# it did wrong and which tool to call instead — with option (b) — an honest
# "I'll generate that properly" fallback — as the bound's fallback if the
# model bluffs again anyway. Rejected (c) "auto-propose a generate_artifact
# call": the loop has no reliable way to synthesize a correct `kind`/
# `prompt` from a bluff's prose (a mis-guessed kind/prompt would show the
# tutor an HITL approval card for a request he didn't actually make, which
# is worse than an honest "let me redo that" — the approval card is meant to
# gate a call the MODEL chose, not one this guard invented on its behalf).
# Re-prompting instead gives the model a real chance to make the right call
# itself (this is exactly what the live-model check in the task report
# verifies actually happens), and the bounded fallback (b) still guarantees
# the invariant even in the worst case: the bluff NEVER reaches
# `AgentResult.content` or `AgentResult.messages`, no matter how many times
# the model keeps bluffing.
_MAX_TAB_BLUFF_ATTEMPTS = 1
_TAB_BLUFF_REPROMPT_MESSAGE = (
    "You just wrote tablature/a chord diagram as plain text instead of "
    "calling generate_artifact. Never do that — call generate_artifact now "
    "so it renders as a real, playable artifact instead of typed ASCII."
)
_TAB_BLUFF_FALLBACK_MESSAGE = (
    "Let me generate that properly as a real, playable artifact instead of "
    "typing it out — please ask again and I'll call the right tool."
)


# ---------------------------------------------------------------------------
# C1: forced retrieval pre-hop — the content-bearing rule
# ---------------------------------------------------------------------------
#
# THE RULE: a turn forces a library search only when it plausibly asks a
# guitar technique/theory/gear/tone QUESTION. Two categories are excluded
# even though neither is small talk:
#   1. small talk / pleasantries ("hi", "hello", "thanks") — nothing to
#      look up.
#   2. an INSTRUCTION about an entity already in the tutor's OWN data (a
#      student, curriculum, lesson, session, note, progress entry, or a
#      request for a generated artifact/tab/diagram/scale) — these are
#      resolved by a read/mutation tool (`app/agent/tools.py`), never by a
#      library search: "split session 2 of that lesson" is about session #2
#      of one specific row, not a question his book could ever answer, and
#      forcing a search on it would inject irrelevant "grounding" noise into
#      a command turn for no benefit.
#
# DEFENCE: kept a plain regex/keyword rule — deliberately NOT an LLM call.
# The entire point of C1 is removing an unreliable judgment call FROM the
# model ("the model routinely declines to retrieve"); asking a model
# (possibly the very same one) to classify the turn first would just move
# that same unreliability one hop earlier and re-introduce exactly the
# failure mode this task exists to remove. A keyword rule is auditable,
# deterministic, and — because both exclusion categories above are already
# literally enumerated in `SYSTEM_PROMPT` itself ("students, curricula,
# lessons, sessions, notes, progress, artifacts" vs "any guitar technique/
# theory/gear/tone question") — this rule is not a new taxonomy, it is the
# SAME split the prompt already draws, just enforced in code instead of
# requested in prose. See `tests/test_agent_grounding.py`'s "TEST BOTH
# SIDES" section for the concrete cases this is pinned against (small talk,
# entity instructions, and genuine content questions).
_SMALL_TALK_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|sup|thanks|thank you|ok|okay|cool|bye|goodbye|"
    r"good\s+(morning|afternoon|evening|night)|how'?s?\s+it\s+going|"
    r"how\s+are\s+you|what'?s\s+up)\b",
    re.IGNORECASE,
)

# Nouns naming the tutor's OWN data (students/curricula/lessons/sessions/
# notes/progress) or a structured artifact request (tab/chord diagram/scale)
# — exactly the domain `SYSTEM_PROMPT` already routes to a read/mutation
# tool rather than a knowledge question about the library.
_ENTITY_OR_ARTIFACT_RE = re.compile(
    r"\b(students?|curricul(?:um|a)\w*|lessons?|sessions?|notes?|progress|"
    r"artifacts?|chord\s+diagrams?|diagrams?|tabs?|scales?)\b",
    re.IGNORECASE,
)

# A positive signal that the turn is actually ASKING something, not issuing
# a bare command ("do three things", "split session 2") — a literal "?", or
# a leading wh-question word. Deliberately excludes bare modal starters
# ("do", "can", "is", ...): those are just as common as imperative-sentence
# openers ("do three things") as they are real questions, so including them
# produced false positives on exactly the entity-instruction turns category
# 2 above exists to exclude.
_QUESTION_RE = re.compile(r"\?|^\s*(what|why|how|when|where|which)\b", re.IGNORECASE)

# Conservative on purpose: this PoC has no calibration data for what a
# "genuinely relevant" cosine score looks like against this corpus/embedder,
# so the floor only screens out clear noise (near-zero/negative similarity)
# rather than risking false-negative filtering of a real hit. Revisit with
# real usage data once there's a distribution to tune against.
_RELEVANCE_FLOOR = 0.15


def _is_content_bearing(text: str) -> bool:
    """True iff `text` plausibly asks a guitar technique/theory/gear/tone
    question — see the module-level comment block above for the full rule
    and its defence.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _SMALL_TALK_RE.match(stripped):
        return False
    if _ENTITY_OR_ARTIFACT_RE.search(stripped):
        return False
    return bool(_QUESTION_RE.search(stripped))


def _snippet(text: str, limit: int = 300) -> str:
    """A bounded preview of a hit's chunk text for a citation payload — the
    full text already reached the model via the GROUNDING block; this is
    only what a citation chip would show, not what the model reasons over.
    """
    stripped = text.strip()
    return stripped if len(stripped) <= limit else stripped[:limit].rstrip() + "…"


def _to_citation(hit) -> dict:
    """One `AgentResult.citations` entry (C2) from an `app.brain.retrieve.
    Hit` — `{source_id, source_title, page_no, page_id, snippet}`, the exact
    shape the brief specifies so the UI can render a chip that deep-links
    into the Reader at the cited page. `page_no`/`page_id` are None when the
    chunk predates page-addressable ingest (`Hit`'s own documented case) —
    passed through as-is rather than papered over.
    """
    return {
        "source_id": str(hit.source_id),
        "source_title": hit.source_title,
        "page_no": hit.page,
        "page_id": str(hit.page_id) if hit.page_id is not None else None,
        "snippet": _snippet(hit.text),
    }


_NO_HITS_GROUNDING = (
    "GROUNDING: searched the tutor's library for this question and found nothing "
    "relevant. Say plainly, in your answer, that his material doesn't cover this, "
    "and label the rest of your answer as general knowledge (not from his library)."
)


def _grounding_block(hits: list) -> str:
    """The injected context TEXT (C1/C2) — appended onto the end of the
    user's OWN turn (see `run_agent_turn`'s pre-hop), deliberately NOT a
    second `{"role": "system", ...}` message: the real vLLM chat template
    this app targets 400s a system message that isn't the very first one
    ("System message must be at the beginning") — confirmed against the
    live server while building this pre-hop, not a theoretical concern. A
    mid-transcript system message would also defeat agentic-gotchas.md #8's
    prefix-caching invariant (`loop.py`'s own module docstring) even if the
    server tolerated it: that invariant is about `SYSTEM_PROMPT` staying
    BYTE-IDENTICAL turn over turn, and retrieved passages are per-query by
    definition. Appending to the user turn instead keeps index 0 untouched
    (still exactly `SYSTEM_PROMPT`) and the dynamic part where it belongs —
    in the part of the prefix that was already going to change this turn.

    Empty `hits` (no results, or every result screened out by
    `_RELEVANCE_FLOOR`) gets the explicit "say so, label as general
    knowledge" instruction (`_NO_HITS_GROUNDING`) rather than silently
    omitting a grounding block — an omitted block is indistinguishable from
    "retrieval wasn't attempted" and invites exactly the unlabelled-
    pretrained-knowledge failure C2 exists to prevent.
    """
    if not hits:
        return _NO_HITS_GROUNDING
    passages = "\n\n".join(
        f"[{i}] (source_id={hit.source_id}, page={hit.page}) {hit.text}"
        for i, hit in enumerate(hits, start=1)
    )
    return (
        "GROUNDING — from the tutor's library, the passages most relevant to this "
        f"question:\n\n{passages}\n\n"
        "Answer from this context and cite the passages you use inline as [n]. If "
        "none of it actually answers the question, say his material doesn't cover "
        "this and label the rest of your answer as general knowledge."
    )


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

    `citations` (Plan 11 Task 1, C1/C2) is the list of `_to_citation(hit)`
    dicts for whatever this turn's forced-retrieval pre-hop found —
    `[]` when the turn wasn't content-bearing (no search ran at all) OR the
    search ran but returned nothing above `_RELEVANCE_FLOOR`. Populated on
    EVERY return branch below (not just the plain "answer" path), since a
    turn can still be genuinely grounded even when it also suspends on a
    mutation in the same reply (see `test_agent_grounding.py`'s C1/C6
    composition test) — `app/routers/chat.py` persists this onto the
    `Message` row it writes for the turn's own trailing assistant message.
    """

    status: str
    content: str | None
    messages: list[dict]
    pending_tool: dict | None = None
    citations: list[dict] = field(default_factory=list)


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
    tab_bluff_attempts = 0
    last_content: str | None = None
    citations: list[dict] = []

    # --- C1: forced retrieval pre-hop ---------------------------------------
    # Fires ONLY when the newest message in the transcript is a fresh user
    # turn (i.e. `messages[-1]["role"] == "user"`) — a resumed turn after an
    # HITL resolve always ends with a `{"role": "tool", ...}` answer to the
    # approved/rejected mutation, never a user message, so this never
    # re-triggers mid-approval. `search()` is called directly — not offered
    # to the model as a tool it might decline — and its hits are appended
    # onto the END of the user's own turn (see `_grounding_block`'s own
    # docstring for why NOT a second system message) BEFORE the loop's first
    # `chat_tools` call below, so every branch that follows (plain answer,
    # mutation suspend, max_steps fallback, ...) already has grounding in
    # `messages` by construction.
    #
    # `messages[-1] = {...}` REPLACES the list slot with a NEW dict rather
    # than mutating `last["content"]` in place: `messages` here shares its
    # element objects (not just the list) with the caller's own transcript
    # (`_ensure_system_prompt` unpacks the caller's original dicts into a new
    # list, but doesn't copy the dicts themselves) — `app/routers/chat.py`
    # holds onto that same list as `prior_wire` and diffs against it
    # (`_new_tail`) to compute what to persist. An in-place mutation would
    # retroactively rewrite `prior_wire`'s own last entry too, corrupting
    # that diff; reassigning the slot only ever changes what THIS function's
    # local list points to.
    last = messages[-1]
    if last.get("role") == "user" and _is_content_bearing(last.get("content") or ""):
        raw_hits = search(db, last["content"], k=5)
        hits = [hit for hit in raw_hits if hit.score >= _RELEVANCE_FLOOR]
        citations = [_to_citation(hit) for hit in hits]
        grounded_content = f"{last['content']}\n\n{_grounding_block(hits)}"
        messages[-1] = {**last, "content": grounded_content}

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
                return AgentResult(status="answer", content=_GIVEUP_MESSAGE, messages=messages, citations=citations)
            # The broken turn is NOT added to history (no assistant message,
            # no dangling tool_call) — just a corrective user turn, so the
            # model gets a clean shot at a valid call next time.
            messages.append({"role": "user", "content": _REPAIR_MESSAGE})
            continue

        repair_attempts = 0  # a successful call resets the CONSECUTIVE-failure streak
        last_content = turn.content

        if not turn.tool_calls:
            if looks_like_tablature(turn.content or ""):
                # C3: the assistant free-typed a tab instead of calling
                # generate_artifact. The bluff is NOT appended to `messages`
                # here — it must never reach the tutor, not even inside the
                # transcript history — see the module-level comment above
                # `_MAX_TAB_BLUFF_ATTEMPTS` for the recovery choice/defence.
                if tab_bluff_attempts >= _MAX_TAB_BLUFF_ATTEMPTS:
                    log.warning(
                        "run_agent_turn: model free-typed tablature again after a "
                        "re-prompt; suppressing and returning an honest fallback"
                    )
                    messages.append({"role": "assistant", "content": _TAB_BLUFF_FALLBACK_MESSAGE})
                    return AgentResult(
                        status="answer", content=_TAB_BLUFF_FALLBACK_MESSAGE,
                        messages=messages, citations=citations,
                    )
                tab_bluff_attempts += 1
                log.warning(
                    "run_agent_turn: model free-typed tablature instead of calling "
                    "generate_artifact; re-prompting once"
                )
                messages.append({"role": "user", "content": _TAB_BLUFF_REPROMPT_MESSAGE})
                continue
            messages.append(_wire_assistant_message(turn.content, turn.tool_calls))
            return AgentResult(status="answer", content=turn.content, messages=messages, citations=citations)

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
            citations=citations,
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
        return AgentResult(status="answer", content=_MAX_STEPS_MESSAGE, messages=messages, citations=citations)
    return AgentResult(status="answer", content=last_content, messages=messages, citations=citations)
