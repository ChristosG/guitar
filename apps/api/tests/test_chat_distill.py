"""Tests for `POST /chat/{session_id}/distill` — the revise chat's "talk it
through first" exit (2026-07-21). One cheap `guided_json` call distills the
conversation into ONE tutor-voiced revision instruction, returned for the
tutor to review and edit in his composer. Approve-before-spend: the endpoint
returns TEXT; it never plans, proposes, or applies anything.

Contract under test:
  * unknown session -> 404;
  * a session NOT bound to a curriculum -> 409 (the button only exists in the
    Revise panel, but the API guards it independently);
  * a bound session with no tutor turns -> 409, provider never called;
  * a bound session with a real back-and-forth -> 200 with the instruction,
    and the prompt the provider saw carries the transcript;
  * an empty instruction from the model -> 409, not a blank 200.

DB-touching but NO live LLM — the provider is a scripted fake patched onto
`app.curriculum.revise`'s own bound `get_provider` (the service module's
import, not the router's — mirrors `test_chat_suggestions.py`'s note on
patching the name the callee actually reads).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.curriculum.revise as revise_mod
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
from app.models.chat import Message

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )


def setup_module(_):
    Base.metadata.create_all(engine)


client = TestClient(app)


class _FakeGuidedJSON:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[list[dict]] = []

    def guided_json(self, messages, schema, *, temperature=0.2, role="spec", max_tokens=None):
        self.calls.append([dict(m) for m in messages])
        if self.error is not None:
            raise self.error
        return self.result


def _create_session(**overrides) -> str:
    r = client.post("/chat", json=overrides)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _seed_messages(session_id: str, turns: list[tuple[str, str]]) -> None:
    db = SessionLocal()
    try:
        for role, content in turns:
            db.add(Message(session_id=uuid.UUID(session_id), role=role, content=content))
        db.commit()
    finally:
        db.close()


def _seed_course() -> Block:
    db = SessionLocal()
    try:
        course = Block(
            kind="course", title="Tone 101", is_template=True, language="el",
            meta={"brief": None},
        )
        db.add(course)
        db.commit()
        db.refresh(course)
        return course
    finally:
        db.close()


def test_unknown_session_404s():
    r = client.post(f"/chat/{uuid.uuid4()}/distill")
    assert r.status_code == 404, r.text


def test_a_session_without_a_curriculum_409s(monkeypatch):
    fake = _FakeGuidedJSON(result={"instruction": "never reached"})
    monkeypatch.setattr(revise_mod, "get_provider", lambda: fake)

    sid = _create_session()
    _seed_messages(sid, [("user", "θέλω αλλαγές"), ("assistant", "ποιες;")])

    r = client.post(f"/chat/{sid}/distill")
    assert r.status_code == 409, r.text
    assert fake.calls == [], "an unbound session must never spend a call"


def test_no_tutor_turns_409s_without_calling_the_provider(monkeypatch):
    fake = _FakeGuidedJSON(result={"instruction": "never reached"})
    monkeypatch.setattr(revise_mod, "get_provider", lambda: fake)

    course = _seed_course()
    sid = _create_session(root_id=str(course.id))
    _seed_messages(sid, [("assistant", "Γεια! Τι θα ήθελες να αλλάξουμε;")])

    r = client.post(f"/chat/{sid}/distill")
    assert r.status_code == 409, r.text
    assert fake.calls == []


def test_a_real_conversation_distills_to_one_instruction(monkeypatch):
    fake = _FakeGuidedJSON(result={"instruction": "Πρόσθεσε ασκήσεις πεταλιών στο μάθημα 12."})
    monkeypatch.setattr(revise_mod, "get_provider", lambda: fake)

    course = _seed_course()
    sid = _create_session(root_id=str(course.id))
    _seed_messages(sid, [
        ("user", "Το μάθημα για τα πετάλια είναι ρηχό."),
        ("assistant", "Να προσθέσουμε ασκήσεις με αλυσίδα πεταλιών;"),
        ("user", "Ναι, ακριβώς αυτό."),
    ])

    r = client.post(f"/chat/{sid}/distill")
    assert r.status_code == 200, r.text
    assert r.json() == {"instruction": "Πρόσθεσε ασκήσεις πεταλιών στο μάθημα 12."}

    # The one call it spent read the ACTUAL conversation, both sides.
    assert len(fake.calls) == 1
    prompt = fake.calls[0][0]["content"]
    assert "Το μάθημα για τα πετάλια είναι ρηχό." in prompt
    assert "ASSISTANT:" in prompt and "USER:" in prompt


def test_an_empty_model_reply_is_a_409_not_a_blank_200(monkeypatch):
    fake = _FakeGuidedJSON(result={"instruction": "   "})
    monkeypatch.setattr(revise_mod, "get_provider", lambda: fake)

    course = _seed_course()
    sid = _create_session(root_id=str(course.id))
    _seed_messages(sid, [("user", "κάνε το καλύτερο")])

    r = client.post(f"/chat/{sid}/distill")
    assert r.status_code == 409, r.text
