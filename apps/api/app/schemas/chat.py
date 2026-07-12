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

from pydantic import BaseModel, ConfigDict


class ChatSessionCreate(BaseModel):
    student_id: UUID | None = None


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
