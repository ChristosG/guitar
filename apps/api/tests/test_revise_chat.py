"""D2a — the two chat agent tools + session→curriculum binding + the
validate-before-approval-card ordering.

`propose_curriculum_revision` (READ) returns the validated plan inline;
`apply_curriculum_revision` (async MUTATION, `job_kind="curriculum_revise"`) is
approval-gated and enqueues the revise job on approve. A session bound to a
curriculum (`ChatSession.root_id`) gets a transient compact-tree + brief context
block appended to its last user turn — never persisted, never in the cached
prefix.

APPROVED == APPLIED, EXACT (controller, 2026-07-18): the plan an
`apply_curriculum_revision` proposal carries is VALIDATED before the
`ApprovalRequest` is stored, so an op an id-validation would drop never reaches
the approval card or apply. Two tests pin exactly that ordering.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.agent.tools as agent_tools
import app.routers.chat as chat_router
from app.agent.loop import AgentResult
from app.agent.tools import TOOLS
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
from app.models.chat import ApprovalRequest, ChatSession
from app.models.generation_job import GenerationJob
from app.routers.chat import (
    _inject_curriculum_context,
    _respond_to_turn,
    _validate_pending_revision,
)

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


client = TestClient(app)


def _seed_tree(db):
    course = Block(kind="course", title="Tone 101", is_template=True, language="el",
                   meta={"brief": "for gigging players", "source_ids": None})
    db.add(course)
    db.flush()
    m = Block(kind="module", title="Overdrive", parent_id=course.id, order=0,
              language="el", meta={"objective": "od pedals"})
    db.add(m)
    db.flush()
    lesson = Block(kind="lesson", title="Tube Screamer", parent_id=m.id, order=0,
                   language="el", meta={"objective": "ts basics"})
    db.add(lesson)
    db.commit()
    return course, m, lesson


# ---------------------------------------------------------------------------
# (b) registry shape — propose is read, apply is async curriculum_revise mutation
# ---------------------------------------------------------------------------

def test_propose_is_a_read_tool_and_apply_is_an_async_curriculum_revise_mutation():
    propose = TOOLS["propose_curriculum_revision"]
    assert propose.kind == "read"
    assert propose.async_job is False

    apply = TOOLS["apply_curriculum_revision"]
    assert apply.kind == "mutation"
    assert apply.async_job is True
    assert apply.job_kind == "curriculum_revise"


# ---------------------------------------------------------------------------
# (a) propose fn calls plan_revision and returns its (validated) dict
# ---------------------------------------------------------------------------

def test_propose_fn_passes_through_the_planner_result(monkeypatch):
    captured = {}
    canned = {"summary": "add a DS-1 lesson", "ops": [
        {"op": "insert_lesson", "module_id": "m", "title": "DS-1", "objective": "o", "reason": "r"}]}

    def _fake_plan(db, root_id, *, instruction):
        captured.update(root_id=root_id, instruction=instruction)
        return canned

    monkeypatch.setattr(agent_tools, "_plan_revision_service", _fake_plan)
    root = uuid.uuid4()
    out = TOOLS["propose_curriculum_revision"].fn(None, root_id=str(root), instruction="add DS-1")
    assert out == canned
    assert captured["root_id"] == root
    assert captured["instruction"] == "add DS-1"


def test_propose_fn_bad_root_id_returns_graceful_error():
    out = TOOLS["propose_curriculum_revision"].fn(None, root_id="not-a-uuid", instruction="x")
    assert "error" in out


# ---------------------------------------------------------------------------
# (d) _inject_curriculum_context — tree onto the last user turn, only when bound
# ---------------------------------------------------------------------------

def test_inject_appends_the_tree_to_the_last_user_message_when_bound():
    db = SessionLocal()
    try:
        course, m, lesson = _seed_tree(db)
        session = ChatSession(locale="el", root_id=course.id)
        wire = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "should I add a DS-1?"},
        ]
        out = _inject_curriculum_context(db, session, wire)
        # last user message carries the context; the assistant + earlier user do not
        assert str(m.id) in out[-1]["content"]
        assert "Tube Screamer" in out[-1]["content"]
        assert "should I add a DS-1?" in out[-1]["content"]
        assert out[0]["content"] == "first"
        assert out[1]["content"] == "hi"
        # original wire dicts are NOT mutated (transient injection)
        assert wire[-1]["content"] == "should I add a DS-1?"
    finally:
        db.close()


def test_inject_is_a_noop_when_the_session_is_not_bound():
    db = SessionLocal()
    try:
        session = ChatSession(locale="el", root_id=None)
        wire = [{"role": "user", "content": "hello"}]
        out = _inject_curriculum_context(db, session, wire)
        assert out == wire
        assert out[0]["content"] == "hello"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Approved == applied, EXACT — validate BEFORE the approval card
# ---------------------------------------------------------------------------

def test_validate_pending_revision_drops_a_bogus_op_before_the_card():
    db = SessionLocal()
    try:
        course, m, lesson = _seed_tree(db)
        pending = {
            "tool_call_id": "call_1",
            "name": "apply_curriculum_revision",
            "arguments": {"root_id": str(course.id), "plan": {"summary": "s", "ops": [
                {"op": "insert_lesson", "module_id": str(m.id), "title": "DS-1",
                 "objective": "o", "reason": "keep"},
                {"op": "insert_lesson", "module_id": str(uuid.uuid4()),  # bogus
                 "title": "ghost", "objective": "x", "reason": "drop"},
            ]}},
        }
        _validate_pending_revision(db, pending)
        ops = pending["arguments"]["plan"]["ops"]
        assert len(ops) == 1                              # bogus op dropped
        assert ops[0]["module_id"] == str(m.id)
    finally:
        db.close()


def test_validate_pending_revision_leaves_other_tools_untouched():
    db = SessionLocal()
    try:
        pending = {"name": "generate_artifact",
                   "arguments": {"kind": "tab", "prompt": "riff"}}
        _validate_pending_revision(db, pending)
        assert pending["arguments"] == {"kind": "tab", "prompt": "riff"}
    finally:
        db.close()


def test_respond_to_turn_stores_the_VALIDATED_plan_on_the_approval_and_response():
    """End-to-end: an awaiting_approval turn proposing apply_curriculum_revision
    with a bogus op lands on the ApprovalRequest (and the response) with that op
    already dropped — the card can only ever render/apply the validated plan."""
    db = SessionLocal()
    try:
        course, m, lesson = _seed_tree(db)
        session = ChatSession(locale="el", root_id=course.id)
        db.add(session)
        db.commit()
        session_id = session.id

        plan = {"summary": "s", "ops": [
            {"op": "insert_lesson", "module_id": str(m.id), "title": "DS-1",
             "objective": "o", "reason": "keep"},
            {"op": "insert_lesson", "module_id": str(uuid.uuid4()),
             "title": "ghost", "objective": "x", "reason": "drop"},
        ]}
        assistant_msg = {
            "role": "assistant", "content": "I'll apply that revision.",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "apply_curriculum_revision", "arguments": "{}"}}],
        }
        result = AgentResult(
            status="awaiting_approval",
            content="I'll apply that revision.",
            messages=[assistant_msg],
            pending_tool={"tool_call_id": "call_1", "name": "apply_curriculum_revision",
                          "arguments": {"root_id": str(course.id), "plan": plan}},
            citations=[],
        )
        out = _respond_to_turn(db, session_id, [], result)

        assert out.status == "awaiting_approval"
        assert len(out.tool_args["plan"]["ops"]) == 1        # response carries validated plan
        approval = db.get(ApprovalRequest, out.approval_id)
        assert len(approval.tool_args["plan"]["ops"]) == 1   # stored plan is validated too
        assert approval.tool_args["plan"]["ops"][0]["module_id"] == str(m.id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# (c) resolve_approval on an apply approval enqueues the curriculum_revise job
# ---------------------------------------------------------------------------

def test_resolve_approval_enqueues_a_curriculum_revise_job_and_returns_job_pending(monkeypatch):
    ran = []
    monkeypatch.setattr(chat_router, "run_curriculum_revise_job", lambda job_id: ran.append(job_id))

    db = SessionLocal()
    try:
        course, m, lesson = _seed_tree(db)
        session = ChatSession(locale="el", root_id=course.id)
        db.add(session)
        db.commit()
        session_id = session.id

        plan = {"summary": "s", "ops": [
            {"op": "insert_lesson", "module_id": str(m.id), "title": "DS-1",
             "objective": "o", "reason": "r"}]}
        approval = ApprovalRequest(
            session_id=session_id, tool_name="apply_curriculum_revision",
            tool_args={"root_id": str(course.id), "plan": plan},
            tool_call_id="call_1", status="pending",
        )
        db.add(approval)
        db.commit()
        approval_id = approval.id
    finally:
        db.close()

    r = client.post(f"/chat/{session_id}/approvals/{approval_id}/resolve",
                    json={"decision": "approve"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "job_pending"
    job_id = uuid.UUID(body["job_id"])

    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        assert job.kind == "curriculum_revise"
        assert job.params["root_id"] == str(course.id)
        assert job.params["plan"]["ops"][0]["module_id"] == str(m.id)
        approval = db.get(ApprovalRequest, approval_id)
        assert approval.status == "approved"
        assert approval.result_ref == str(job_id)
    finally:
        db.close()

    assert ran == [job_id]            # the (monkeypatched) runner was scheduled
