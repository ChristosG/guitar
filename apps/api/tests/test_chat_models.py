"""Tests for the `ChatSession`/`Message`/`ApprovalRequest` models' DB
round-trip (Plan 5 Task 3 — the HITL state machine). Mirrors
`test_generation_job.py`'s skip-guard + setup_module pattern (this
codebase's current convention for DB-touching test modules) rather than
`test_models_roundtrip.py`'s older standalone one.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.db import Base, SessionLocal, engine

# Skip cleanly (not error) when no DB is reachable — mirrors test_students_api.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )

import app.models  # noqa: F401  register every model's table on Base.metadata
from app.models.chat import ApprovalRequest, ChatSession, Message


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


# ---------------------------------------------------------------------------
# ChatSession
# ---------------------------------------------------------------------------

def test_chat_session_persists_and_rereads_with_no_student():
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        session_id = session.id
    finally:
        db.close()

    # Fresh session -> a genuine DB round-trip, not the identity-map object
    # reused under expire_on_commit=False (mirrors test_models_roundtrip.py).
    db2 = SessionLocal()
    try:
        got = db2.get(ChatSession, session_id)
        assert got is not None
        assert got.student_id is None
        assert got.created_at is not None
        assert got.updated_at is not None
    finally:
        db2.close()


def test_chat_session_student_id_has_no_fk_a_dangling_uuid_is_accepted():
    """`ChatSession.student_id` is a plain nullable UUID column, deliberately
    NOT a `ForeignKey` (see `app.models.chat.ChatSession`'s own docstring for
    the rationale — mirrors `GenerationJob.result_root_id`'s own
    "no FK when decoupled" precedent). An id that matches no real `Student`
    row must still be accepted without error — a genuine FK would instead
    reject it with an IntegrityError.
    """
    dangling_id = uuid.uuid4()
    db = SessionLocal()
    try:
        session = ChatSession(student_id=dangling_id)
        db.add(session)
        db.commit()
        session_id = session.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(ChatSession, session_id)
        assert got.student_id == dangling_id
    finally:
        db2.close()


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------

def test_message_persists_and_rereads_with_defaults():
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        msg = Message(session_id=session.id, role="user", content="hi there")
        db.add(msg)
        db.commit()
        msg_id = msg.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(Message, msg_id)
        assert got is not None
        assert got.role == "user"
        assert got.content == "hi there"
        assert got.tool_calls is None
        assert got.created_at is not None
    finally:
        db2.close()


def test_message_persists_tool_calls_json_payload_and_null_content():
    tool_calls = [{
        "id": "call_1", "type": "function",
        "function": {"name": "search_knowledge", "arguments": "{\"query\": \"tone\"}"},
    }]
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        # A tool-calls-only assistant turn has no plain text (mirrors
        # AssistantTurn.content's own None-when-absent contract).
        msg = Message(session_id=session.id, role="assistant", content=None, tool_calls=tool_calls)
        db.add(msg)
        db.commit()
        msg_id = msg.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(Message, msg_id)
        assert got.content is None
        assert got.tool_calls == tool_calls  # JSON round-trip
    finally:
        db2.close()


def test_message_persists_tool_call_id_for_a_tool_role_message():
    """Task 4: `Message.tool_call_id` pairs a `role="tool"` row back to the
    assistant `tool_call` it answers — needed for lossless wire-transcript
    round-tripping (`messages_to_wire` reconstructs `{"role":"tool",
    "tool_call_id": ..., "content": ...}` from this column).
    """
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        msg = Message(
            session_id=session.id, role="tool", content="42",
            tool_call_id="call_abc123",
        )
        db.add(msg)
        db.commit()
        msg_id = msg.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(Message, msg_id)
        assert got.role == "tool"
        assert got.tool_call_id == "call_abc123"
        assert got.content == "42"
    finally:
        db2.close()


def test_message_tool_call_id_defaults_to_none_for_user_and_assistant():
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        user_msg = Message(session_id=session.id, role="user", content="hi")
        assistant_msg = Message(session_id=session.id, role="assistant", content="hello")
        db.add_all([user_msg, assistant_msg])
        db.commit()
        user_id, assistant_id = user_msg.id, assistant_msg.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        assert db2.get(Message, user_id).tool_call_id is None
        assert db2.get(Message, assistant_id).tool_call_id is None
    finally:
        db2.close()


def test_message_cascade_deletes_with_its_session():
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        msg = Message(session_id=session.id, role="user", content="bye")
        db.add(msg)
        db.commit()
        session_id, msg_id = session.id, msg.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        db2.delete(db2.get(ChatSession, session_id))
        db2.commit()
    finally:
        db2.close()

    db3 = SessionLocal()
    try:
        assert db3.get(ChatSession, session_id) is None
        assert db3.get(Message, msg_id) is None  # cascaded via ondelete=CASCADE
    finally:
        db3.close()


# ---------------------------------------------------------------------------
# ApprovalRequest
# ---------------------------------------------------------------------------

def test_approval_request_persists_with_defaults():
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        approval = ApprovalRequest(
            session_id=session.id,
            tool_name="generate_artifact",
            tool_args={"kind": "chord_diagram", "prompt": "G major open chord"},
        )
        db.add(approval)
        db.commit()
        approval_id = approval.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(ApprovalRequest, approval_id)
        assert got is not None
        assert got.tool_name == "generate_artifact"
        assert got.tool_args == {"kind": "chord_diagram", "prompt": "G major open chord"}
        assert got.status == "pending"  # column default, not passed explicitly
        assert got.edited_args is None
        assert got.result_ref is None
        assert got.resolved_at is None
    finally:
        db2.close()


def test_approval_request_persists_tool_call_id():
    """Task 4: `ApprovalRequest.tool_call_id` is the `tool_call_id` of the
    pending mutation call it gates (`AgentResult.pending_tool["tool_call_id"]`
    at suspend time) — the resolve endpoint needs it to answer the exact
    right call without re-scanning the transcript for it.
    """
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        approval = ApprovalRequest(
            session_id=session.id,
            tool_name="update_block",
            tool_args={"block_id": "b1", "title": "New Title"},
            tool_call_id="call_xyz",
        )
        db.add(approval)
        db.commit()
        approval_id = approval.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(ApprovalRequest, approval_id)
        assert got.tool_call_id == "call_xyz"
    finally:
        db2.close()


def test_approval_request_tool_call_id_defaults_to_none():
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        approval = ApprovalRequest(session_id=session.id, tool_name="x", tool_args={})
        db.add(approval)
        db.commit()
        approval_id = approval.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        assert db2.get(ApprovalRequest, approval_id).tool_call_id is None
    finally:
        db2.close()


def test_approval_request_persists_a_resolved_approve_decision():
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        approval = ApprovalRequest(
            session_id=session.id, tool_name="update_block",
            tool_args={"block_id": "x", "title": "advanced"},
        )
        db.add(approval)
        db.commit()
        approval_id = approval.id
    finally:
        db.close()

    resolved_at = datetime.now(timezone.utc)
    edited_args = {"block_id": "x", "title": "intermediate"}
    db2 = SessionLocal()
    try:
        approval = db2.get(ApprovalRequest, approval_id)
        approval.status = "approved"
        approval.edited_args = edited_args
        approval.result_ref = "some-block-id"
        approval.resolved_at = resolved_at
        db2.commit()
    finally:
        db2.close()

    db3 = SessionLocal()
    try:
        got = db3.get(ApprovalRequest, approval_id)
        assert got.status == "approved"
        assert got.edited_args == edited_args
        assert got.result_ref == "some-block-id"
        assert got.resolved_at is not None
    finally:
        db3.close()


def test_approval_request_cascade_deletes_with_its_session():
    db = SessionLocal()
    try:
        session = ChatSession()
        db.add(session)
        db.commit()
        approval = ApprovalRequest(session_id=session.id, tool_name="x", tool_args={})
        db.add(approval)
        db.commit()
        session_id, approval_id = session.id, approval.id
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        db2.delete(db2.get(ChatSession, session_id))
        db2.commit()
    finally:
        db2.close()

    db3 = SessionLocal()
    try:
        assert db3.get(ApprovalRequest, approval_id) is None  # cascaded via ondelete=CASCADE
    finally:
        db3.close()


# ---------------------------------------------------------------------------
# Part 5: planning chat columns
# ---------------------------------------------------------------------------

def test_chat_session_interview_id_and_interview_planning_brief_roundtrip():
    """Part 5 columns: an interview-bound session (interview_id set, root_id
    None) and the stored planning brief both persist and reload."""
    from app.models.interview import CurriculumInterview

    db = SessionLocal()
    try:
        interview = CurriculumInterview(title="Ήχος και Ενισχυτές")
        db.add(interview)
        db.flush()
        interview.planning_brief = "Στόχος: 20 εβδομάδες για ήχο και ενισχυτές."
        session = ChatSession(interview_id=interview.id, locale="el")
        db.add(session)
        db.commit()

        db.expire_all()
        reloaded = db.get(ChatSession, session.id)
        assert reloaded.interview_id == interview.id
        assert reloaded.root_id is None
        assert db.get(CurriculumInterview, interview.id).planning_brief.startswith("Στόχος")
    finally:
        db.close()
