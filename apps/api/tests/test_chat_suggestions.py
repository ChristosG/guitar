"""Tests for `POST /chat/{session_id}/suggestions` — the suggestion-chips
endpoint (chat overhaul, Piece B). A SEPARATE, lightweight `guided_json` call
from the turn itself: never the ReAct loop, never a tool, and never allowed
to surface as an error the tutor has to react to — every failure mode (no
messages yet, a provider error, an unusable reply) degrades to
`{"suggestions": []}`.

DB-touching (real Postgres, real `ChatSession`/`Message`/`Block` rows) but NO
live LLM — the provider is a scripted fake, monkeypatched onto `app.routers.
chat`'s own bound `get_provider` name (mirrors `test_chat_router.py`'s
`_use_provider` convention for `agent_loop.get_provider`; this endpoint calls
`get_provider()` directly rather than through `agent_loop`, so the patch
target is this module's own import).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.routers.chat as chat_router
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
from app.models.chat import ChatSession, Message

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
    """Scripted `get_provider()` stand-in exposing only `guided_json` — the
    one method this endpoint ever calls. Records every call's `messages` so
    a test can assert on the system prompt's content (e.g. curriculum
    context), same "spy + canned response" shape as `test_revise_chat.py`'s
    own inline fakes.
    """

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
    """Writes `Message` rows directly — no need to run the real agent loop
    just to give this endpoint a transcript to read.
    """
    db = SessionLocal()
    try:
        for role, content in turns:
            db.add(Message(session_id=uuid.UUID(session_id), role=role, content=content))
        db.commit()
    finally:
        db.close()


def _seed_course(**overrides) -> Block:
    db = SessionLocal()
    try:
        course = Block(
            kind="course", title=overrides.pop("title", "Tone 101"),
            is_template=True, language="el", meta={"brief": None},
        )
        db.add(course)
        db.commit()
        db.refresh(course)
        return course
    finally:
        db.close()


# ---------------------------------------------------------------------------
# unknown session -> 404
# ---------------------------------------------------------------------------

def test_unknown_session_404s():
    r = client.post(f"/chat/{uuid.uuid4()}/suggestions")
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# no messages yet -> [] without ever calling the provider
# ---------------------------------------------------------------------------

def test_no_messages_yet_returns_empty_and_never_calls_the_provider(monkeypatch):
    fake = _FakeGuidedJSON(result={"suggestions": ["should never be reached"]})
    monkeypatch.setattr(chat_router, "get_provider", lambda: fake)
    session_id = _create_session()

    r = client.post(f"/chat/{session_id}/suggestions")

    assert r.status_code == 200, r.text
    assert r.json() == {"suggestions": []}
    assert fake.calls == []


# ---------------------------------------------------------------------------
# a real transcript -> up to 3 constrained suggestions
# ---------------------------------------------------------------------------

def test_returns_up_to_3_suggestions_and_caps_a_longer_reply(monkeypatch):
    fake = _FakeGuidedJSON(result={
        "suggestions": [
            "Add a lesson on legato",
            "Explain sweep picking",
            "  ",  # blank — must be dropped, not rendered as an empty chip
            "Extra fourth suggestion",
        ],
    })
    monkeypatch.setattr(chat_router, "get_provider", lambda: fake)
    session_id = _create_session()
    _seed_messages(session_id, [
        ("user", "what should I teach next?"),
        ("assistant", "Consider legato technique."),
    ])

    r = client.post(f"/chat/{session_id}/suggestions")

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["suggestions"]) == 3
    assert body["suggestions"] == [
        "Add a lesson on legato",
        "Explain sweep picking",
        "Extra fourth suggestion",
    ]
    assert len(fake.calls) == 1
    # The transcript reached the model — the seeded exchange, not an empty prompt.
    call = fake.calls[0]
    assert call[0]["role"] == "system"
    assert call[1]["role"] == "user"
    assert "legato" in call[1]["content"]


def test_non_string_or_missing_suggestions_key_degrades_to_empty(monkeypatch):
    fake = _FakeGuidedJSON(result={"suggestions": "not a list"})
    monkeypatch.setattr(chat_router, "get_provider", lambda: fake)
    session_id = _create_session()
    _seed_messages(session_id, [("user", "hi"), ("assistant", "hello")])

    r = client.post(f"/chat/{session_id}/suggestions")

    assert r.status_code == 200, r.text
    assert r.json() == {"suggestions": []}


# ---------------------------------------------------------------------------
# a provider error -> [] gracefully, never a 4xx/5xx
# ---------------------------------------------------------------------------

def test_provider_error_degrades_to_empty_list_gracefully(monkeypatch):
    fake = _FakeGuidedJSON(error=RuntimeError("boom"))
    monkeypatch.setattr(chat_router, "get_provider", lambda: fake)
    session_id = _create_session()
    _seed_messages(session_id, [("user", "hi"), ("assistant", "hello")])

    r = client.post(f"/chat/{session_id}/suggestions")

    assert r.status_code == 200, r.text
    assert r.json() == {"suggestions": []}


# ---------------------------------------------------------------------------
# curriculum-bound session (revise drawer) -> the course's title reaches the prompt
# ---------------------------------------------------------------------------

def test_curriculum_bound_session_gets_curriculum_context_in_the_prompt(monkeypatch):
    course = _seed_course(title="Blues Foundations")
    fake = _FakeGuidedJSON(result={"suggestions": ["Add a module on turnarounds"]})
    monkeypatch.setattr(chat_router, "get_provider", lambda: fake)
    session_id = _create_session(root_id=str(course.id))
    _seed_messages(session_id, [
        ("user", "what's missing from this course?"),
        ("assistant", "It could use more on turnarounds."),
    ])

    r = client.post(f"/chat/{session_id}/suggestions")

    assert r.status_code == 200, r.text
    assert r.json() == {"suggestions": ["Add a module on turnarounds"]}
    system_prompt = fake.calls[0][0]["content"]
    assert "Blues Foundations" in system_prompt


def test_ordinary_global_chat_session_has_no_curriculum_context_in_the_prompt(monkeypatch):
    fake = _FakeGuidedJSON(result={"suggestions": []})
    monkeypatch.setattr(chat_router, "get_provider", lambda: fake)
    session_id = _create_session()  # no root_id
    _seed_messages(session_id, [("user", "hi"), ("assistant", "hello")])

    client.post(f"/chat/{session_id}/suggestions")

    # The base prompt mentions "curriculum" in passing (the conditional
    # clause allowing a revision suggestion IF one is bound) — what must be
    # ABSENT here is the bound-course block itself, appended only when
    # `session.root_id` is set (see `_suggestions_system_prompt`).
    system_prompt = fake.calls[0][0]["content"]
    assert "revising the curriculum" not in system_prompt.lower()
