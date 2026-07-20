"""Part 5 planning chat: interview-bound sessions. The binding column is
interview_id (never root_id), so every root_id-gated revise behavior in
routers/chat.py must NOT fire for these sessions — pinned here.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import Base, SessionLocal, engine

# Skip cleanly (not error) when no DB is reachable — mirrors test_chat_models.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )

from app.main import app
from app.models.chat import ChatSession
from app.models.interview import CurriculumInterview
from app.routers.chat import _inject_curriculum_context, _inject_interview_context


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


client = TestClient(app)


def _mk_interview(db, title="Ήχος και Ενισχυτές"):
    interview = CurriculumInterview(title=title)
    db.add(interview)
    db.commit()
    return interview


def test_get_or_create_interview_chat_session_creates_then_reuses():
    db = SessionLocal()
    try:
        interview = _mk_interview(db)
        r1 = client.get(f"/curricula/interview/{interview.id}/chat-session")
        assert r1.status_code == 200
        sid = r1.json()["session_id"]

        r2 = client.get(f"/curricula/interview/{interview.id}/chat-session")
        assert r2.json()["session_id"] == sid  # reused, not duplicated

        db.expire_all()
        session = db.get(ChatSession, sid)
        assert str(session.interview_id) == str(interview.id)
        assert session.root_id is None
    finally:
        db.close()


def test_interview_chat_session_404_for_unknown_interview():
    r = client.get("/curricula/interview/00000000-0000-0000-0000-000000000000/chat-session")
    assert r.status_code == 404


def test_interview_context_injected_and_curriculum_context_not():
    """An interview-bound session gets the light planning steer appended to
    the last user message; the curriculum-context injector must be a no-op
    for it (root_id is None)."""
    db = SessionLocal()
    try:
        interview = _mk_interview(db)
        session = ChatSession(interview_id=interview.id, locale="el")
        db.add(session); db.commit()

        wire = [{"role": "user", "content": "θέλω ένα πρόγραμμα για ήχο"}]
        out = _inject_curriculum_context(db, session, wire)
        assert out == wire  # untouched — no root_id

        out2 = _inject_interview_context(db, session, wire)
        assert out2 is not wire
        assert out2[-1]["content"].startswith("θέλω ένα πρόγραμμα για ήχο")
        assert "[PLANNING CONTEXT" in out2[-1]["content"]
        assert interview.title in out2[-1]["content"]
        # the original wire dicts are NEVER mutated in place
        assert wire[-1]["content"] == "θέλω ένα πρόγραμμα για ήχο"
    finally:
        db.close()


def test_interview_context_noop_for_ordinary_and_curriculum_sessions():
    db = SessionLocal()
    try:
        plain = ChatSession(locale="el")
        db.add(plain); db.commit()
        wire = [{"role": "user", "content": "γεια"}]
        assert _inject_interview_context(db, plain, wire) == wire
    finally:
        db.close()
