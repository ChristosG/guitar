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
from sqlalchemy import func, select, text

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

    # `scope_module_id` is keyword-only and optional (module-scoped restructure,
    # 2026-08-07). The tool always forwards it, so a stub that omits it fails
    # with a TypeError dressed up as a planner error.
    def _fake_plan(db, root_id, *, instruction, scope_module_id=None):
        captured.update(root_id=root_id, instruction=instruction, scope_module_id=scope_module_id)
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
# Feature 2 — the revise overhaul: TARGETED RETRIEVAL grounding + LLM freedom
# ---------------------------------------------------------------------------

def test_plan_revision_grounds_via_ground_topic_not_the_whole_library(monkeypatch):
    """The planner retrieves passages for the INSTRUCTION topic (`ground_topic`) —
    the same BM25 + e5 path refine/draft use — and no longer reads the whole
    library/canon. `build_curriculum_context` is gone from the revise module, and
    the retrieved passages reach the planner prompt."""
    import app.curriculum.revise as revise
    from app.curriculum.ground import Passage

    # The whole-library context helper is no longer reachable from revise at all.
    assert not hasattr(revise, "build_curriculum_context")

    grounded = {}

    def _fake_ground(db, topic, *, source_ids=None, k=5):
        grounded.update(topic=topic, source_ids=source_ids, k=k)
        return [Passage(text="A retrieved passage about the DS-1 distortion pedal.",
                        source_id=uuid.uuid4(), source_title="Distortion Book",
                        page_no=7, page_id=uuid.uuid4(), score=0.9)]

    monkeypatch.setattr(revise, "ground_topic", _fake_ground)

    captured = {}

    class _P:
        def guided_json(self, messages, schema, **kw):
            captured["messages"] = messages
            return {"summary": "", "ops": []}

    monkeypatch.setattr(revise, "get_provider", lambda: _P())

    db = SessionLocal()
    try:
        course, m, lesson = _seed_tree(db)     # meta.source_ids is None
        revise.plan_revision(db, course.id, instruction="add a lesson on the DS-1")
    finally:
        db.close()

    # grounded on the INSTRUCTION topic, scoped to the course's own source_ids
    assert grounded["topic"] == "add a lesson on the DS-1"
    assert grounded["source_ids"] is None
    # the retrieved passage reached the planner prompt (grounding block in the tail)
    joined = "\n".join(msg["content"] for msg in captured["messages"])
    assert "A retrieved passage about the DS-1 distortion pedal." in joined
    assert "Distortion Book" in joined


def test_the_revise_planner_prompt_frees_the_llm_and_demands_citation_honesty():
    """The load-bearing directive (item c): the passages ground citations only; the
    model must use its full knowledge freely and NOT hedge/refuse when the library
    is thin — the one discipline being citation honesty (cite only where supported,
    never misattribute)."""
    from app.curriculum.revise import build_revise_messages

    msgs = build_revise_messages(
        course_title="Tone 101", brief=None, language="el",
        tree_text="M1 [x] Overdrive — od pedals", instruction="add distortion",
        retrieved="[Distortion Book, p.7] passage",
    )
    assert any(msg["role"] == "system" for msg in msgs)      # shared system, no whole-library prefix
    user = next(msg["content"] for msg in msgs if msg["role"] == "user")
    low = user.lower()
    assert "use your full knowledge" in low                  # freedom
    assert "do not narrow, hedge, or refuse" in low          # no refusal for thin library
    assert "grounding citations" in low                      # passages are for citations only
    assert "citation honesty" in low and "misattribute" in low
    # the retrieved block is present when passages were found
    assert "[Distortion Book, p.7] passage" in user

    # and absent (no dangling f-string placeholder) when retrieval found nothing
    empty = build_revise_messages(
        course_title="Tone 101", brief=None, language="el",
        tree_text="M1 [x] Overdrive — od pedals", instruction="add distortion",
        retrieved=None,
    )
    empty_user = next(msg["content"] for msg in empty if msg["role"] == "user")
    assert "{retrieved_block}" not in empty_user and "{retrieved}" not in empty_user


def test_propose_tool_description_is_scoped_to_change_requests_only():
    """Item a: the tool description tells the model to call the planner ONLY for
    add/change/remove/restructure requests, and to answer questions ABOUT the
    course directly instead."""
    desc = TOOLS["propose_curriculum_revision"].schema["function"]["description"].lower()
    assert "only when" in desc
    for verb in ("add", "change", "remove", "restructure"):
        assert verb in desc
    assert "do not call it to answer a question" in desc
    assert "directly from the curriculum tree" in desc


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


def test_validate_pending_revision_attaches_the_computed_impact():
    """Task 4: `plan["impact"]` is server-computed (`compute_impact`) off the
    VALIDATED ops — never the model's own claim — and lands on `pending`
    alongside the trimmed op list."""
    db = SessionLocal()
    try:
        course, m, lesson = _seed_tree(db)
        pending = {
            "tool_call_id": "call_1",
            "name": "apply_curriculum_revision",
            "arguments": {"root_id": str(course.id), "plan": {"summary": "s", "ops": [
                {"op": "insert_lesson", "module_id": str(m.id), "title": "DS-1",
                 "objective": "o", "reason": "keep"},
                {"op": "remove_lesson", "lesson_id": str(lesson.id), "reason": "cut"},
                {"op": "insert_lesson", "module_id": str(uuid.uuid4()),  # bogus, dropped
                 "title": "ghost", "objective": "x", "reason": "drop"},
            ]}},
        }
        _validate_pending_revision(db, pending)
        plan = pending["arguments"]["plan"]
        assert len(plan["ops"]) == 2                     # bogus op dropped BEFORE impact is computed
        assert plan["impact"] == {
            "rewrites": 0, "segment_additions": 0, "segment_edits": 0, "segment_removals": 0,
            "lesson_removals": 1, "lessons_added": 1, "blueprint_changed": False, "destructive": True,
        }
    finally:
        db.close()


def test_validate_pending_revision_impact_is_not_destructive_for_a_surgical_only_plan():
    db = SessionLocal()
    try:
        course, m, lesson = _seed_tree(db)
        pending = {
            "tool_call_id": "call_1",
            "name": "apply_curriculum_revision",
            "arguments": {"root_id": str(course.id), "plan": {"summary": "s", "ops": [
                {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Warm-up",
                 "instruction": "add a warm-up drill", "reason": "keep"},
            ]}},
        }
        _validate_pending_revision(db, pending)
        impact = pending["arguments"]["plan"]["impact"]
        assert impact["destructive"] is False
        assert impact["segment_additions"] == 1
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
        assert out.tool_args["plan"]["impact"]["lessons_added"] == 1   # and its computed impact
        assert out.tool_args["plan"]["impact"]["destructive"] is False
        approval = db.get(ApprovalRequest, out.approval_id)
        assert len(approval.tool_args["plan"]["ops"]) == 1   # stored plan is validated too
        assert approval.tool_args["plan"]["ops"][0]["module_id"] == str(m.id)
        assert approval.tool_args["plan"]["impact"]["lessons_added"] == 1  # stored impact too
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


# ---------------------------------------------------------------------------
# (d) whole-branch review finding #2 — edited_args bypasses the propose-time
# gate entirely (it REPLACES tool_args wholesale), so resolve_approval must
# re-run validate_ops on it before enqueueing, same discipline as
# _validate_pending_revision.
# ---------------------------------------------------------------------------

def test_resolve_approval_with_edited_args_drops_a_cross_course_remove_segment(monkeypatch):
    """A tutor-edited (or otherwise untrusted) `edited_args` plan carrying a
    `remove_segment` targeting a segment OUTSIDE this course must never reach
    apply — resolve_approval re-validates before enqueueing, same as the
    normal propose path, and the op is dropped rather than applied."""
    ran = []
    monkeypatch.setattr(chat_router, "run_curriculum_revise_job", lambda job_id: ran.append(job_id))

    db = SessionLocal()
    try:
        course, m, lesson = _seed_tree(db)
        other_course, other_m, other_lesson = _seed_tree(db)
        other_seg = Block(kind="segment", title="Other seg", body="x", order=0,
                          parent_id=other_lesson.id, language="el", meta={})
        db.add(other_seg)
        db.commit()

        session = ChatSession(locale="el", root_id=course.id)
        db.add(session)
        db.commit()
        session_id = session.id

        # The APPROVED (proposal-time) plan carries only the legitimate op.
        approval = ApprovalRequest(
            session_id=session_id, tool_name="apply_curriculum_revision",
            tool_args={"root_id": str(course.id), "plan": {"summary": "s", "ops": [
                {"op": "insert_lesson", "module_id": str(m.id), "title": "DS-1",
                 "objective": "o", "reason": "r"}]}},
            tool_call_id="call_1", status="pending",
        )
        db.add(approval)
        db.commit()
        approval_id = approval.id
        other_seg_id = other_seg.id
    finally:
        db.close()

    # The EDITED args a tutor submits on resolve smuggle in a cross-course
    # remove_segment alongside the legitimate op.
    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve",
        json={
            "decision": "approve",
            "edited_args": {"root_id": str(course.id), "plan": {"summary": "edited", "ops": [
                {"op": "insert_lesson", "module_id": str(m.id), "title": "DS-1",
                 "objective": "o", "reason": "r"},
                {"op": "remove_segment", "segment_id": str(other_seg_id), "reason": "delete it"},
            ]}},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "job_pending"
    job_id = uuid.UUID(body["job_id"])

    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        ops = job.params["plan"]["ops"]
        assert [o["op"] for o in ops] == ["insert_lesson"]   # remove_segment dropped
        # The cross-course segment is untouched.
        assert db.get(Block, other_seg_id) is not None
        approval = db.get(ApprovalRequest, approval_id)
        assert approval.status == "approved"
    finally:
        db.close()


def test_resolve_approval_with_edited_args_all_dropped_returns_422_and_never_enqueues(monkeypatch):
    """If the edit hollows the plan out to nothing (every op fails
    validate_ops), resolve_approval rejects with 422 rather than silently
    enqueueing a no-op job — the approval stays pending, nothing is applied."""
    ran = []
    monkeypatch.setattr(chat_router, "run_curriculum_revise_job", lambda job_id: ran.append(job_id))

    db = SessionLocal()
    try:
        course, m, lesson = _seed_tree(db)
        other_course, other_m, other_lesson = _seed_tree(db)
        other_seg = Block(kind="segment", title="Other seg", body="x", order=0,
                          parent_id=other_lesson.id, language="el", meta={})
        db.add(other_seg)
        db.commit()

        session = ChatSession(locale="el", root_id=course.id)
        db.add(session)
        db.commit()
        session_id = session.id

        approval = ApprovalRequest(
            session_id=session_id, tool_name="apply_curriculum_revision",
            tool_args={"root_id": str(course.id), "plan": {"summary": "s", "ops": [
                {"op": "insert_lesson", "module_id": str(m.id), "title": "DS-1",
                 "objective": "o", "reason": "r"}]}},
            tool_call_id="call_1", status="pending",
        )
        db.add(approval)
        db.commit()
        approval_id = approval.id
        other_seg_id = other_seg.id
        jobs_before = db.scalar(select(func.count()).select_from(GenerationJob))
    finally:
        db.close()

    r = client.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve",
        json={
            "decision": "approve",
            "edited_args": {"root_id": str(course.id), "plan": {"summary": "edited", "ops": [
                {"op": "remove_segment", "segment_id": str(other_seg_id), "reason": "delete it"},
            ]}},
        },
    )
    assert r.status_code == 422, r.text
    assert ran == []

    db = SessionLocal()
    try:
        assert db.get(Block, other_seg_id) is not None       # untouched
        approval = db.get(ApprovalRequest, approval_id)
        assert approval.status == "pending"                  # never advanced
        jobs_after = db.scalar(select(func.count()).select_from(GenerationJob))
        assert jobs_after == jobs_before                     # no job enqueued
    finally:
        db.close()
