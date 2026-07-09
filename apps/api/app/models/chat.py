"""HITL (human-in-the-loop) chat models (Plan 5 Task 3): the Postgres-backed
state machine behind the chat copilot's approve-before-execute mutation gate.

`ChatSession` is one conversation thread; `Message` is one turn in it (user /
assistant / tool); `ApprovalRequest` is the pending-or-resolved record of one
proposed MUTATION tool call `run_agent_turn` suspended on (`app/agent/
loop.py`). Modeled after `app.models.generation_job.GenerationJob`'s
restart-safe, Postgres-backed design (mixins, JSON columns for opaque
LLM-shaped payloads, a `status` lifecycle starting "pending") — same "the row
IS the state machine, no in-memory session state" precedent this app already
established for async work, just for a human decision instead of a
background job.

Task 4 (not built here) is what actually WRITES these rows on the request
path (persisting a `Message` per turn, an `ApprovalRequest` on suspend,
resolving one on approve/reject) — this task only defines the shapes so
Task 4 has somewhere to put them. `run_agent_turn`'s own suspend change
(this same task) does NOT persist anything itself; it stays session-agnostic
and returns the suspend info for a caller to persist (see `loop.py`'s
`AgentResult.pending_tool`).
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PkMixin, TimestampMixin


class ChatSession(Base, PkMixin, TimestampMixin):
    """One conversation thread with the chat copilot.

    `student_id` is an optional CONTEXT tag ("this conversation is about
    this student") — deliberately NOT a `ForeignKey`, unlike `Message.
    session_id`/`ApprovalRequest.session_id` below. Mirrors `GenerationJob.
    result_root_id`'s own "no FK when decoupled" precedent, just pointed the
    other way: that column points FORWARD to a job's independent result,
    this one points to a student the session happens to reference as
    context. A chat transcript is a standalone record of what was said and
    approved — it must stay intact and readable even if the referenced
    Student is later edited or deleted. An FK with `ondelete=CASCADE` would
    silently wipe an entire conversation history as a side effect of an
    unrelated roster edit (`DELETE /students/{id}`), and `SET NULL` would
    erase exactly the context a historical record needs most. A stale/
    dangling id here is an accepted tradeoff, same as `GenerationJob.
    result_root_id`'s own documented one.
    """

    __tablename__ = "chat_session"
    student_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Message(Base, PkMixin, TimestampMixin):
    """One turn in a `ChatSession`.

    `role` is "user" | "assistant" | "tool" — soft, relabelable (no DB-level
    enum), same precedent as `Block.kind`/`Progress.status`/`GenerationJob.
    status`. `content` is nullable: a tool-calls-only assistant turn has no
    plain text (mirrors `AssistantTurn.content`'s own None-when-absent
    contract in `app.llm.tools_types`). `tool_calls` holds the OpenAI
    wire-shape list (mirrors `loop.py`'s `_wire_assistant_message` output)
    for an assistant turn that proposed one or more tool calls — None for a
    plain user message, a plain assistant-text-only message, or a tool-result
    message.

    `session_id` IS a `ForeignKey` (unlike `ChatSession.student_id` above):
    a `Message` has no meaning detached from its session, so CASCADE-deleting
    it along with the session it belongs to is correct — same ownership
    rationale as `Block.children`'s cascade or `Assignment.student_id`'s.
    """

    __tablename__ = "message"
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_session.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)


class ApprovalRequest(Base, PkMixin, TimestampMixin):
    """The pending-or-resolved record of one proposed MUTATION tool call
    that `run_agent_turn` suspended on (see `app/agent/loop.py`).

    `tool_name`/`tool_args` are the exact `ToolCall.name`/`.arguments` the
    model proposed, captured verbatim so Task 4's resolve step can replay
    them (`edited_args`, when set, overrides `tool_args` at execute time
    instead of the raw model proposal — the human's edit wins). `status`
    starts "pending" and is later flipped to "approved" | "rejected" by Task
    4 — same pending -> terminal lifecycle SHAPE as `GenerationJob.status`
    (pending -> running -> succeeded|failed), just a different vocabulary
    for a one-shot human decision instead of a multi-step background job.

    `result_ref` is an opaque, untyped pointer Task 4 fills in on approve —
    e.g. a created row's id, or "job:<job_id>" for the async
    `generate_curriculum` path (per the design doc's compose-two-pauses
    flow). Deliberately a plain `String`, not a `ForeignKey`: which table it
    points at depends entirely on which tool was approved, so a single typed
    FK column could never cover it. `resolved_at` is None until a decision is
    made — no `server_default` (unlike `created_at`/`updated_at`), since it
    must reflect the actual resolution moment, not row-creation time.

    `session_id` IS a `ForeignKey` + CASCADE, same rationale as `Message.
    session_id`: an approval only makes sense scoped to the session that
    raised it.
    """

    __tablename__ = "approval_request"
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_session.id", ondelete="CASCADE"), index=True)
    tool_name: Mapped[str] = mapped_column(String(50))
    tool_args: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending | approved | rejected
    edited_args: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
