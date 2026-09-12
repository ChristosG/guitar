"""`/chat` routes (Plan 5 Task 4) — THE integration crux of the chat
copilot: session creation, turn-taking through `run_agent_turn` (Task 2),
and the HITL approve/reject/resume + async-generation compose built on
Task 3's suspend-on-mutation models, reusing Plan 8's exact enqueue/poll
pattern (`routers/curriculum.py:88-131`) for every `async_job=True` mutation
in `app.agent.tools.TOOLS` — DATA-DRIVEN off each tool's own `job_kind`
(review fix, Plan 10 Task 3; see `_ASYNC_JOB_RUNNERS`/`_ASYNC_JOB_LABELS`
below and `resolve_approval`'s async branch), not hardcoded to one tool.

This router owns ALL persistence for the chat state machine — `run_agent_turn`
itself stays session-agnostic and never writes anything (see `loop.py`'s own
module docstring); this module is what turns a session's `Message` rows into
the wire transcript the loop consumes (`app.agent.transcript.
messages_to_wire`), persists whatever new turns the loop produces
(`persist_new_messages`), and persists/resolves the `ApprovalRequest` a
suspended turn leaves behind.

Auth: every route here sits behind the `gt_session` password gate
(`app/auth/middleware.py`) — a whole-API ASGI middleware, not a per-router
dependency, so there is nothing to declare in this file. One tutor, one
password; there is still no authorization model, because there is nobody to
authorize against anybody else.
"""
import json
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.guards import CURRICULUM_CONTEXT_SENTINEL, PLANNING_CONTEXT_SENTINEL
from app.agent.handoff import FIRST_TURN_HANDOFF
from app.agent.loop import AgentResult, _stringify, run_agent_turn, stream_plain_turn
from app.agent.tools import TOOLS, with_locale
from app.agent.transcript import messages_to_wire, persist_new_messages, window_wire
from app.curriculum.revise import compact_tree_text, compute_impact, validate_ops
from app.db import get_db
from app.i18n import normalize_locale
from app.jobs.chat_turn import run_chat_turn_job
from app.jobs.curriculum_revise import run_curriculum_revise_job
from app.jobs.runner import run_curriculum_job, run_lesson_job
from app.llm.errors import LLMError
from app.llm.factory import get_provider
from app.models.block import Block
from app.models.chat import ApprovalRequest, ChatSession, Message
from app.models.generation_job import GenerationJob
from app.models.interview import CurriculumInterview
from app.prompts.overrides import resolve as resolve_text
from app.schemas.chat import (
    ApprovalResolveRequest,
    ChatMessageIn,
    ChatSessionCreate,
    ChatSessionCreated,
    ChatSessionOut,
    ChatSessionSummary,
    ChatSessionUpdate,
    ChatTurnOut,
    DistilledInstructionOut,
    MessageOut,
    PendingApprovalOut,
    SuggestionsOut,
)
from app.schemas.jobs import JobAccepted

log = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

# Human-facing label per `job_kind`, for the tool message `resolve_approval`
# records when it enqueues an async job (e.g. "Curriculum generation started
# (job <id>)."). Purely cosmetic strings — safe at module scope, unlike the
# runner lookup below (see `resolve_approval`'s own comment for why THAT one
# can't live here).
_ASYNC_JOB_LABELS: dict[str, str] = {
    "curriculum": "Curriculum generation",
    "lesson": "Lesson drafting",
    "curriculum_revise": "Curriculum revision",
}


# The roles a human ever sees. `tool` rows are internal plumbing (see
# `MessageOut`'s docstring); everything user-facing in this module — the
# transcript, the sidebar's count, its preview, its "last activity" ordering —
# filters on exactly this tuple, so all four agree by construction.
_VISIBLE_ROLES = ("user", "assistant")

# A truncation of the first user message, NOT a model-written summary (Plan 13
# Stage 5.6): a "name this conversation" call is a billed request per
# conversation, for a string the tutor can rename in one click. 60 chars is a
# sidebar-width truncation; the column holds 200.
_TITLE_MAX = 60
_PREVIEW_MAX = 120

# Suggestion chips (chat overhaul, Piece B): how many of the most-recent
# VISIBLE turns feed the one guided_json call — a handful of exchanges is
# enough context for "what's the tutor's next move", and keeping this small
# keeps the call cheap (the whole point of a call that only ever produces up
# to 3 short strings, fired non-blocking after the real answer already
# rendered). `_SUGGESTIONS_CAP` mirrors the frontend contract ("2-3 chips");
# both the prompt AND this post-hoc slice enforce it, because `guided_json`'s
# structured output does NOT enforce `maxItems` (see `llm/schema.py`'s own
# module docstring on why array-length constraints are stripped before the
# call, not trusted after it).
_SUGGESTIONS_HISTORY = 8
_SUGGESTIONS_CAP = 3

SUGGESTIONS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["suggestions"],
}


def _truncate(text: str, limit: int) -> str:
    """Whitespace-collapsed, ellipsized-if-cut. Collapsing first matters: a
    pasted multi-line message would otherwise put a newline (and a run of
    indentation) inside a sidebar row.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "…"


def _ensure_title(session: ChatSession, content: str) -> None:
    """Name a session after its first user message. Idempotent by the NULL
    check — a rename (`PATCH /chat/{id}`) is never undone by the next turn.
    Does not commit: every caller is about to, through
    `persist_new_messages`'s own commit on the same Session.
    """
    if session.title is None and content.strip():
        session.title = _truncate(content, _TITLE_MAX)


def _get_session_or_404(db: Session, session_id: UUID) -> ChatSession:
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    return session


def _ordered_messages(db: Session, session_id: UUID) -> list[Message]:
    """All of a session's `Message` rows, in transcript order. `created_at`
    (then `id` as a last-resort tiebreak) — see `app.agent.transcript.
    persist_new_messages`'s own docstring for why committing once per
    message (not once per batch) is what makes this ordering reliable.
    """
    return db.scalars(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at, Message.id)
    ).all()


def _open_pending_approval(db: Session, session_id: UUID) -> ApprovalRequest | None:
    """The session's currently-open (`status == "pending"`) `ApprovalRequest`,
    most-recent first, or None. Shared by `GET .../pending` (surfaces it) and
    `post_message`'s guard (refuses a new turn while one is open) so both
    read "is there an open approval" identically — a chat turn's suspend
    leaves exactly one unanswered mutation tool_call, so there is at most one
    pending approval per session at a time in practice; `.first()` on a
    most-recent ordering is defensive regardless.
    """
    return db.scalars(
        select(ApprovalRequest)
        .where(ApprovalRequest.session_id == session_id, ApprovalRequest.status == "pending")
        .order_by(ApprovalRequest.created_at.desc())
    ).first()


def _new_tail(result_messages: list[dict], prior_wire: list[dict]) -> list[dict]:
    """The part of `result_messages` (an `AgentResult.messages`) genuinely
    NEW relative to `prior_wire` (what was actually fed into `run_agent_turn`)
    — i.e. everything the loop appended this call.

    `run_agent_turn` prepends `SYSTEM_PROMPT` (`_ensure_system_prompt`)
    whenever the caller's own list doesn't already start with one — which
    `prior_wire` never does here (`messages_to_wire` never emits a system
    entry; the system prompt is never persisted). A naive
    `result_messages[len(prior_wire):]` would therefore be off by exactly
    that prepended message and re-include `prior_wire`'s OWN last entry.
    Stripping a leading system message first, before slicing, avoids
    persisting that entry a second time.
    """
    messages = result_messages
    if messages and messages[0].get("role") == "system":
        messages = messages[1:]
    return messages[len(prior_wire):]


_RESULT_REF_MAX = 255  # ApprovalRequest.result_ref is String(255)


def _result_ref(tool_result) -> str | None:
    """Best-effort opaque pointer for `ApprovalRequest.result_ref` on a sync
    mutation's success (`app.models.chat.ApprovalRequest.result_ref` is
    deliberately untyped — "a created row's id" is just the common case, not
    a contract every tool must satisfy). Every current sync mutation's `fn`
    returns a dict (the student/note tools this docstring used to enumerate
    left with the desktop build — see tools.py's module docstring):
    `update_block`/`generate_artifact`/`merge_sessions`/`add_session` carry
    `"id"`; `segment_block` has no single id, only `"session_ids"`;
    `split_session` returns a `"sessions"` list, which lands in the
    catch-all below. Anything else (or a non-dict result) falls
    back to `None` rather than raising — this is bookkeeping, not a
    correctness-critical value.

    The `session_ids` case records a BOUNDED COUNT summary, never a join of
    every id: `segment_block` commonly yields many sessions (a 3.5h
    curriculum at 30min/session = 7 ids × 36-char UUID + separators = 264
    chars), which would overflow the `String(255)` column and raise
    `DataError` on commit — and that commit happens in `persist_new_messages`
    OUTSIDE the resolve endpoint's fn-error try/except (the fn already
    succeeded), so the overflow would surface as a raw 500 AND roll back the
    approval status-flip + tool-result persist, bricking the session. The
    full ids are already in the tool-message `content`; a count is all this
    bookkeeping pointer needs. Every return is clamped to `_RESULT_REF_MAX`
    as a defensive backstop so NO branch can ever overflow the column
    (`id`/`assign_curriculum`'s UUID is only 36 chars, but the clamp is free
    insurance against a future tool returning a longer `"id"`).
    """
    if not isinstance(tool_result, dict):
        return None
    if "id" in tool_result:
        ref = str(tool_result["id"])
    elif "session_ids" in tool_result:
        ref = f"{len(tool_result['session_ids'])} sessions"
    else:
        return None
    return ref[:_RESULT_REF_MAX]


def _default_description(tool_name: str, tool_args: dict) -> str:
    """Fallback `description` for an `awaiting_approval` response when the
    model gave no narration (`AgentResult.content is None` — a real,
    documented case; Task 3's own tests pin exactly this for the suspend
    path). Just enough for a human to recognize what they're being asked to
    approve without the UI needing its own per-tool copy (Task 5's concern).
    """
    return f"Proposed action: {tool_name} with arguments {tool_args}"


def _inject_curriculum_context(db: Session, session: ChatSession, wire: list[dict]) -> list[dict]:
    """Bind the turn to the session's curriculum WITHOUT touching the cached
    system+tools prefix or the persisted transcript: append a compact tree +
    brief to the LAST user message of the transient `wire` (the same tail
    position the forced-retrieval grounding block already uses — never the
    cached prefix). Re-injected EVERY turn so the model always sees the CURRENT
    tree (e.g. right after an apply), and only when the session is bound to a
    curriculum (`root_id` set) — the ordinary global chat gets nothing.

    Returns a NEW list with a NEW dict for the one message it edits; it never
    mutates `wire`'s dicts in place (they alias the persisted transcript's wire
    shape), so the context is transient by construction — it is never written to
    the `message` table and never re-cached."""
    if not session.root_id:
        return wire
    course = db.get(Block, session.root_id)
    if course is None or course.kind != "course":
        return wire
    brief = (course.meta or {}).get("brief") or ""
    ctx = (
        f"\n\n{CURRICULUM_CONTEXT_SENTINEL} — this conversation is about curriculum "
        f"{course.id} titled \"{course.title}\". ANSWER QUESTIONS ABOUT IT (what a "
        f"lesson covers, how it is structured, whether a topic is included) "
        f"DIRECTLY from the structure below — use find_lesson or search_knowledge "
        f"for detail, but do NOT run propose_curriculum_revision just to answer a "
        f"question. Call propose_curriculum_revision ONLY when the tutor explicitly "
        f"asks to ADD, CHANGE, REMOVE, or RESTRUCTURE the course, then "
        f"apply_curriculum_revision (with this root_id) to apply an approved plan."
        + (f" Brief: {brief}." if brief else "")
        + f"\nCurrent structure:\n{compact_tree_text(db, course)}]"
    )
    wire = list(wire)
    for i in range(len(wire) - 1, -1, -1):
        if wire[i].get("role") == "user":
            wire[i] = {**wire[i], "content": (wire[i].get("content") or "") + ctx}
            break
    return wire


def _inject_interview_context(db: Session, session: ChatSession, wire: list[dict]) -> list[dict]:
    """Part 5: the planning chat's light steer — same transient tail-append
    contract as `_inject_curriculum_context` above (new list, new dict, never
    persisted, re-applied every turn), but deliberately MINIMAL: a title and a
    role, no tree (nothing is materialized yet) and no revise-tool steering
    (there is no root_id to revise)."""
    if session.root_id:
        # a session bound to BOTH a curriculum and an interview must get only
        # the curriculum context, never two contradictory steers
        return wire
    if not session.interview_id:
        return wire
    interview = db.get(CurriculumInterview, session.interview_id)
    if interview is None:
        return wire
    ctx = (
        f"\n\n{PLANNING_CONTEXT_SENTINEL} — the tutor is planning a NEW course "
        f"titled \"{interview.title}\" that does not exist yet. Help him think "
        f"it through: goals, topics, emphasis, sequencing, what to avoid. "
        f"Ground answers in his library where relevant. Do NOT call "
        f"propose_curriculum_revision or apply_curriculum_revision — there is "
        f"no curriculum to revise yet. Do NOT call generate_curriculum either "
        f"— the tutor will generate the course through the wizard after this "
        f"chat, not from here.]"
    )
    wire = list(wire)
    for i in range(len(wire) - 1, -1, -1):
        if wire[i].get("role") == "user":
            wire[i] = {**wire[i], "content": (wire[i].get("content") or "") + ctx}
            break
    return wire


# SYSTEM PROMPT for the suggestion-chips call (chat overhaul, Piece B) —
# constrained HARD, on purpose. `claude -p` cannot reliably emit inline
# structured suggestions inside free-form prose (the solidity decision this
# endpoint exists to satisfy), so this is a SEPARATE one-shot classification
# call, and the one thing worse than no chip is a chip that reads like
# filler or asks for something the app cannot do — "the composer is always
# primary, chips are optional shortcuts, never a cage" only holds if every
# chip is a REAL, DOABLE action.
#
# EDITABLE, like every other prompt in this app (`app/prompts/registry.py`'s
# `chat.suggestions` entry) — `{lang}`/`{course_context}` are filled in by
# `_suggestions_system_prompt` below via `.format()`, exactly the way
# `agent/loop.py`'s `GROUNDING_BLOCK`/`_grounding_block` fill in `{passages}`/
# `{answer_in}`: the tutor's own edit is the TEMPLATE, not a full replacement
# of the whole rendered string, so he cannot accidentally delete the
# placeholders his own suggestions depend on.
SUGGESTIONS_SYSTEM = (
    "You read a tutoring-copilot conversation and suggest the tutor's NEXT "
    "MOVE, in {lang}.\n"
    "Propose UP TO 3 short, concrete, DOABLE next actions: a specific "
    "question about this exact topic, or — only if a curriculum is bound "
    "to this conversation (see below) — a specific revision this app can "
    "actually carry out (add/remove/reorder a lesson or module, change "
    "its focus, adjust its length or level).\n"
    "Each suggestion is a short imperative sentence, at most 8 words, "
    "written as if the TUTOR is about to type it himself.\n"
    'NEVER vague filler ("tell me more", "explain further"). NEVER '
    "propose an action the app cannot perform. If nothing concrete "
    "applies, return an empty list — an empty list is a correct answer, "
    "not a failure.\n"
    "Return ONLY the JSON object the schema describes."
    "{course_context}"
)
SUGGESTIONS_SLICE_ID = "chat.suggestions"


def _suggestions_system_prompt(db: Session, session: ChatSession) -> str:
    """Fills `SUGGESTIONS_SYSTEM` (tutor-editable via `resolve_text`) with the
    two things only this CALL knows: the language, and — curriculum-aware,
    when the session is bound to one (`ChatSession.root_id`, same field
    `_inject_curriculum_context` reads) — the bound course's own title, so the
    revise drawer's suggestions are scoped to THAT course rather than generic
    guitar chat. Mirrors `_inject_curriculum_context`'s own "only inject when
    bound" gate, though this is a much lighter touch (a title, not the whole
    compact tree) since this call only needs to steer wording, not ground an
    actual revision plan.
    """
    lang = "Greek" if session.locale == "el" else "English"
    course_context = ""
    if session.root_id:
        course = db.get(Block, session.root_id)
        if course is not None and course.kind == "course":
            course_context = (
                f'\nThis conversation is about revising the curriculum "'
                f'{course.title}" — every suggestion must fit revising or '
                f"asking about THIS curriculum specifically, not a generic "
                f"guitar topic."
            )
    return resolve_text(db, SUGGESTIONS_SLICE_ID, SUGGESTIONS_SYSTEM).format(
        lang=lang, course_context=course_context,
    )


def _suggestions_transcript(messages: list[Message]) -> str:
    """The last `_SUGGESTIONS_HISTORY` VISIBLE turns, as plain `ROLE:
    content` lines — NOT the OpenAI wire shape `messages_to_wire` builds.
    This is a single one-shot classification prompt the model never replies
    to in character, so a flat transcript is honestly what it is, rather than
    dressing it up as a conversation this call will continue.
    """
    recent = messages[-_SUGGESTIONS_HISTORY:]
    return "\n".join(f"{m.role.upper()}: {m.content}" for m in recent)


def _validate_pending_revision(db: Session, pending: dict) -> None:
    """APPROVED == APPLIED, EXACT (controller, 2026-07-18). `apply_curriculum_
    revision` is `async_job=True`, so the loop SUSPENDS without calling its fn —
    the plan the model re-emitted would otherwise reach the approval card (and
    then apply) UN-validated. Validate it HERE, in place on `pending`, before the
    `ApprovalRequest` is stored: replace the plan with the validated one so an op
    an id-validation would drop never renders on the card or reaches apply.
    Apply-time re-validation (`apply_revision`) stays as defence-in-depth, a
    no-op now that the stored plan is already clean.

    Best-effort: a malformed/absent root_id or plan is left untouched (the runner
    and `apply_revision` re-validate regardless) rather than raised — a garbled
    proposal must degrade to a harmless card, not a 500 on the turn.

    Also attaches `plan["impact"]` — `compute_impact`'s pure, server-computed
    blast-radius summary of the VALIDATED ops (never the model's own claim about
    what it did). It rides the same stored/returned plan the card and apply see,
    so the tutor-facing UI (Task 5) never has to trust the model's `reason` text
    for how big a change actually is."""
    if pending.get("name") != "apply_curriculum_revision":
        return
    args = pending.get("arguments") or {}
    plan = args.get("plan")
    raw_root = args.get("root_id")
    if not raw_root or not isinstance(plan, dict):
        return
    try:
        root_id = UUID(str(raw_root))
    except (ValueError, TypeError):
        return
    try:
        validated = validate_ops(db, root_id, plan)
    except Exception:
        log.warning("revise: could not pre-validate a proposed plan for the "
                    "approval card (root_id=%s)", raw_root, exc_info=True)
        return
    validated = {**validated, "impact": compute_impact(validated["ops"])}
    pending["arguments"] = {**args, "plan": validated}


def _revalidate_revision_args_for_resolve(db: Session, args: dict) -> dict:
    """Defence-in-depth for `resolve_approval`'s `apply_curriculum_revision`
    branch (whole-branch review finding #2). The normal propose -> approve
    round trip is already clean by the time it gets here: `_validate_pending_
    revision` validated the plan before the `ApprovalRequest` was even stored.
    But `edited_args` (a tutor's edited JSON on the approval card, or any
    direct caller of this endpoint) REPLACES `tool_args` WHOLESALE and skips
    that gate entirely — and `apply_revision`'s segment branches trust
    `segment_id`/`section_key` at face value, they don't re-check root
    membership (a `remove_segment` could otherwise delete ANY block id, not
    just one under this course). Mirrors `_validate_pending_revision`: run
    `validate_ops`, then recompute `impact` on the validated result, so the
    plan that actually gets enqueued is the one this call validated, not the
    one the client sent.

    Malformed/absent root_id or plan is left untouched (best-effort, same as
    `_validate_pending_revision`) — `apply_revision` re-validates ids again
    regardless, so that path is never a smuggling route. What IS enforced
    here: if the incoming plan had ops and validation drops every one of
    them, this raises 422 rather than silently enqueueing a no-op (or worse,
    quietly waving through whatever DID resolve while masking that the
    edit's real intent — e.g. that cross-course `remove_segment` — got
    dropped without the caller ever finding out)."""
    plan = args.get("plan")
    raw_root = args.get("root_id")
    if not raw_root or not isinstance(plan, dict):
        return args
    try:
        root_id = UUID(str(raw_root))
    except (ValueError, TypeError):
        return args
    original_ops = plan.get("ops") or []
    try:
        validated = validate_ops(db, root_id, plan)
    except Exception:
        log.warning("revise: could not re-validate a revision plan at resolve "
                    "time (root_id=%s)", raw_root, exc_info=True)
        return args
    if original_ops and not validated["ops"]:
        raise HTTPException(
            status_code=422,
            detail="revision plan failed validation: every op was dropped",
        )
    validated = {**validated, "impact": compute_impact(validated["ops"])}
    return {**args, "plan": validated}


_PROPOSE_TOOL = "propose_curriculum_revision"
_APPLY_TOOL = "apply_curriculum_revision"


def _wire_call_arguments(call: dict) -> dict:
    """One wire-shape tool_call's `arguments` as a dict. The OpenAI wire shape
    says JSON STRING (`loop._wire_assistant_message` always writes one, and
    `claude_cli.py` `json.loads`es one back), but a persisted row round-tripped
    through `sa.JSON` — or a test fixture — can just as well hold the dict
    itself, so both are accepted, exactly as `loop.py` tolerates both. Anything
    unparseable degrades to `{}`; this is a best-effort recovery path, never a
    place to raise."""
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _synthesize_apply_from_plan(tail: list[dict]) -> dict | None:
    """THE COMPUTED PLAN ALWAYS BECOMES A CARD (silent failure S5, 2026-09-12).

    Live run, session 97f38a82: `propose_curriculum_revision` ran for 307 s and
    returned 7 `edit_segment` ops — and the model's final hop NARRATED the plan
    («Ετοίμασα το πλάνο αναθεώρησης — …») instead of calling
    `apply_curriculum_revision`. The turn ended `answer`, `GET .../pending`
    returned null, and the tutor was left reading a description of work he had
    no way to apply: five minutes of planner time, and a dead end.

    Given a turn's NEW tail, this returns the `pending_tool` dict the model
    SHOULD have produced — `{tool_call_id, name, arguments}`, the exact shape
    `AgentResult.pending_tool` carries — or None when there is nothing to
    recover. The plan is taken verbatim from the tool RESULT (the server's own
    validated planner output), never re-derived from the model's prose, and
    `root_id`/`scope_module_id` come from the propose call's own arguments, so
    a module-scoped proposal cannot silently widen into a whole-course apply.

    Returns None — i.e. leaves an ordinary answer alone — when:
      * no tool result in this tail answers a `propose_curriculum_revision`
        call made in this same tail (nothing was computed this turn);
      * the result is not parseable JSON (`loop._stringify` caps a tool result
        at `TOOL_RESULT_MAX_CHARS`, and a cut result is no longer valid JSON —
        better a plain answer than a card built on a guess);
      * the parsed result has no `ops` (an empty plan, or the planner's
        graceful `{"error": ...}` shape) or carries no `root_id`.
    """
    proposals: dict[str, dict] = {}          # propose tool_call_id -> its arguments
    found: tuple[str, dict] | None = None    # (tool result content, propose arguments)
    for message in tail:
        role = message.get("role")
        if role == "assistant":
            for call in (message.get("tool_calls") or []):
                if (call.get("function") or {}).get("name") == _PROPOSE_TOOL and call.get("id"):
                    proposals[call["id"]] = _wire_call_arguments(call)
        elif role == "tool":
            propose_args = proposals.get(message.get("tool_call_id"))
            if propose_args is not None:
                # Keep walking: the LAST plan of the turn is the live one.
                found = (message.get("content") or "", propose_args)
    if found is None:
        return None

    content, propose_args = found
    try:
        plan = json.loads(content)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(plan, dict) or not plan.get("ops"):
        return None
    root_id = propose_args.get("root_id")
    if not root_id:
        return None

    arguments = {"root_id": root_id, "plan": plan}
    scope_module_id = propose_args.get("scope_module_id")
    if scope_module_id:
        arguments["scope_module_id"] = scope_module_id
    return {
        "tool_call_id": f"synth-{uuid4().hex[:12]}",
        "name": _APPLY_TOOL,
        "arguments": arguments,
    }


def _tail_with_synthetic_call(tail: list[dict], pending: dict, content: str | None) -> list[dict]:
    """`tail` with `pending` hung off its LAST assistant row as a wire-shape
    tool_call, so the persisted transcript ends in exactly the shape a
    model-emitted suspend leaves behind: an assistant `tool_calls` row that
    `resolve_approval` later answers with the matching `role="tool"` result.
    Without this the wire rebuilt from the transcript would carry an approval
    nothing ever asked for — and with it, no dangling call survives a resolve.

    Returns a NEW list with a NEW dict for the one row it changes (`tail`'s own
    dicts are `AgentResult.messages` entries the caller still holds). A tail
    that does not end on an assistant row (the `max_steps` degenerate shape)
    gets a fresh assistant row carrying the narration instead."""
    call = {
        "id": pending["tool_call_id"],
        "type": "function",
        "function": {
            "name": pending["name"],
            "arguments": json.dumps(pending["arguments"], ensure_ascii=False, default=str),
        },
    }
    if tail and tail[-1].get("role") == "assistant":
        return [*tail[:-1], {**tail[-1], "content": content, "tool_calls": [call]}]
    return [*tail, {"role": "assistant", "content": content, "tool_calls": [call]}]


def _respond_to_turn(db: Session, session_id: UUID, prior_wire: list[dict], result: AgentResult) -> ChatTurnOut:
    """Shared response-shaping for every call site that runs (or resumes)
    `run_agent_turn` and must react to its outcome: the very first turn
    (`POST .../messages`) AND resuming after a resolve (both reject and a
    sync approve). Persists the newly appended tail, then either surfaces a
    NEW `awaiting_approval` (creating its own `ApprovalRequest`, exactly like
    the original suspend) or a plain answer.

    Handling `awaiting_approval` here too (not just in the first-turn route)
    matters: a RESUMED turn can itself immediately propose ANOTHER mutation
    (e.g. the model tries a different action right after a rejection) —
    this reacts to that the same correct way as the original suspend rather
    than silently discarding that new pending call's trackability (it would
    otherwise persist as an unanswered tool_call with no `ApprovalRequest`
    row and no way to ever resolve it via `GET .../pending`).

    A COMPUTED PLAN ALWAYS BECOMES A CARD (task 0.7, silent failure S5):
    an `answer` turn whose tail carries a `propose_curriculum_revision` result
    with real `ops` gets the `apply_curriculum_revision` call the model
    forgot to make SYNTHESIZED here (`_synthesize_apply_from_plan`) and hung
    off the tail's last assistant row (`_tail_with_synthetic_call`), so the
    turn ends `awaiting_approval` with a real `ApprovalRequest` instead of a
    paragraph about work the tutor cannot apply. Being here — and not in
    `run_agent_turn` — is the point: all three doors into a turn (sync
    `post_message`, the `chat_turn` job via `run_turn_core`, and
    resume-after-resolve) pass through this one function.
    """
    tail = _new_tail(result.messages, prior_wire)

    pending: dict | None = None
    if result.status == "awaiting_approval":
        pending = result.pending_tool
        # Approved == applied, EXACT: validate an apply_curriculum_revision plan
        # (in place on `pending`) BEFORE it is stored/rendered — a dropped op must
        # never reach the card. No-op for every other tool.
        _validate_pending_revision(db, pending)
    elif result.status == "answer":
        # S5: the model computed a plan and then only TALKED about it. Recover
        # the apply call it omitted (None for every turn that didn't compute a
        # plan, i.e. almost all of them — the pure, DB-free scan runs first so
        # the ordinary answer turn pays nothing but a walk of its own tail).
        # Skipped outright while an approval is already open: one unanswered
        # mutation call at a time is the whole protocol `post_message`'s 409
        # guard and `_open_pending_approval` defend; a second would strand both.
        synthesized = _synthesize_apply_from_plan(tail)
        if synthesized is not None and _open_pending_approval(db, session_id) is None:
            # Same approved==applied gate as the suspend path above — run
            # BEFORE the call is written into the transcript, so the persisted
            # arguments ARE the validated plan the card and the job will see.
            _validate_pending_revision(db, synthesized)
            ops = (synthesized["arguments"].get("plan") or {}).get("ops") or []
            if ops:
                pending = synthesized
                tail = _tail_with_synthetic_call(tail, pending, result.content)
                log.info(
                    "chat: synthesized apply_curriculum_revision card for session "
                    "%s (%d ops) — the model narrated instead of calling it",
                    session_id, len(ops),
                )
            # Validation emptying the plan lands here: nothing left to approve,
            # so this stays the plain answer it already was (same rule as an
            # empty `ops` straight off the planner).

    persist_new_messages(db, session_id, tail, citations=result.citations)

    if pending is not None:
        approval = ApprovalRequest(
            session_id=session_id,
            tool_name=pending["name"],
            tool_args=pending["arguments"],
            tool_call_id=pending["tool_call_id"],
            status="pending",
        )
        db.add(approval)
        db.commit()
        description = result.content or _default_description(pending["name"], pending["arguments"])
        return ChatTurnOut(
            status="awaiting_approval",
            approval_id=approval.id,
            tool_name=pending["name"],
            tool_args=pending["arguments"],
            description=description,
            citations=result.citations or None,
        )

    return ChatTurnOut(status="answer", content=result.content, citations=result.citations or None)


@router.post("/chat", response_model=ChatSessionCreated)
def create_chat_session(payload: ChatSessionCreate, db: Session = Depends(get_db)) -> ChatSessionCreated:
    """`locale` is normalized (`el-GR` -> `el`, unknown -> `el`) BEFORE it is
    stored, not when it is read: this column is the single source of truth for
    the language of every turn, every proposed tool call and every job this
    conversation ever enqueues (see `run_agent_turn`'s `locale`), and it is
    read from four places. Normalizing once, at the boundary, means none of
    them can disagree.
    """
    session = ChatSession(
        student_id=payload.student_id,
        locale=normalize_locale(payload.locale),
        root_id=payload.root_id,
    )
    db.add(session)
    db.commit()
    return ChatSessionCreated(session_id=session.id)


@router.get("/chat", response_model=list[ChatSessionSummary])
def list_chat_sessions(db: Session = Depends(get_db)) -> list[ChatSessionSummary]:
    """The sidebar's list (Plan 13 Stage 5.6), most-recently-active first.

    Sessions with NO user/assistant message are EXCLUDED — and that exclusion
    is the INNER JOIN itself, not a filter bolted on after it. It has to be:
    the UI this replaces created a session on every single mount of the chat
    page and kept its id in React state only, so the deployed database holds a
    pile of orphaned empty sessions, and the new UI still creates one the
    moment the tutor clicks "new chat" and then walks away. Neither is a
    conversation. Listing them would bury the real transcripts under blanks.

    Ordered by LAST ACTIVITY, not by `created_at`: resuming a week-old thread
    and adding to it should float it to the top, which is what a chat sidebar
    means by "recent". `updated_at` on the session row would NOT do this —
    nothing in the message path touches the parent row (see `_ensure_title`,
    the one exception, and only on the first turn).

    Two queries, deliberately, and neither one is per-session (no N+1): one
    aggregate for count + last-activity, one Postgres `DISTINCT ON` for the
    preview text. The preview cannot come out of the aggregate — SQL has no
    "the value from the max row" aggregate — and a correlated subquery per row
    is the same N+1 in a costume.
    """
    visible = Message.role.in_(_VISIBLE_ROLES)

    agg = (
        select(
            Message.session_id.label("session_id"),
            func.count(Message.id).label("message_count"),
            func.max(Message.created_at).label("last_message_at"),
        )
        .where(visible)
        .group_by(Message.session_id)
        .subquery()
    )
    rows = db.execute(
        select(ChatSession, agg.c.message_count, agg.c.last_message_at)
        .join(agg, agg.c.session_id == ChatSession.id)
        .order_by(agg.c.last_message_at.desc())
    ).all()
    if not rows:
        return []

    # DISTINCT ON (session_id) + ORDER BY session_id, created_at DESC = "the
    # newest visible message per session". `content IS NOT NULL` skips a
    # tool-calls-only assistant turn (`Message.content` is nullable — a
    # suspended mutation's narration can be absent entirely), which would
    # otherwise win the ordering and give the session a blank preview.
    session_ids = [session.id for session, _, _ in rows]
    previews = {
        session_id: _truncate(content, _PREVIEW_MAX)
        for session_id, content in db.execute(
            select(Message.session_id, Message.content)
            .where(
                Message.session_id.in_(session_ids),
                visible,
                Message.content.is_not(None),
            )
            .distinct(Message.session_id)
            .order_by(Message.session_id, Message.created_at.desc(), Message.id.desc())
        ).all()
    }

    return [
        ChatSessionSummary(
            id=session.id,
            title=session.title,
            locale=session.locale,
            student_id=session.student_id,
            created_at=session.created_at,
            updated_at=session.updated_at,
            message_count=message_count,
            last_message_at=last_message_at,
            preview=previews.get(session.id),
        )
        for session, message_count, last_message_at in rows
    ]


@router.patch("/chat/{session_id}", response_model=ChatSessionOut)
def rename_chat_session(
    session_id: UUID, payload: ChatSessionUpdate, db: Session = Depends(get_db)
) -> ChatSession:
    session = _get_session_or_404(db, session_id)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title must not be blank")
    session.title = title[:200]
    db.commit()
    db.refresh(session)
    return session


@router.delete("/chat/{session_id}", status_code=204)
def delete_chat_session(session_id: UUID, db: Session = Depends(get_db)) -> Response:
    """Deletes the transcript AND its approval records — the `ON DELETE
    CASCADE` on `message.session_id`/`approval_request.session_id` (migration
    `8fa57b72fb21`) does that in the database, so this is one DELETE, not a
    hand-rolled three-table teardown that could half-fail.

    A pending approval is destroyed with the rest of the session. That is the
    honest semantic: the mutation it gated was never executed (nothing outside
    these three tables was written), so there is nothing left dangling — and
    a tutor deleting the conversation is, unambiguously, not going to approve
    what it proposed.
    """
    session = _get_session_or_404(db, session_id)
    db.delete(session)
    db.commit()
    return Response(status_code=204)


@router.get("/chat/{session_id}", response_model=list[MessageOut])
def get_chat_history(session_id: UUID, db: Session = Depends(get_db)) -> list[Message]:
    _get_session_or_404(db, session_id)
    rows = _ordered_messages(db, session_id)
    return [row for row in rows if row.role in _VISIBLE_ROLES]


@router.get("/chat/{session_id}/pending", response_model=PendingApprovalOut | None)
def get_pending_approval(session_id: UUID, db: Session = Depends(get_db)) -> PendingApprovalOut | None:
    _get_session_or_404(db, session_id)
    approval = _open_pending_approval(db, session_id)
    if approval is None:
        return None
    return PendingApprovalOut.model_validate(approval, from_attributes=True)


def has_running_turn(db: Session, session_id: UUID) -> bool:
    """A `chat_turn` job for this session that has not finished. Same reason
    `_open_pending_approval` guards a new turn: two turns interleaving on one
    transcript is an out-of-protocol shape that gets PERSISTED."""
    return db.scalar(
        select(GenerationJob.id).where(
            GenerationJob.kind == "chat_turn",
            GenerationJob.status.in_(("pending", "running")),
            GenerationJob.params["session_id"].as_string() == str(session_id),
        ).limit(1)
    ) is not None


def run_turn_core(db: Session, session: ChatSession, content: str, *, precomputed=None) -> ChatTurnOut:
    """Everything a turn does AFTER its user row is persisted: window, inject,
    run the loop, persist the tail, open an approval if the loop suspended.
    Shared verbatim by the synchronous `post_message` and the `chat_turn` job
    (`app/jobs/chat_turn.py`) — the job is the same turn off the request path,
    so this is the one place a turn is defined. Raises `LLMError` through."""
    wire = window_wire(messages_to_wire(_ordered_messages(db, session.id)))
    wire = _inject_curriculum_context(db, session, wire)
    wire = _inject_interview_context(db, session, wire)
    result = run_agent_turn(
        db, wire, locale=session.locale, raw_user_text=content,
        precomputed_first=precomputed,
    )
    return _respond_to_turn(db, session.id, wire, result)


@router.post("/chat/{session_id}/messages", response_model=ChatTurnOut | JobAccepted)
def post_message(
    session_id: UUID,
    payload: ChatMessageIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    async_: bool = Query(False, alias="async"),
):
    session = _get_session_or_404(db, session_id)

    # Refuse a new turn while an approval is still open (409, same vocabulary
    # as the resolve endpoint's non-pending 409). After an `awaiting_approval`
    # turn the transcript ends with an UNANSWERED assistant tool-calls turn
    # (the suspended mutation); persisting a new `user` row after it and
    # handing `...assistant(tool_calls), user(new)` to `chat_tools` is an
    # out-of-protocol shape (an assistant tool-calls turn must be answered by
    # its `tool` messages before any user turn) that vLLM would 500 or
    # silently degrade on — and whatever it returned would then get
    # PERSISTED, durably corrupting the transcript (the mutation call stranded
    # unanswered forever). The user must resolve the pending approval first.
    # Checked BEFORE persisting the user message, so a refused message never
    # lands in history at all.
    if _open_pending_approval(db, session_id) is not None:
        # A refused turn also invalidates any stashed streamed first response
        # for this session (CORE_DECISIONS.md §3 handoff): the designed
        # protocol is stream-fallback → IMMEDIATE resend, so an approval
        # appearing in between means the transcript moved and whatever was
        # stashed is contextually stale — it must not survive to replay
        # against a transcript it was never computed for.
        FIRST_TURN_HANDOFF.discard(session_id)
        raise HTTPException(
            status_code=409,
            detail="an approval is pending — resolve it before sending a new message",
        )

    # And refuse while the PREVIOUS turn of this session is still being
    # answered off the request path (a `chat_turn` job). Same corruption the
    # approval guard above prevents, by the other door: the running job will
    # window the transcript and persist its own tail, so a second turn started
    # against the same history would interleave two answers on one transcript.
    # Checked before the user row is persisted, so a refused message never
    # lands in history — and checked for the SYNC path too, because "which
    # door the second message came through" doesn't change the corruption.
    if has_running_turn(db, session_id):
        raise HTTPException(
            status_code=409,
            detail={"code": "turn_running",
                    "message": "the previous message is still being answered"},
        )

    # Flushed by `persist_new_messages`'s own commit, on the same Session.
    _ensure_title(session, payload.content)
    persist_new_messages(db, session_id, [{"role": "user", "content": payload.content}])

    if async_:
        # THE DRAWER'S DOOR. A revise turn runs the planner inline and can take
        # minutes under claude -p; every proxy in front of this process cuts a
        # request long before that. Off the request path, nothing can cut it.
        # The stashed streamed first response is discarded rather than handed
        # to the job: the handoff is a same-request, immediate-resend protocol
        # (CORE_DECISIONS.md §3), and this turn is neither.
        FIRST_TURN_HANDOFF.discard(session_id)
        job = GenerationJob(
            kind="chat_turn", status="pending",
            params={"session_id": str(session_id), "content": payload.content},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        background_tasks.add_task(run_chat_turn_job, job.id)
        return JSONResponse(
            status_code=202,
            content=JobAccepted(job_id=job.id, status=job.status).model_dump(mode="json"),
        )

    # CORE_DECISIONS.md §3 (the tool-turn double-billing fix): if this very
    # turn already ran its first model call on the STREAMING endpoint and fell
    # back here, claim that response and let the loop consume it instead of
    # re-billing the identical call. `claim` is atomic and single-use, and
    # returns None unless this session's stash is unexpired AND was produced
    # by exactly this `content` — every miss simply pays for a fresh call,
    # which is yesterday's (correct) behavior. One documented edge stays: if
    # a LATER hop of this turn dies (e.g. a 429 below), the claimed first
    # response is already consumed, so the tutor's retry pays for a fresh
    # first call — acceptable, because re-stashing on failure would risk
    # replaying against a transcript the failed attempt half-advanced.
    precomputed = FIRST_TURN_HANDOFF.claim(session_id, payload.content)
    try:
        return run_turn_core(db, session, payload.content, precomputed=precomputed)
    except LLMError as e:
        # A provider failure mid-turn used to escape as a raw 500 — "Internal
        # Server Error" in a non-technical user's browser for a 429 he only
        # needed to wait out. The user's message row is already persisted
        # (above), so after the wait his Send simply retries the turn. The
        # status codes match the taxonomy the frontend already translates.
        if e.kind == "too_long":
            raise HTTPException(
                status_code=413,
                detail={"code": "conversation_too_long",
                        "message": "the conversation is too large for the model — start a new chat"},
            ) from e
        status = {"rate_limit": 429, "auth": 409, "timeout": 504}.get(e.kind, 502)
        raise HTTPException(
            status_code=status,
            detail=str(e) or f"the model provider failed ({e.kind}) — try again",
        ) from e


@router.post("/chat/{session_id}/messages/stream")
def post_message_stream(session_id: UUID, payload: ChatMessageIn, db: Session = Depends(get_db)) -> StreamingResponse:
    """SSE token streaming for the plain-answer path (Plan 11 Task 3, C4) —
    Chris's second complaint ("no stream"): tokens now appear as the model
    writes them instead of after ~15s of nothing. A SEPARATE endpoint from
    `post_message` above, which is UNCHANGED and stays the single source of
    truth for every turn this one can't handle — see `app.agent.loop.
    stream_plain_turn`'s own module-level comment for the full "honest
    simplification" rationale: only a plain, tool-free, tablature-free
    answer ever streams here; a tool/mutation call, a C3 guard trip, or any
    error yields an SSE `fallback` event and this endpoint persists NOTHING
    for the turn — the browser is expected to resend the same `content`
    through the existing `POST .../messages` for an authoritative response
    (the full ReAct loop, the HITL suspend gate, and the C3 re-prompt all
    still apply there, unchanged). `apps/web/src/lib/api.ts`'s
    `streamChatMessage` is the one caller that drives this contract.

    Same 409 "an approval is pending" guard as `post_message`, checked the
    same way and for the same reason (see that route's own comment) — both
    endpoints gate a new turn on an open approval identically; this one is
    not a side door around that guard.

    Persistence happens ONLY once the underlying `stream_plain_turn`
    generator reaches its `"done"` event, and then ATOMICALLY from the
    caller's perspective — the user turn (`payload.content`, verbatim — same
    "persist the RAW text, ground only in memory" contract `post_message`
    follows) and the model's final assistant turn are written in the SAME
    `persist_new_messages` call. Nothing is ever written on a `"fallback"`,
    which is exactly what makes resending via REST always safe: there is
    never a stray persisted user message left with no answer for the loop to
    choke on next turn.
    """
    session = _get_session_or_404(db, session_id)

    # Same guard, same reasoning, as `post_message` above.
    if _open_pending_approval(db, session_id) is not None:
        raise HTTPException(
            status_code=409,
            detail="an approval is pending — resolve it before sending a new message",
        )

    # And the same `chat_turn`-job guard, for the same reason: the streaming
    # door must not be the side door around it. A running job will window this
    # transcript and persist its own tail; a stream started against the same
    # history would interleave two answers on one transcript — and this
    # endpoint DOES persist (atomically, on `"done"`). Refused before anything
    # is written, so a blocked message leaves no trace.
    if has_running_turn(db, session_id):
        raise HTTPException(
            status_code=409,
            detail={"code": "turn_running",
                    "message": "the previous message is still being answered"},
        )

    prior_wire = window_wire(messages_to_wire(_ordered_messages(db, session_id)))
    user_wire = {"role": "user", "content": payload.content}
    wire = _inject_curriculum_context(db, session, prior_wire + [user_wire])
    wire = _inject_interview_context(db, session, wire)

    def event_stream():
        try:
            for event in stream_plain_turn(db, wire, locale=session.locale, raw_user_text=payload.content):
                if event["event"] == "delta":
                    yield f"event: delta\ndata: {json.dumps({'text': event['text']})}\n\n"
                elif event["event"] == "fallback":
                    # CORE_DECISIONS.md §3: a "tool_call"/"tablature" fallback
                    # carries the complete, ALREADY-BILLED first response
                    # (`event["turn"]`, server-internal — see
                    # `stream_plain_turn`'s docstring). Stash it so the REST
                    # resend the client is about to make can consume it
                    # instead of re-billing the identical model call. The SSE
                    # payload the browser sees is unchanged: `{"reason"}`
                    # only, exactly as before.
                    if event.get("turn") is not None:
                        FIRST_TURN_HANDOFF.stash(session_id, payload.content, event["turn"])
                    yield f"event: fallback\ndata: {json.dumps({'reason': event['reason']})}\n\n"
                    return
                elif event["event"] == "done":
                    # Titled here and not at the top of the request for the
                    # same reason NOTHING else is persisted before "done":
                    # a fallback writes nothing at all, and a session titled
                    # after a turn it never recorded would show up in the
                    # sidebar with a name and an empty transcript. On the
                    # fallback path the REST retry (`post_message`) titles it.
                    _ensure_title(session, payload.content)
                    persist_new_messages(
                        db, session_id,
                        [user_wire, event["messages"][-1]],
                        citations=event["citations"] or None,
                    )
                    # A completed streamed turn moves the transcript on — any
                    # stashed first response from an EARLIER fallback the
                    # client never resent is stale now; drop it rather than
                    # let it linger until TTL (§3 handoff hygiene).
                    FIRST_TURN_HANDOFF.discard(session_id)
                    yield f"event: done\ndata: {json.dumps({'citations': event['citations']})}\n\n"
                    return
        except Exception:
            # Never let an unhandled exception mid-generator surface as a
            # bare closed connection — an already-started SSE response can't
            # switch to a 500 status at this point (headers are long sent),
            # so the only honest thing left to do is tell the client to fall
            # back, same as `stream_plain_turn`'s own internal guard does.
            log.exception("post_message_stream: unhandled error mid-stream (session_id=%s)", session_id)
            yield f"event: fallback\ndata: {json.dumps({'reason': 'error'})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/chat/{session_id}/approvals/{approval_id}/resolve", response_model=ChatTurnOut)
def resolve_approval(
    session_id: UUID,
    approval_id: UUID,
    payload: ApprovalResolveRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ChatTurnOut:
    session = _get_session_or_404(db, session_id)

    approval = db.get(ApprovalRequest, approval_id)
    if approval is None or approval.session_id != session_id:
        raise HTTPException(status_code=404, detail="approval request not found")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail=f"approval already {approval.status}")

    wire = window_wire(messages_to_wire(_ordered_messages(db, session_id)))
    wire = _inject_curriculum_context(db, session, wire)
    tool_call_id = approval.tool_call_id

    if payload.decision == "reject":
        approval.status = "rejected"
        approval.resolved_at = datetime.now(timezone.utc)
        tool_msg = {
            "role": "tool", "tool_call_id": tool_call_id,
            "content": "User rejected this action.",
        }
        # Also flushes `approval`'s own pending status/resolved_at change —
        # one Session, one unit of work.
        persist_new_messages(db, session_id, [tool_msg])

        wire_with_answer = wire + [tool_msg]
        result = run_agent_turn(db, wire_with_answer, locale=session.locale)
        return _respond_to_turn(db, session_id, wire_with_answer, result)

    # decision == "approve" (the only other value Literal["approve","reject"] allows)
    if payload.edited_args is not None:
        approval.edited_args = payload.edited_args
    args = approval.edited_args if approval.edited_args is not None else approval.tool_args

    # RE-INJECT the session's locale (Plan 13, Stage 5.4). `loop.py` already
    # injected it into `tool_args` at suspend time, but `edited_args` REPLACES
    # `tool_args` wholesale — a tutor who edits the JSON on the approval card and
    # drops `language` would hand `run_curriculum_job` a params dict with no
    # language in it (a `TypeError` inside a background task, i.e. a job that
    # just says "failed"), and one who *changes* it would be choosing a language
    # the rest of the app disagrees with. The session's locale wins, always.
    args = with_locale(approval.tool_name, args, session.locale)

    # Re-validate an apply_curriculum_revision plan against the live tree
    # BEFORE it is enqueued (finding #2, whole-branch review) — `edited_args`
    # above bypasses `_validate_pending_revision`'s gate entirely, and the job
    # this enqueues applies the plan in one untrusted-input-free transaction.
    # Raises 422 if the edit hollowed the plan out to nothing.
    if approval.tool_name == "apply_curriculum_revision":
        args = _revalidate_revision_args_for_resolve(db, args)

    entry = TOOLS.get(approval.tool_name)
    if entry is None:
        # Defensive only: every `ApprovalRequest` this router itself creates
        # names a tool that was, by construction, resolvable in `TOOLS` at
        # propose time (Task 3's registry hasn't shrunk since); guard rather
        # than crash if it somehow doesn't resolve at resolve time anyway.
        raise HTTPException(status_code=422, detail=f"unknown tool: {approval.tool_name!r}")

    if entry.async_job:
        # DATA-DRIVEN dispatch off `entry.job_kind` (review fix, Plan 10
        # Task 3) — this used to hardcode `kind="curriculum"` +
        # `run_curriculum_job` for ANY `async_job=True` tool, which was
        # harmless while `generate_curriculum` was the only one but silently
        # WRONG the moment `draft_lesson_from_selection` (`job_kind="lesson"`)
        # was registered alongside it: approving a lesson-draft would enqueue
        # a `kind="curriculum"` job and run `run_curriculum_job` against
        # lesson params (TypeError -> job `status="failed"`, no crash but a
        # misleading result). A THIRD async tool needs no new branch here —
        # just a `job_kind` on its `ToolEntry` (`app/agent/tools.py`) and one
        # entry in the two dicts below.
        #
        # The runner lookup is built HERE, inside the function body (NOT at
        # module scope), specifically so `run_curriculum_job`/`run_lesson_job`
        # are resolved through THIS module's own globals on every call — the
        # tests rely on `monkeypatch.setattr(chat_router, "run_curriculum_job"
        # / "run_lesson_job", ...)` still taking effect (Starlette's
        # TestClient runs `BackgroundTasks` in-process, AFTER the response;
        # an unpatched runner would fire a real multi-minute LLM call). A
        # module-level dict built once at import time would freeze in the
        # ORIGINAL function objects and silently ignore that monkeypatch.
        job_kind = entry.job_kind
        runner = {
            "curriculum": run_curriculum_job,
            "lesson": run_lesson_job,
            "curriculum_revise": run_curriculum_revise_job,
        }[job_kind]
        label = _ASYNC_JOB_LABELS[job_kind]

        # Exact Plan 8 enqueue pattern (`routers/curriculum.py:117-131`):
        # commit the GenerationJob row BEFORE scheduling/returning, so an
        # immediate `GET /jobs/{id}` poll is guaranteed to see it.
        job = GenerationJob(kind=job_kind, status="pending", params=args)
        db.add(job)
        db.commit()
        db.refresh(job)
        background_tasks.add_task(runner, job.id)

        approval.status = "approved"
        approval.result_ref = str(job.id)
        approval.resolved_at = datetime.now(timezone.utc)
        tool_msg = {
            "role": "tool", "tool_call_id": tool_call_id,
            "content": f"{label} started (job {job.id}).",
        }
        persist_new_messages(db, session_id, [tool_msg])

        # Do NOT resume the loop here — the tree isn't ready yet; the
        # tool-result above is recorded for whenever the conversation next
        # continues. The client polls `GET /jobs/{id}` (Plan 8) instead.
        return ChatTurnOut(status="job_pending", job_id=job.id)

    try:
        tool_result = entry.fn(db, **args)
    except Exception as exc:
        # Task 3 concern #3: a mutation fn CAN raise (validation error, an
        # LLM-backed tool's GuidedJSONError/timeout, ...) — must not become a
        # raw 500. `db.rollback()` mirrors `run_curriculum_job`'s own
        # documented recovery: `entry.fn` may have partially flushed (though
        # never committed) writes before raising — e.g. `assign_curriculum`'s
        # `clone_content_subtree` flushes each cloned node as it recurses —
        # and this Session keeps being used afterward (to persist the error
        # tool-message and resume the loop), unlike the existing HTTP
        # mutation endpoints which just let the exception map straight to an
        # HTTPException and end the request. Rollback expires the identity
        # map, so `approval` is re-fetched before being mutated again.
        log.warning(
            "mutation tool %r raised during resolve (approval_id=%s)",
            approval.tool_name, approval_id, exc_info=True,
        )
        db.rollback()
        approval = db.get(ApprovalRequest, approval_id)
        approval.status = "error"
        approval.resolved_at = datetime.now(timezone.utc)
        tool_msg = {"role": "tool", "tool_call_id": tool_call_id, "content": f"ERROR: {exc}"}
    else:
        # A mutation fn can also FAIL GRACEFULLY by RETURNING a `{"error":
        # ...}` dict (bad/hallucinated UUID, not-found, empty-title — the
        # `{"error"}` paths in `agent/tools.py`) rather than raising. That's a
        # failed action too, so record `status="error"` (same terminal value
        # as the raise path above), not "approved" with a misleading
        # `result_ref=None` indistinguishable from a real no-id success. The
        # tool message still carries the stringified error dict so the model
        # narrates it — identical to the success shape, only the audit status
        # differs.
        if isinstance(tool_result, dict) and "error" in tool_result:
            approval.status = "error"
        else:
            approval.status = "approved"
            approval.result_ref = _result_ref(tool_result)
        approval.resolved_at = datetime.now(timezone.utc)
        tool_msg = {"role": "tool", "tool_call_id": tool_call_id, "content": _stringify(tool_result)}

    persist_new_messages(db, session_id, [tool_msg])

    wire_with_answer = wire + [tool_msg]
    result = run_agent_turn(db, wire_with_answer, locale=session.locale)
    return _respond_to_turn(db, session_id, wire_with_answer, result)


@router.post("/chat/{session_id}/distill", response_model=DistilledInstructionOut)
def distill_chat_instruction(
    session_id: UUID, db: Session = Depends(get_db)
) -> DistilledInstructionOut:
    """The revise chat's "talk it through first" exit (task 7, 2026-07-21):
    the tutor thinks a change through with the assistant, presses the distill
    button, and ONE cheap call writes the revision instruction he MEANT —
    which lands back in his composer to review, edit, and send. Deliberately
    approve-before-spend, same posture as the interview's planning distill:
    this endpoint returns text; it never plans, proposes, or applies anything.

    409s (not empty 200s) for the states the tutor can fix: a session that is
    not bound to a curriculum, a conversation he hasn't spoken in yet, or a
    model reply with nothing usable in it — each with a sentence naming it.
    """
    from app.curriculum.revise import distill_revise_instruction

    session = _get_session_or_404(db, session_id)
    if not session.root_id:
        raise HTTPException(
            status_code=409,
            detail="This chat is not attached to a curriculum — open it from a course's Revise panel.",
        )
    try:
        instruction = distill_revise_instruction(db, session)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return DistilledInstructionOut(instruction=instruction)


@router.post("/chat/{session_id}/suggestions", response_model=SuggestionsOut)
def get_chat_suggestions(session_id: UUID, db: Session = Depends(get_db)) -> SuggestionsOut:
    """Suggestion CHIPS (chat overhaul, Piece B) — "next move" shortcuts the
    tutor can click instead of typing. A SEPARATE, lightweight call from the
    turn itself, fired by the frontend AFTER an assistant answer already
    rendered — this endpoint is never on the critical path of a turn, and
    nothing here may ever make the tutor wait longer for his answer.

    WHY A SEPARATE ENDPOINT, NOT PARSED OUT OF THE ANSWER. `claude -p` cannot
    reliably emit inline structured suggestions inside free-form prose — the
    tutor was emphatic this must be SOLID, not perplexing, so rather than
    regex/parse for a maybe-there sidecar block in the model's own answer,
    this runs one independent `guided_json` call with a schema the provider
    enforces server-side (`SUGGESTIONS_SCHEMA`).

    GRACEFUL ON EVERY FAILURE MODE. A `guided_json` call can raise (rate
    limit, timeout, malformed/truncated JSON — the whole `LLMError`/
    `GuidedJSONError` taxonomy) or return something not usable (missing key,
    wrong type). Either way this returns `{"suggestions": []}`, never a
    4xx/5xx — the frontend's contract is "chips appear a moment later, or
    they don't", and a suggestions failure must never surface as an error the
    tutor has to react to.

    NOTHING TO SUGGEST FROM YET (`visible` empty) short-circuits before ever
    calling the provider — a session with no user/assistant turns has no
    "next move" to suggest, and there is no reason to spend a call finding
    that out.
    """
    session = _get_session_or_404(db, session_id)
    visible = [m for m in _ordered_messages(db, session_id) if m.role in _VISIBLE_ROLES and m.content]
    if not visible:
        return SuggestionsOut(suggestions=[])

    try:
        # Prompt/transcript build INSIDE the guard too: `_suggestions_system_prompt`
        # does a `db.get(Block, root_id)` + `.format(...)`, so a transient DB hiccup
        # there must also degrade to no chips, not 500 (this endpoint's contract is
        # graceful on EVERY failure mode).
        system = _suggestions_system_prompt(db, session)
        transcript = _suggestions_transcript(visible)
        # `role="chat"` (medium effort, no thinking) — cheap and fast is the
        # whole point of a call that only ever produces up to 3 short
        # strings; this is not the ReAct loop and touches no tool.
        data = get_provider().guided_json(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": transcript},
            ],
            SUGGESTIONS_SCHEMA,
            role="chat",
        )
    except Exception:
        log.warning(
            "chat suggestions: guided_json call failed (session_id=%s) — "
            "degrading to no chips", session_id, exc_info=True,
        )
        return SuggestionsOut(suggestions=[])

    raw = data.get("suggestions") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return SuggestionsOut(suggestions=[])
    # Post-hoc cleanup, not trust: structured output does not enforce
    # `maxItems`/non-empty strings (see `SUGGESTIONS_SCHEMA`'s own comment),
    # so a stray non-string entry or a blank/whitespace-only suggestion is
    # dropped here rather than rendered as an empty chip.
    cleaned = [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    return SuggestionsOut(suggestions=cleaned[:_SUGGESTIONS_CAP])
