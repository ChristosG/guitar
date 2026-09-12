"""Pydantic request/response models for the Chat API (`routers/chat.py`):
session creation, turn-taking through `run_agent_turn`, and the HITL
approve/reject/resume + async-generation compose (Plan 5 Task 4).

Kept separate from the SQLAlchemy `app.models.chat` models per this
codebase's established split (mirrors `schemas/curriculum.py` vs
`app.models.block`) — this module is only the HTTP boundary's shape.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ChatSessionCreate(BaseModel):
    student_id: UUID | None = None
    # The UI language this conversation is being started in. Optional so every
    # pre-existing caller (and every test written before Stage 5.6) still
    # compiles — the column's own default, "el", is the app's default locale.
    locale: str | None = Field(default=None, max_length=5)
    # Unit D: bind this conversation to one curriculum (the revise drawer sets
    # it; None for the ordinary global chat). Optional so every pre-existing
    # caller still compiles.
    root_id: UUID | None = None


class ChatSessionUpdate(BaseModel):
    """`PATCH /chat/{id}` — rename only. `min_length=1` after the router
    strips: an all-whitespace title would render as a blank row in the
    sidebar with nothing to click back to.
    """
    title: str = Field(min_length=1, max_length=200)


class ChatSessionOut(BaseModel):
    """One session, WITHOUT the transcript-derived fields (`GET /chat`'s
    `ChatSessionSummary` below carries those). This is what `PATCH` answers
    with: a rename cannot change a message count or a preview, and computing
    them again for the one renamed row would mean re-running the list
    endpoint's aggregate for a value the client already holds.
    """
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str | None = None
    locale: str
    student_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class ChatSessionSummary(ChatSessionOut):
    """One row of `GET /chat`'s sidebar list.

    `message_count`/`preview`/`last_message_at` count only user+assistant
    rows — the same filter `GET /chat/{id}` applies to the transcript itself
    (tool rows are internal plumbing), so the count the tutor sees matches the
    number of bubbles he'll get when he opens it, rather than silently
    including the tool traffic of an approved mutation.
    """
    message_count: int
    last_message_at: datetime
    preview: str | None = None


class ChatSessionCreated(BaseModel):
    """`POST /chat`'s response: just enough for the caller to start posting
    messages against — mirrors `schemas/jobs.py`'s `JobAccepted` precedent
    ("just enough for the caller to start polling"), same idea pointed at a
    session instead of a job.
    """
    session_id: UUID


class ChatMessageIn(BaseModel):
    content: str


class MessageOut(BaseModel):
    """One row of `GET /chat/{session_id}`'s history — user/assistant only;
    tool/system rows are internal plumbing the brief says to omit.
    """
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: str
    content: str | None = None
    created_at: datetime
    # Plan 11 Task 1 (C2): the grounding hits `run_agent_turn`'s forced
    # retrieval found for this turn, if this row is the assistant message
    # that answered it — None for a user row, or an assistant row from a
    # turn that had nothing to cite. Lets the UI render a citation chip that
    # deep-links into the Reader at `page_no`.
    citations: list[dict] | None = None


class ChatTurnOut(BaseModel):
    """The response shape for both `POST /chat/{session_id}/messages` and
    `POST /chat/{session_id}/approvals/{approval_id}/resolve` — which fields
    are populated depends on `status`:
      - "answer": `content`.
      - "awaiting_approval": `approval_id`, `tool_name`, `tool_args`, `description`.
      - "job_pending" (resolve only): `job_id`.

    `POST .../messages?async=1` does NOT return this model at all — it returns
    a 202 `JobAccepted` and the finished `ChatTurnOut` lands on the job row's
    `progress["turn"]` (`app/jobs/chat_turn.py`), so the poller reads the very
    same shape a synchronous turn would have returned.

    One flat model (every field beyond `status` optional) rather than 3
    separate response_models: FastAPI favors a single concrete response_model
    per route, and all 3 shapes are fundamentally "a turn just happened,
    here's what to show/do next" — a discriminated union would add ceremony
    without a real type-safety payoff for a single-user PoC client.
    """
    status: str
    content: str | None = None
    approval_id: UUID | None = None
    tool_name: str | None = None
    tool_args: dict | None = None
    description: str | None = None
    job_id: UUID | None = None
    # Plan 11 Task 1 (C2) — mirrors `MessageOut.citations`: populated for an
    # "answer"/"awaiting_approval" turn that had something to cite, None
    # otherwise (never persisted for "job_pending" — that response never
    # carries a fresh assistant turn of its own).
    citations: list[dict] | None = None


class ApprovalResolveRequest(BaseModel):
    decision: Literal["approve", "reject"]
    edited_args: dict | None = None


class PendingApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tool_name: str
    tool_args: dict
    status: str
    created_at: datetime


class DistilledInstructionOut(BaseModel):
    """`POST /chat/{session_id}/distill`'s response — the revise chat's "talk
    it through first" exit. One tutor-voiced revision instruction distilled
    from the conversation, returned for the tutor to REVIEW AND EDIT in his
    composer; nothing is planned, proposed, or applied by this call."""
    instruction: str


class SuggestionsOut(BaseModel):
    """`POST /chat/{session_id}/suggestions`'s response (chat overhaul, Piece
    B "next move" chips). 0-3 short, concrete, DOABLE next actions — never a
    failure surface: a provider error or an unusable reply both degrade to an
    empty list here (see `routers/chat.py`'s `get_chat_suggestions`), so the
    frontend's non-blocking fetch never needs to distinguish "nothing to
    suggest" from "the call failed" — both just mean no chips render.
    """
    suggestions: list[str] = []
