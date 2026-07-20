"""Part 5 planning chat: interview-bound sessions. The binding column is
interview_id (never root_id), so every root_id-gated revise behavior in
routers/chat.py must NOT fire for these sessions — pinned here.
"""
from uuid import uuid4

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
        # the steer forbids ALL THREE curriculum-mutating tools, not just the
        # revise pair — a tutor saying "ωραία, φτιάξ' το" mid-planning-chat
        # must not get a generate_curriculum approval card that bypasses the
        # wizard and discards the still-unsaved brief.
        assert "Do NOT call generate_curriculum" in out2[-1]["content"]
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


def test_interview_context_yields_to_curriculum_when_both_ids_set():
    """A session bound to BOTH root_id and interview_id must get only the
    curriculum context — the interview steer is a no-op here."""
    db = SessionLocal()
    try:
        interview = _mk_interview(db)
        session = ChatSession(root_id=uuid4(), interview_id=interview.id, locale="el")
        db.add(session); db.commit()

        wire = [{"role": "user", "content": "γεια"}]
        assert _inject_interview_context(db, session, wire) == wire
    finally:
        db.close()


from app.models.chat import Message


def _mk_bound_session_with_transcript(db, interview):
    session = ChatSession(interview_id=interview.id, locale="el")
    db.add(session); db.flush()
    db.add(Message(session_id=session.id, role="user",
                   content="Θέλω 20 εβδομάδες για ήχο κιθάρας, έμφαση στην πράξη."))
    db.add(Message(session_id=session.id, role="assistant",
                   content="Προτείνω 4 ενότητες: μαγνήτες, ενισχυτές, ηχεία, πετάλια."))
    db.commit()
    return session


def test_distill_returns_brief_and_does_not_store(monkeypatch):
    db = SessionLocal()
    try:
        interview = _mk_interview(db)
        _mk_bound_session_with_transcript(db, interview)

        from app.curriculum import interview as interview_mod
        seen = {}

        class _FakeProvider:
            def guided_json(self, messages, schema, *, role=None, **kw):
                seen["messages"] = messages
                seen["role"] = role
                return {"brief": "Στόχος: 20 εβδομάδες, πρακτική έμφαση, 4 ενότητες."}

        monkeypatch.setattr(interview_mod, "get_provider", lambda: _FakeProvider())

        r = client.post(f"/curricula/interview/{interview.id}/distill")
        assert r.status_code == 200
        assert r.json()["brief"].startswith("Στόχος")
        assert seen["role"] == "chat"
        # the transcript reached the model
        joined = str(seen["messages"])
        assert "20 εβδομάδες" in joined and "4 ενότητες" in joined

        db.expire_all()
        assert db.get(CurriculumInterview, interview.id).planning_brief is None  # not stored yet
    finally:
        db.close()


def test_distill_falls_back_to_session_locale_when_who_is_empty(monkeypatch):
    """`who.language` is essentially always empty at distill time — planning
    chat runs BEFORE the "who" interview step. Without a fallback, distill
    silently used DEFAULT_LOCALE ("el") even for an English-locale tutor. The
    bound session's own locale (set from X-App-Locale when the planning chat
    session was created) must reach the prompt instead."""
    db = SessionLocal()
    try:
        interview = _mk_interview(db)
        session = ChatSession(interview_id=interview.id, locale="en")
        db.add(session); db.flush()
        db.add(Message(session_id=session.id, role="user",
                       content="I want 20 weeks on guitar tone, practical emphasis."))
        db.add(Message(session_id=session.id, role="assistant",
                       content="I suggest 4 modules: pickups, amps, speakers, pedals."))
        db.commit()
        # interview.answers has no "who" step at all — the common case here.
        assert not (interview.answers or {}).get("who")

        from app.curriculum import interview as interview_mod
        seen = {}

        class _FakeProvider:
            def guided_json(self, messages, schema, *, role=None, **kw):
                seen["messages"] = messages
                return {"brief": "Goal: 20 weeks, practical emphasis, 4 modules."}

        monkeypatch.setattr(interview_mod, "get_provider", lambda: _FakeProvider())

        r = client.post(f"/curricula/interview/{interview.id}/distill")
        assert r.status_code == 200

        joined = str(seen["messages"])
        # the shared language directive's English fragment, not the Greek default
        assert "write everything you produce in English (en)" in joined
    finally:
        db.close()


def test_distill_409_when_no_chat_or_empty_transcript():
    db = SessionLocal()
    try:
        interview = _mk_interview(db)
        r = client.post(f"/curricula/interview/{interview.id}/distill")
        assert r.status_code == 409
    finally:
        db.close()


def test_distill_maps_llm_error_to_status(monkeypatch):
    """A provider failure (rate limit, timeout, ...) must not escape as a raw
    500 — same taxonomy as routers/chat.py's post_message."""
    db = SessionLocal()
    try:
        interview = _mk_interview(db)
        _mk_bound_session_with_transcript(db, interview)

        from app.curriculum import interview as interview_mod
        from app.llm.errors import LLMError

        class _FailingProvider:
            def guided_json(self, messages, schema, *, role=None, **kw):
                raise LLMError("rate_limit", "429")

        monkeypatch.setattr(interview_mod, "get_provider", lambda: _FailingProvider())

        r = client.post(f"/curricula/interview/{interview.id}/distill")
        assert r.status_code == 429
    finally:
        db.close()


def test_distill_409_when_brief_is_empty(monkeypatch):
    """An all-whitespace brief is not a successful distillation — it must be
    retryable (409), not a silent 200 the tutor carries forward as blank
    text."""
    db = SessionLocal()
    try:
        interview = _mk_interview(db)
        _mk_bound_session_with_transcript(db, interview)

        from app.curriculum import interview as interview_mod

        class _BlankProvider:
            def guided_json(self, messages, schema, *, role=None, **kw):
                return {"brief": "  "}

        monkeypatch.setattr(interview_mod, "get_provider", lambda: _BlankProvider())

        r = client.post(f"/curricula/interview/{interview.id}/distill")
        assert r.status_code == 409
    finally:
        db.close()


def test_outline_prompt_byte_identical_without_planning_brief():
    """Spec §5 regression pin: no planning brief -> the outline prompt is
    EXACTLY today's. Same discipline as the blueprint byte-identity test."""
    from app.curriculum.corpus import LibraryContext
    from app.curriculum.outline import build_outline_messages
    from app.curriculum.shape import plan_shape

    kwargs = dict(
        title="Ήχος", brief="σύντομο", language="el",
        shape=plan_shape(4, 1, 60),
        library=LibraryContext(text="", token_count=0, fits=True),
        student_brief=None, gap_policy="general_knowledge",
    )
    baseline = build_outline_messages(**kwargs)
    with_default = build_outline_messages(**kwargs, planning_brief=None)
    assert baseline == with_default


def test_outline_prompt_includes_planning_brief_when_set():
    from app.curriculum.corpus import LibraryContext
    from app.curriculum.outline import build_outline_messages
    from app.curriculum.shape import plan_shape

    kwargs = dict(
        title="Ήχος", brief="σύντομο", language="el",
        shape=plan_shape(4, 1, 60),
        library=LibraryContext(text="", token_count=0, fits=True),
        student_brief=None, gap_policy="general_knowledge",
    )
    msgs = build_outline_messages(**kwargs, planning_brief="Έμφαση στην πράξη, όχι φυσική.")
    joined = str(msgs)
    assert "Έμφαση στην πράξη" in joined
    assert "PLANNING DISCUSSION" in joined  # the block header
    # and the scope brief still present alongside — the two blocks coexist
    assert "σύντομο" in joined


def test_generate_interview_outline_passes_planning_brief(monkeypatch):
    """The interview job seam: interview.planning_brief reaches generate_outline."""
    from app.curriculum import interview as interview_mod

    captured = {}

    def _fake_generate_outline(db, **kwargs):
        captured.update(kwargs)
        return {"modules": []}

    monkeypatch.setattr(interview_mod, "generate_outline", _fake_generate_outline)

    db = SessionLocal()
    try:
        interview = _mk_interview(db)
        interview.answers = {
            "duration": {"weeks": 8, "sessions_per_week": 1, "minutes_per_session": 50},
        }
        interview.planning_brief = "Πρακτική πρώτα."
        db.commit()
        interview_mod.generate_interview_outline(db, interview)
        assert captured.get("planning_brief") == "Πρακτική πρώτα."
    finally:
        db.close()


def test_put_planning_brief_stores_and_clears():
    db = SessionLocal()
    try:
        interview = _mk_interview(db)
        r = client.put(f"/curricula/interview/{interview.id}/planning-brief",
                       json={"brief": "  Τελικό σχέδιο: πρακτική πρώτα.  "})
        assert r.status_code == 204
        db.expire_all()
        assert db.get(CurriculumInterview, interview.id).planning_brief == "Τελικό σχέδιο: πρακτική πρώτα."

        r2 = client.put(f"/curricula/interview/{interview.id}/planning-brief", json={"brief": ""})
        assert r2.status_code == 204
        db.expire_all()
        assert db.get(CurriculumInterview, interview.id).planning_brief is None
    finally:
        db.close()
