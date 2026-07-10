"""`/chat` routes (Plan 5 Task 4) — THE integration crux of the chat
copilot: session creation, turn-taking through `run_agent_turn` (Task 2),
and the HITL approve/reject/resume + async-generation compose built on
Task 3's suspend-on-mutation models, reusing Plan 8's exact enqueue/poll
pattern (`routers/curriculum.py:88-131`, `app.jobs.runner.run_curriculum_job`)
for the one async mutation (`generate_curriculum`).

This router owns ALL persistence for the chat state machine — `run_agent_turn`
itself stays session-agnostic and never writes anything (see `loop.py`'s own
module docstring); this module is what turns a session's `Message` rows into
the wire transcript the loop consumes (`app.agent.transcript.
messages_to_wire`), persists whatever new turns the loop produces
(`persist_new_messages`), and persists/resolves the `ApprovalRequest` a
suspended turn leaves behind.

No-auth PoC posture, same as every other router in this app — no
authentication/authorization here either; this deploys origin-locked behind
Cloudflare for a single user.
"""
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.loop import AgentResult, run_agent_turn
from app.agent.tools import TOOLS
from app.agent.transcript import messages_to_wire, persist_new_messages
from app.db import get_db
from app.jobs.runner import run_curriculum_job
from app.models.chat import ApprovalRequest, ChatSession, Message
from app.models.generation_job import GenerationJob
from app.schemas.chat import (
    ApprovalResolveRequest,
    ChatMessageIn,
    ChatSessionCreate,
    ChatSessionCreated,
    ChatTurnOut,
    MessageOut,
    PendingApprovalOut,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


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


def _stringify(result) -> str:
    """Mirrors `loop.py`'s own `_stringify` exactly (small deliberate
    duplication rather than importing a `_`-prefixed helper from another
    module, per this codebase's established precedent) — turns a mutation
    fn's plain JSON-serializable result into the tool message's `content`
    string. `default=str` covers non-JSON-native fields (`UUID`, `datetime`).
    """
    return json.dumps(result, default=str)


def _result_ref(tool_result) -> str | None:
    """Best-effort opaque pointer for `ApprovalRequest.result_ref` on a sync
    mutation's success (`app.models.chat.ApprovalRequest.result_ref` is
    deliberately untyped — "a created row's id" is just the common case, not
    a contract every tool must satisfy). Every current sync mutation's `fn`
    returns a dict: `create_student`/`update_student`/`update_block`/
    `generate_artifact` carry `"id"`; `assign_curriculum` returns a block
    tree (also `"id"`, the new root's); `segment_block` alone has no single
    id, only `"session_ids"`. Anything else (or a non-dict result) falls
    back to `None` rather than raising — this is bookkeeping, not a
    correctness-critical value.
    """
    if isinstance(tool_result, dict):
        if "id" in tool_result:
            return str(tool_result["id"])
        if "session_ids" in tool_result:
            return ", ".join(str(i) for i in tool_result["session_ids"])
    return None


def _default_description(tool_name: str, tool_args: dict) -> str:
    """Fallback `description` for an `awaiting_approval` response when the
    model gave no narration (`AgentResult.content is None` — a real,
    documented case; Task 3's own tests pin exactly this for the suspend
    path). Just enough for a human to recognize what they're being asked to
    approve without the UI needing its own per-tool copy (Task 5's concern).
    """
    return f"Proposed action: {tool_name} with arguments {tool_args}"


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
    """
    tail = _new_tail(result.messages, prior_wire)
    persist_new_messages(db, session_id, tail)

    if result.status == "awaiting_approval":
        pending = result.pending_tool
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
        )

    return ChatTurnOut(status="answer", content=result.content)


@router.post("/chat", response_model=ChatSessionCreated)
def create_chat_session(payload: ChatSessionCreate, db: Session = Depends(get_db)) -> ChatSessionCreated:
    session = ChatSession(student_id=payload.student_id)
    db.add(session)
    db.commit()
    return ChatSessionCreated(session_id=session.id)


@router.get("/chat/{session_id}", response_model=list[MessageOut])
def get_chat_history(session_id: UUID, db: Session = Depends(get_db)) -> list[Message]:
    _get_session_or_404(db, session_id)
    rows = _ordered_messages(db, session_id)
    return [row for row in rows if row.role in ("user", "assistant")]


@router.get("/chat/{session_id}/pending", response_model=PendingApprovalOut | None)
def get_pending_approval(session_id: UUID, db: Session = Depends(get_db)) -> PendingApprovalOut | None:
    _get_session_or_404(db, session_id)
    approval = _open_pending_approval(db, session_id)
    if approval is None:
        return None
    return PendingApprovalOut.model_validate(approval, from_attributes=True)


@router.post("/chat/{session_id}/messages", response_model=ChatTurnOut)
def post_message(session_id: UUID, payload: ChatMessageIn, db: Session = Depends(get_db)) -> ChatTurnOut:
    _get_session_or_404(db, session_id)

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
        raise HTTPException(
            status_code=409,
            detail="an approval is pending — resolve it before sending a new message",
        )

    persist_new_messages(db, session_id, [{"role": "user", "content": payload.content}])

    wire = messages_to_wire(_ordered_messages(db, session_id))
    result = run_agent_turn(db, wire)

    return _respond_to_turn(db, session_id, wire, result)


@router.post("/chat/{session_id}/approvals/{approval_id}/resolve", response_model=ChatTurnOut)
def resolve_approval(
    session_id: UUID,
    approval_id: UUID,
    payload: ApprovalResolveRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ChatTurnOut:
    _get_session_or_404(db, session_id)

    approval = db.get(ApprovalRequest, approval_id)
    if approval is None or approval.session_id != session_id:
        raise HTTPException(status_code=404, detail="approval request not found")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail=f"approval already {approval.status}")

    wire = messages_to_wire(_ordered_messages(db, session_id))
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
        result = run_agent_turn(db, wire_with_answer)
        return _respond_to_turn(db, session_id, wire_with_answer, result)

    # decision == "approve" (the only other value Literal["approve","reject"] allows)
    if payload.edited_args is not None:
        approval.edited_args = payload.edited_args
    args = approval.edited_args if approval.edited_args is not None else approval.tool_args

    entry = TOOLS.get(approval.tool_name)
    if entry is None:
        # Defensive only: every `ApprovalRequest` this router itself creates
        # names a tool that was, by construction, resolvable in `TOOLS` at
        # propose time (Task 3's registry hasn't shrunk since); guard rather
        # than crash if it somehow doesn't resolve at resolve time anyway.
        raise HTTPException(status_code=422, detail=f"unknown tool: {approval.tool_name!r}")

    if entry.async_job:
        # generate_curriculum ONLY (Task 3's one `async_job=True` entry) —
        # exact Plan 8 enqueue pattern (`routers/curriculum.py:117-131`):
        # commit the GenerationJob row BEFORE scheduling/returning, so an
        # immediate `GET /jobs/{id}` poll is guaranteed to see it.
        job = GenerationJob(kind="curriculum", status="pending", params=args)
        db.add(job)
        db.commit()
        db.refresh(job)
        background_tasks.add_task(run_curriculum_job, job.id)

        approval.status = "approved"
        approval.result_ref = str(job.id)
        approval.resolved_at = datetime.now(timezone.utc)
        tool_msg = {
            "role": "tool", "tool_call_id": tool_call_id,
            "content": f"Curriculum generation started (job {job.id}).",
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
    result = run_agent_turn(db, wire_with_answer)
    return _respond_to_turn(db, session_id, wire_with_answer, result)
