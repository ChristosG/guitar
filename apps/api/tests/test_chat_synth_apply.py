"""Task 0.7 — a COMPUTED revision plan always reaches the tutor as a card.

Live evidence (2026-09-12 09:53, webapp, claude_cli, session 97f38a82): the
tutor asked for the remaining modules to be rewritten, `propose_curriculum_
revision` ran for 307 s and came back with 7 `edit_segment` ops — and the
model's final hop NARRATED the plan («Ετοίμασα το πλάνο αναθεώρησης — …»)
instead of calling `apply_curriculum_revision`. The turn ended `answer`,
`GET /chat/{id}/pending` returned null, and the tutor was left reading a
description of a plan he could not apply. Silent failure S5.

The fix lives in `_respond_to_turn` (the ONE place every turn's outcome is
shaped — sync, the `chat_turn` job, and resume-after-resolve): when an
`answer` turn's new tail carries a `propose_curriculum_revision` result with
a non-empty `ops`, the router SYNTHESIZES the apply call the model omitted,
hangs it off the tail's last assistant row (so the transcript keeps the exact
shape a model-emitted suspend produces) and returns the approval card.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.routers.chat as chat_router
from app.agent.loop import AgentResult
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
from app.models.chat import ApprovalRequest, Message

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


client = TestClient(app)


def _seed_tree():
    """A real course/module/lesson so `validate_ops` keeps the ops instead of
    dropping them as unknown ids."""
    db = SessionLocal()
    try:
        course = Block(kind="course", title="Tone 101", is_template=True, language="el",
                       meta={"brief": "for gigging players", "source_ids": None})
        db.add(course)
        db.flush()
        module = Block(kind="module", title="Overdrive", parent_id=course.id, order=0,
                       language="el", meta={"objective": "od pedals"})
        db.add(module)
        db.flush()
        lesson = Block(kind="lesson", title="Tube Screamer", parent_id=module.id, order=0,
                       language="el", meta={"objective": "ts basics"})
        db.add(lesson)
        db.commit()
        return str(course.id), str(module.id), str(lesson.id)
    finally:
        db.close()


NARRATION = "Ετοίμασα το πλάνο αναθεώρησης — ξαναγράφει τις υπόλοιπες ενότητες…"


def _narrated_plan_turn(root_id: str, plan_content: str, *, scope_module_id: str | None = None):
    """A turn that runs `propose_curriculum_revision`, gets `plan_content`
    back as the tool result, and then just TALKS about it — exactly the live
    shape: `status="answer"`, no `pending_tool`."""
    args = {"root_id": root_id, "instruction": "ξαναγράψε τις υπόλοιπες ενότητες"}
    if scope_module_id:
        args["scope_module_id"] = scope_module_id

    def fake(db, wire, **kw):
        tail = [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_propose_1", "type": "function", "function": {
                    "name": "propose_curriculum_revision",
                    "arguments": json.dumps(args, ensure_ascii=False)}}]},
            {"role": "tool", "tool_call_id": "call_propose_1", "content": plan_content},
            {"role": "assistant", "content": NARRATION},
        ]
        return AgentResult(status="answer", content=NARRATION,
                           messages=[*wire, *tail], citations=[])
    return fake


def _plan_json(module_id: str, n: int = 2) -> str:
    ops = [{"op": "insert_lesson", "module_id": module_id, "title": f"DS-{i}",
            "objective": "στόχος", "reason": "γιατί"} for i in range(1, n + 1)]
    return json.dumps({"summary": "ξαναγραφή", "ops": ops, "dropped": []},
                      ensure_ascii=False)


def _new_session(root_id: str) -> str:
    return client.post("/chat", json={"locale": "el", "root_id": root_id}).json()["session_id"]


# ---------------------------------------------------------------------------
# The fix itself
# ---------------------------------------------------------------------------

def test_a_narrated_plan_becomes_an_approval_card(monkeypatch):
    root_id, module_id, _ = _seed_tree()
    monkeypatch.setattr(chat_router, "run_agent_turn",
                        _narrated_plan_turn(root_id, _plan_json(module_id, 2)))
    session = _new_session(root_id)

    r = client.post(f"/chat/{session}/messages", json={"content": "ξαναγράψε τις υπόλοιπες"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "awaiting_approval"
    assert body["tool_name"] == "apply_curriculum_revision"
    assert body["tool_args"]["root_id"] == root_id
    assert len(body["tool_args"]["plan"]["ops"]) == 2          # validated, nothing dropped
    assert body["tool_args"]["plan"]["impact"]["lessons_added"] == 2
    assert body["description"] == NARRATION

    # The card is discoverable exactly like a model-emitted suspend's.
    pending = client.get(f"/chat/{session}/pending").json()
    assert pending and pending["tool_name"] == "apply_curriculum_revision"
    assert pending["id"] == body["approval_id"]

    # And the transcript carries the synthetic call on its last assistant row,
    # so the wire stays valid once `resolve_approval` answers it.
    db = SessionLocal()
    try:
        rows = db.query(Message).filter_by(session_id=uuid.UUID(session)).order_by(
            Message.created_at, Message.id).all()
        last_assistant = [m for m in rows if m.role == "assistant"][-1]
        approval = db.get(ApprovalRequest, uuid.UUID(body["approval_id"]))
        assert last_assistant.content == NARRATION
        assert last_assistant.tool_calls[0]["function"]["name"] == "apply_curriculum_revision"
        assert last_assistant.tool_calls[0]["id"] == approval.tool_call_id
        synth_args = json.loads(last_assistant.tool_calls[0]["function"]["arguments"])
        assert synth_args["root_id"] == root_id
        assert len(synth_args["plan"]["ops"]) == 2
    finally:
        db.close()


def test_the_propose_scope_module_id_rides_along(monkeypatch):
    """A module-scoped propose must stay module-scoped when its apply is
    synthesized — otherwise the card silently widens the blast radius."""
    root_id, module_id, _ = _seed_tree()
    monkeypatch.setattr(chat_router, "run_agent_turn",
                        _narrated_plan_turn(root_id, _plan_json(module_id, 1),
                                            scope_module_id=module_id))
    session = _new_session(root_id)
    body = client.post(f"/chat/{session}/messages", json={"content": "μόνο αυτή"}).json()
    assert body["status"] == "awaiting_approval"
    assert body["tool_args"]["scope_module_id"] == module_id


def test_an_empty_plan_stays_a_plain_answer(monkeypatch):
    root_id, module_id, _ = _seed_tree()
    empty = json.dumps({"summary": "τίποτα", "ops": [], "dropped": []}, ensure_ascii=False)
    monkeypatch.setattr(chat_router, "run_agent_turn", _narrated_plan_turn(root_id, empty))
    session = _new_session(root_id)

    body = client.post(f"/chat/{session}/messages", json={"content": "ε"}).json()
    assert body["status"] == "answer"
    assert body["content"] == NARRATION
    assert client.get(f"/chat/{session}/pending").json() is None


def test_a_truncated_plan_result_stays_a_plain_answer(monkeypatch):
    """`loop._stringify` caps a tool result at 60K chars; a cut result is no
    longer valid JSON. Better a plain answer than a card built on a guess."""
    root_id, module_id, _ = _seed_tree()
    truncated = _plan_json(module_id, 2)[:120] + "\n…[το αποτέλεσμα περικόπηκε]"
    monkeypatch.setattr(chat_router, "run_agent_turn", _narrated_plan_turn(root_id, truncated))
    session = _new_session(root_id)

    body = client.post(f"/chat/{session}/messages", json={"content": "ε"}).json()
    assert body["status"] == "answer"
    assert client.get(f"/chat/{session}/pending").json() is None


def test_a_plan_error_result_stays_a_plain_answer(monkeypatch):
    """`_propose_curriculum_revision` degrades to `{"error": ...}` — valid
    JSON, no `ops`. Nothing to approve."""
    root_id, _m, _l = _seed_tree()
    err = json.dumps({"error": "invalid root_id"}, ensure_ascii=False)
    monkeypatch.setattr(chat_router, "run_agent_turn", _narrated_plan_turn(root_id, err))
    session = _new_session(root_id)

    body = client.post(f"/chat/{session}/messages", json={"content": "ε"}).json()
    assert body["status"] == "answer"
    assert client.get(f"/chat/{session}/pending").json() is None


def test_a_genuine_answer_turn_is_untouched(monkeypatch):
    """No propose in the tail — the ordinary answer path must not change."""
    root_id, _m, _l = _seed_tree()

    def plain(db, wire, **kw):
        return AgentResult(status="answer", content="γεια",
                           messages=[*wire, {"role": "assistant", "content": "γεια"}],
                           citations=[])
    monkeypatch.setattr(chat_router, "run_agent_turn", plain)
    session = _new_session(root_id)
    body = client.post(f"/chat/{session}/messages", json={"content": "ε"}).json()
    assert body["status"] == "answer" and body["content"] == "γεια"
    assert client.get(f"/chat/{session}/pending").json() is None


# ---------------------------------------------------------------------------
# The transcript the synthesis leaves behind must survive a resolve
# ---------------------------------------------------------------------------

def test_a_synthesized_card_can_be_rejected_and_the_session_keeps_going(monkeypatch):
    root_id, module_id, _ = _seed_tree()
    monkeypatch.setattr(chat_router, "run_agent_turn",
                        _narrated_plan_turn(root_id, _plan_json(module_id, 2)))
    session = _new_session(root_id)
    body = client.post(f"/chat/{session}/messages", json={"content": "ξαναγράψε"}).json()
    approval_id = body["approval_id"]

    # The resume after a reject is an ordinary answer turn.
    seen = {}

    def after_reject(db, wire, **kw):
        seen["wire"] = wire
        return AgentResult(status="answer", content="Εντάξει, το ακυρώνω.",
                           messages=[*wire, {"role": "assistant", "content": "Εντάξει, το ακυρώνω."}],
                           citations=[])
    monkeypatch.setattr(chat_router, "run_agent_turn", after_reject)

    r = client.post(f"/chat/{session}/approvals/{approval_id}/resolve",
                    json={"decision": "reject"})
    assert r.status_code == 200 and r.json()["status"] == "answer"

    # Every assistant tool_call in the rebuilt wire is answered by a later
    # tool row — the invariant a model-emitted suspend already satisfies.
    wire = seen["wire"]
    answered = {m["tool_call_id"] for m in wire if m.get("role") == "tool"}
    for i, m in enumerate(wire):
        for call in (m.get("tool_calls") or []):
            assert call["id"] in answered, f"dangling tool_call {call['id']} at {i}"

    # No 409 left behind: the next message is accepted.
    assert client.get(f"/chat/{session}/pending").json() is None
    r2 = client.post(f"/chat/{session}/messages", json={"content": "και τώρα;"})
    assert r2.status_code == 200


def test_a_synthesized_card_approves_into_a_curriculum_revise_job(monkeypatch):
    ran = []
    monkeypatch.setattr(chat_router, "run_curriculum_revise_job", lambda job_id: ran.append(job_id))
    root_id, module_id, _ = _seed_tree()
    monkeypatch.setattr(chat_router, "run_agent_turn",
                        _narrated_plan_turn(root_id, _plan_json(module_id, 2)))
    session = _new_session(root_id)
    body = client.post(f"/chat/{session}/messages", json={"content": "ξαναγράψε"}).json()

    r = client.post(f"/chat/{session}/approvals/{body['approval_id']}/resolve",
                    json={"decision": "approve"})
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "job_pending" and out["job_id"]
    assert ran == [uuid.UUID(out["job_id"])]


def test_the_async_job_turn_synthesizes_too(monkeypatch):
    """The `chat_turn` job runs the same `_respond_to_turn`, so the drawer's
    long revise turn gets the card on its `progress["turn"]` too."""
    root_id, module_id, _ = _seed_tree()
    monkeypatch.setattr(chat_router, "run_agent_turn",
                        _narrated_plan_turn(root_id, _plan_json(module_id, 2)))
    session = _new_session(root_id)
    r = client.post(f"/chat/{session}/messages?async=1", json={"content": "ξαναγράψε"})
    assert r.status_code == 202
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "succeeded"
    assert job["progress"]["turn"]["status"] == "awaiting_approval"
    assert job["progress"]["turn"]["tool_name"] == "apply_curriculum_revision"
    pending = client.get(f"/chat/{session}/pending").json()
    assert pending and pending["tool_name"] == "apply_curriculum_revision"
