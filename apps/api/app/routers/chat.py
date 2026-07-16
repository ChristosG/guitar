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
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.loop import AgentResult, run_agent_turn, stream_plain_turn
from app.agent.tools import TOOLS, with_locale
from app.agent.transcript import messages_to_wire, persist_new_messages, window_wire
from app.db import get_db
from app.i18n import normalize_locale
from app.jobs.runner import run_curriculum_job, run_lesson_job
from app.llm.errors import LLMError
from app.models.chat import ApprovalRequest, ChatSession, Message
from app.models.generation_job import GenerationJob
from app.schemas.chat import (
    ApprovalResolveRequest,
    ChatMessageIn,
    ChatSessionCreate,
    ChatSessionCreated,
    ChatSessionOut,
    ChatSessionSummary,
    ChatSessionUpdate,
    ChatTurnOut,
    MessageOut,
    PendingApprovalOut,
)

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


def _stringify(result) -> str:
    """Mirrors `loop.py`'s own `_stringify` exactly (small deliberate
    duplication rather than importing a `_`-prefixed helper from another
    module, per this codebase's established precedent) — turns a mutation
    fn's plain JSON-serializable result into the tool message's `content`
    string. `default=str` covers non-JSON-native fields (`UUID`, `datetime`).
    """
    return json.dumps(result, default=str)


_RESULT_REF_MAX = 255  # ApprovalRequest.result_ref is String(255)


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
    persist_new_messages(db, session_id, tail, citations=result.citations)

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
    session = ChatSession(student_id=payload.student_id, locale=normalize_locale(payload.locale))
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


@router.post("/chat/{session_id}/messages", response_model=ChatTurnOut)
def post_message(session_id: UUID, payload: ChatMessageIn, db: Session = Depends(get_db)) -> ChatTurnOut:
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
        raise HTTPException(
            status_code=409,
            detail="an approval is pending — resolve it before sending a new message",
        )

    # Flushed by `persist_new_messages`'s own commit, on the same Session.
    _ensure_title(session, payload.content)
    persist_new_messages(db, session_id, [{"role": "user", "content": payload.content}])

    wire = window_wire(messages_to_wire(_ordered_messages(db, session_id)))
    try:
        result = run_agent_turn(db, wire, locale=session.locale)
    except LLMError as e:
        # A provider failure mid-turn used to escape as a raw 500 — "Internal
        # Server Error" in a non-technical user's browser for a 429 he only
        # needed to wait out. The user's message row is already persisted
        # (above), so after the wait his Send simply retries the turn. The
        # status codes match the taxonomy the frontend already translates.
        status = {"rate_limit": 429, "auth": 409, "timeout": 504}.get(e.kind, 502)
        raise HTTPException(
            status_code=status,
            detail=str(e) or f"the model provider failed ({e.kind}) — try again",
        ) from e

    return _respond_to_turn(db, session_id, wire, result)


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

    prior_wire = window_wire(messages_to_wire(_ordered_messages(db, session_id)))
    user_wire = {"role": "user", "content": payload.content}
    wire = prior_wire + [user_wire]

    def event_stream():
        try:
            for event in stream_plain_turn(db, wire, locale=session.locale):
                if event["event"] == "delta":
                    yield f"event: delta\ndata: {json.dumps({'text': event['text']})}\n\n"
                elif event["event"] == "fallback":
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
        runner = {"curriculum": run_curriculum_job, "lesson": run_lesson_job}[job_kind]
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
