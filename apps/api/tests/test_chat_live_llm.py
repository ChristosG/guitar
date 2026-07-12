"""Live-LLM integration test for the chat agent (Plan 5 Task 6) — THE
moment-of-truth verification that the REAL model, given the REAL ~13-tool
roster (`app.agent.tools.TOOLS`) and the real, deliberately short/tool-first
`SYSTEM_PROMPT` (`app.agent.prompts` — see its own docstring on agentic-
gotchas.md #1/#2), still reliably tool-calls correctly rather than having
tool-calling SUPPRESSED by the size of the roster/prompt. That suppression
risk is exactly what Plan 5's recon flagged before this plan was even
written (`.superpowers/sdd/progress.md`'s "PLAN 5 PRE-DESIGN" section) —
every other agent test in this suite (`test_agent_loop.py`, `test_agent_
hitl.py`, `test_chat_router.py`, `test_qwen_chat_tools.py`) drives a
SCRIPTED fake provider and therefore cannot catch it; this module is the
first (and only) one in the whole repo that drives the real model through
`POST /chat/{id}/messages` with every tool visible, exactly as production
will.

DB-touching (real Postgres `guitar_test`, forced by `conftest.py` before any
`app.*` import) AND hits the live vLLM chat model — marked
`@pytest.mark.integration` (that marker means "hits live vLLM" per
`pyproject.toml`), same convention `test_curriculum_api.py`'s own
`test_generate_curriculum_endpoint_returns_real_tree` established (the
brief's named precedent for this task). Host runs additionally need
`LLM_BASE_URL=http://localhost:6888/v1 EMBED_BASE_URL=http://localhost:8090/v1`
exported — `app.config.Settings`' defaults are the container-internal
hostnames (`qwen-vllm`/`qwen-emb-vllm`), only resolvable from inside
`platform-net`; see `docs/superpowers/plans/2026-07-07-knowledge-brain.md`'s
"DB access in tests" note for this exact host-vs-container split.

Deliberately does NOT monkeypatch `app.agent.loop.get_provider` (unlike
every other chat test in this suite) — that IS the point: a scripted fake
would prove nothing about whether the real 9B model actually proposes tools
correctly at this roster size.

Two turns, mirroring the brief exactly:
  - a READ intent ("what removes hum from a guitar pickup") — grounded via a
    seeded `KnowledgeSource` (same seed-then-ask shape as
    `test_curriculum_api.py`'s own domain-tagged live-generation test) so a
    `search_knowledge`/`explain_concept` call, if the model makes one, has
    something real to retrieve. Hard-asserts the turn ends "answer" (never
    "awaiting_approval" — the model must not hallucinate a mutation for a
    question) with genuinely on-topic content; separately inspects the
    persisted transcript to REPORT (not gate on) which tool, if any, the
    model actually called — the brief's own "either/or" allows either path.
  - a MUTATION intent ("add a new student named Maria Ioannou, beginner
    level") — hard-asserts the turn suspends: `status == "awaiting_approval"`,
    `tool_name == "create_student"`, and plausible `tool_args` (the proposed
    name substring-matches both name parts). Also asserts the mutation fn
    was NEVER actually invoked (no Student row exists) — suspend-before-
    execute is the entire point of the HITL gate this task re-verifies.

A third turn (Plan 6 Task 6's own brief) re-runs the identical proof for one
of the THREE tools this task wired into the very same registry the two turns
above already drive the full roster of: "make a note that Maria struggled
with barre chords today" -> `status == "awaiting_approval"`,
`tool_name == "add_note"`, plausible `tool_args` (title/body plausibly
mention Maria/barre chords), and — same suspend-before-execute proof as the
`create_student` turn — no `Note` row exists yet.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.brain.ingest import IngestPayload, ingest_source
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
from app.models.chat import ApprovalRequest, Message
from app.models.knowledge import KnowledgeSource
from app.models.note import Note
from app.models.student import Student

# Skip cleanly (not error) when no DB is reachable — mirrors test_curriculum_api.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


client = TestClient(app)

_HUM_TEXT = (
    "A humbucker pickup uses two coils wired in reverse electrical polarity "
    "and reverse magnetic polarity relative to each other. Mains hum and "
    "other electromagnetic interference induce an equal noise signal in both "
    "coils; because the coils are reverse-wound and reverse-polarity, that "
    "induced hum cancels out when the two coil signals are combined. The "
    "guitar string's true signal, unlike the hum, is captured in phase by "
    "both coils and does not cancel, so it survives. This is why "
    "humbucker-equipped guitars are much quieter than single-coil guitars in "
    "electrically noisy rooms full of computer monitors or dimmer switches."
)
# Generous/on-topic, not exact-wording — the model may paraphrase, especially
# if it answers from its own pretrained knowledge rather than the retrieved
# chunk (both are acceptable per the brief's own "either/or" framing).
_HUM_KEYWORDS = (
    "hum", "coil", "cancel", "humbucker", "polarity", "noise",
    "interference", "wound", "winding", "single-coil", "phase",
)


def _create_session() -> str:
    r = client.post("/chat", json={})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _db_messages(session_id: str) -> list[Message]:
    db = SessionLocal()
    try:
        return list(
            db.query(Message)
            .filter(Message.session_id == uuid.UUID(session_id))
            .order_by(Message.created_at)
            .all()
        )
    finally:
        db.close()


@pytest.mark.integration
def test_read_intent_answers_without_hallucinating_a_mutation():
    """Grounds the read-tool path in a real seeded source, then asks a
    classic read-intent question through the FULL real roster + real prompt.
    """
    domain = f"hum-live-{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    try:
        source = KnowledgeSource(
            type="text", title="Humbucker Hum Cancellation", language="en", domain=domain,
        )
        db.add(source)
        db.commit()
        ingest_source(db, source.id, IngestPayload(kind="text", text=_HUM_TEXT))
        db.refresh(source)
        assert source.status == "ready", f"seed ingestion failed: {source.status}/{source.error}"
    finally:
        db.close()

    session_id = _create_session()
    r = client.post(
        f"/chat/{session_id}/messages",
        json={"content": "What removes hum from a guitar pickup?"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Core assertion: no hallucinated mutation for a plain question.
    assert body["status"] == "answer", (
        f"expected a plain answer, model instead proposed/suspended on a mutation: {body}"
    )
    assert body["content"], "model returned an empty answer"

    content_lower = body["content"].lower()
    matched_keywords = [kw for kw in _HUM_KEYWORDS if kw in content_lower]
    assert matched_keywords, (
        f"answer doesn't look on-topic for a humbucker/hum question: {body['content']!r}"
    )

    # Informational (not gating — the brief's own assertion is an "either/or"):
    # which tool, if any, did the model actually call along the way?
    rows = _db_messages(session_id)
    called_tools = sorted({
        call["function"]["name"]
        for row in rows if row.role == "assistant" and row.tool_calls
        for call in row.tool_calls
    })
    print(f"\n[live-llm read-intent] tool(s) called: {called_tools or 'NONE (answered directly)'}")
    print(f"[live-llm read-intent] matched keywords: {matched_keywords}")
    print(f"[live-llm read-intent] answer: {body['content']!r}")


@pytest.mark.integration
def test_mutation_intent_suspends_for_approval_with_the_right_tool():
    """The core proof the model proposes the RIGHT tool at the full roster
    and the router actually suspends rather than executing — approve-before-
    execute's whole reason to exist. Never resolves the approval (this test
    is about the PROPOSE+SUSPEND behavior only); separately confirms the
    mutation fn was genuinely never invoked (no Student row exists).
    """
    session_id = _create_session()
    r = client.post(
        f"/chat/{session_id}/messages",
        json={"content": "Add a new student named Maria Ioannou, beginner level"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    print(
        f"\n[live-llm mutation-intent] status={body['status']!r} "
        f"tool_name={body.get('tool_name')!r} tool_args={body.get('tool_args')!r} "
        f"description={body.get('description')!r}"
    )

    assert body["status"] == "awaiting_approval", (
        f"expected the turn to suspend for approval, got: {body}"
    )
    assert body["tool_name"] == "create_student", (
        f"model proposed the wrong tool for a roster-add request: {body['tool_name']!r}"
    )
    assert body["approval_id"]

    tool_args = body["tool_args"]
    name_arg = str(tool_args.get("name", "")).lower()
    assert "maria" in name_arg and "ioannou" in name_arg, (
        f"proposed create_student args don't plausibly match the request: {tool_args}"
    )

    # Suspended, not executed: the ApprovalRequest is pending, and no Student
    # row exists yet — proving the router gated the mutation rather than
    # running it inline.
    db = SessionLocal()
    try:
        approval = db.get(ApprovalRequest, uuid.UUID(body["approval_id"]))
        assert approval is not None
        assert approval.status == "pending"
        assert approval.tool_name == "create_student"

        maria_rows = db.query(Student).filter(Student.name.ilike("%Maria%")).all()
        assert maria_rows == [], (
            f"create_student fn must NOT run before approval, but found: {maria_rows}"
        )
    finally:
        db.close()


@pytest.mark.integration
def test_note_intent_suspends_for_approval_with_the_right_tool():
    """Plan 6 Task 6's own live-LLM proof: same shape as the `create_student`
    turn above, for one of the three tools this task added into the SAME
    ~16-tool roster (6 read + 10 mutation) — confirming the registry's growth
    since Task 6 of Plan 5 didn't suppress tool-calling for a NEW tool either.
    """
    session_id = _create_session()
    r = client.post(
        f"/chat/{session_id}/messages",
        json={"content": "Make a note that Maria struggled with barre chords today"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    print(
        f"\n[live-llm note-intent] status={body['status']!r} "
        f"tool_name={body.get('tool_name')!r} tool_args={body.get('tool_args')!r} "
        f"description={body.get('description')!r}"
    )

    assert body["status"] == "awaiting_approval", (
        f"expected the turn to suspend for approval, got: {body}"
    )
    assert body["tool_name"] == "add_note", (
        f"model proposed the wrong tool for a make-a-note request: {body['tool_name']!r}"
    )
    assert body["approval_id"]

    tool_args = body["tool_args"]
    text_blob = " ".join(str(v) for v in tool_args.values()).lower()
    assert "maria" in text_blob, f"proposed add_note args don't mention Maria: {tool_args}"
    assert "barre" in text_blob, f"proposed add_note args don't mention barre chords: {tool_args}"

    # Suspended, not executed: the ApprovalRequest is pending, and no Note row
    # exists yet — proving the router gated the mutation rather than running it.
    db = SessionLocal()
    try:
        approval = db.get(ApprovalRequest, uuid.UUID(body["approval_id"]))
        assert approval is not None
        assert approval.status == "pending"
        assert approval.tool_name == "add_note"

        note_rows = db.query(Note).all()
        assert note_rows == [], (
            f"add_note fn must NOT run before approval, but found: {note_rows}"
        )
    finally:
        db.close()


@pytest.mark.integration
def test_split_session_intent_suspends_for_approval_with_the_right_tool():
    """Plan 10 Task 3's own live-LLM proof — the whole reason this task
    exists: Chris says "split session 2, it's too long" and the model must
    actually propose split_session (not another tool, not prose), and the
    router must still suspend rather than execute (B5 — every lesson tool is
    kind="mutation", same suspend-before-execute proof as the two turns
    above).

    Seeds a real 2-session lesson directly (no LLM call needed for the seed
    itself — draft_lesson_from_selection is a separate, already-covered
    concern) and gives the model the lesson's root id in the user message,
    as if the UI had just shown it. This lets the model call get_curriculum
    itself (a READ, executed inline, same turn) to discover session 2's real
    id before proposing split_session (the MUTATION, which suspends) —
    exercising the exact "read-then-mutate in one turn" path `test_agent_
    hitl.py`'s scripted-fake tests cover in isolation, but here against the
    real model + real roster.
    """
    db = SessionLocal()
    try:
        lesson = Block(
            kind="lesson", title="Barre Chords", language="en",
            is_template=False, plane="content",
        )
        db.add(lesson)
        db.flush()

        session1 = Block(
            kind="session", title="Session 1: Warm-up", order=0, parent_id=lesson.id,
            language="en", is_template=False, plane="content", est_minutes=15,
        )
        db.add(session1)
        db.flush()
        db.add(Block(
            kind="item", title="Finger stretches", order=0, parent_id=session1.id,
            language="en", is_template=False, plane="content", est_minutes=15,
        ))

        session2 = Block(
            kind="session", title="Session 2: Barre technique", order=1, parent_id=lesson.id,
            language="en", is_template=False, plane="content", est_minutes=90,
        )
        db.add(session2)
        db.flush()
        for i, minutes in enumerate([30, 30, 30]):
            db.add(Block(
                kind="item", title=f"Barre drill {i + 1}", order=i, parent_id=session2.id,
                language="en", is_template=False, plane="content", est_minutes=minutes,
            ))
        db.commit()
        lesson_id = lesson.id
        session2_id = session2.id
    finally:
        db.close()

    session_id = _create_session()
    r = client.post(
        f"/chat/{session_id}/messages",
        json={
            "content": (
                f"Here is a lesson id: {lesson_id}. Look up its sessions, "
                "then split session 2 into roughly 30-minute sessions — "
                "it's too long."
            ),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    print(
        f"\n[live-llm split-intent] status={body['status']!r} "
        f"tool_name={body.get('tool_name')!r} tool_args={body.get('tool_args')!r} "
        f"description={body.get('description')!r}"
    )

    assert body["status"] == "awaiting_approval", (
        f"expected the turn to suspend for approval, got: {body}"
    )
    assert body["tool_name"] == "split_session", (
        f"model proposed the wrong tool for a split-session request: {body['tool_name']!r}"
    )
    assert body["approval_id"]

    tool_args = body["tool_args"]
    assert str(tool_args.get("session_id")) == str(session2_id), (
        f"model proposed splitting the wrong session (expected session 2, "
        f"{session2_id}): {tool_args}"
    )

    # Suspended, not executed: the ApprovalRequest is pending, and session
    # 2's own 3 original items are still exactly where they were seeded —
    # proving the router gated the mutation rather than running it.
    db = SessionLocal()
    try:
        approval = db.get(ApprovalRequest, uuid.UUID(body["approval_id"]))
        assert approval is not None
        assert approval.status == "pending"
        assert approval.tool_name == "split_session"

        untouched_session = db.get(Block, session2_id)
        assert untouched_session is not None, "split_session fn must NOT run before approval"
        items = db.scalars(select(Block).where(Block.parent_id == session2_id)).all()
        assert len(items) == 3, (
            f"split_session fn must NOT run before approval, but session 2's "
            f"items were already re-parented: {items}"
        )
    finally:
        db.close()
