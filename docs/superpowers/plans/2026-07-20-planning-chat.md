# Planning Chat (Part 5 — "Συζήτησέ το πρώτα") Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An optional conversational step at the start of the curriculum-generation interview: the tutor chats with the grounded copilot to shape the course idea, then one LLM call distills the transcript into an **editable Greek brief** that is stored on the interview and injected into outline generation. Skipping it is byte-identical to today's flow.

**Architecture:** Spec §5 of `docs/superpowers/specs/2026-07-20-curriculum-control-design.md` (approved). A new nullable `ChatSession.interview_id` binds a chat to a `CurriculumInterview` (the FK-less pointer precedent of `root_id`/`student_id`); because every revise-chat behavior in `routers/chat.py` gates on `session.root_id`, an interview session gets none of it by construction. The distilled brief lands in `CurriculumInterview.planning_brief` and threads into `build_outline_messages` as its own prompt block beside the existing `course_brief_block`. The distillation prompt is tutor-editable via the prompt registry (the registry's call-site completeness test *enforces* registration).

**Tech Stack:** FastAPI/SQLAlchemy/Alembic/pytest (apps/api); Next.js/next-intl/Playwright (apps/web); `get_provider().guided_json(..., role="chat")`.

## Global Constraints

- Default locale is Greek (`el`); the brief must come out in the interview's language (`interview.answers["who"]["language"]`, default `el`); every new UI string needs el + en entries in `apps/web/src/messages/{el,en}.json`, Greek primary.
- **Byte-identity:** with no `planning_brief`, the outline prompt assembly must be BYTE-IDENTICAL to today — pinned by a regression test (same discipline as the blueprint default).
- New provider call sites MUST be registered in `apps/api/app/prompts/registry.py` with exact `call_sites` line pins — `tests/test_prompts_registry.py::test_registry_covers_every_provider_call_site` counts them per file and fails otherwise (currently 15 sites).
- Migrations: hand-written additive `ADD COLUMN NULL` (copy `alembic/versions/e5f6a7b8c9d0_chat_session_root_id.py`), `down_revision = "f1a2b3c4d5e6"` (current head).
- Backend tests: `cd apps/api && .venv/bin/python -m pytest <files> -q` (guitar_test Postgres on 5434; conftest pins DATABASE_URL; `create_all` picks up new model columns automatically). Tests named `*live*` fail with ConnectError in this environment — pre-existing, ignore.
- Frontend checks: `cd apps/web && npx tsc --noEmit && npm run build`; Playwright starts its own webServer with a mocked API — 3 pre-existing failures in confirm.spec.ts are known; the set must not grow.
- The distillation call runs synchronously on the request path (like the suggestions endpoint) — it is short; do NOT make it a background job. Use `role="chat"` (no thinking, 8k tokens), never `plan`/`draft`.
- Commit after every task: `feat(interview): ...` / `feat(chat): ...` style.

---

### Task 1: Migration + model columns

**Files:**
- Create: `apps/api/alembic/versions/a1b2c3d4e5f6_planning_chat_columns.py`
- Modify: `apps/api/app/models/chat.py` (ChatSession — add `interview_id` after `root_id`, ~line 81)
- Modify: `apps/api/app/models/interview.py` (CurriculumInterview — add `planning_brief` near `brief`/`outline`)
- Test: `apps/api/tests/test_chat_models.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ChatSession.interview_id: uuid.UUID | None` and `CurriculumInterview.planning_brief: str | None` — Tasks 2–4 depend on these exact names.

- [ ] **Step 1: Write the failing test** — append to `apps/api/tests/test_chat_models.py`:

```python
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
```

(Check `test_chat_models.py`'s existing imports — `SessionLocal` and `ChatSession` are likely already imported; `CurriculumInterview`'s constructor may need other kwargs — adjust ONLY constructor kwargs, mirroring how `tests/test_curricula_manage.py` constructs it.)

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/api && .venv/bin/python -m pytest tests/test_chat_models.py -q`
Expected: FAIL — `TypeError: 'interview_id' is an invalid keyword argument` (or AttributeError for `planning_brief`).

- [ ] **Step 3: Implement.** In `apps/api/app/models/chat.py`, directly after `root_id` (follow the file's mapped_column style):

```python
    # `interview_id` (Part 5, planning chat) BINDS this conversation to one
    # CurriculumInterview — the pre-generation planning discussion. Same
    # deliberate not-a-ForeignKey reasoning as `root_id`/`student_id` above:
    # the transcript outlives the interview row. Mutually exclusive with
    # `root_id` in practice (a session is bound to a curriculum OR an
    # interview, never both) but not DB-enforced — every root_id-gated
    # behavior in routers/chat.py simply doesn't fire for these sessions,
    # which is exactly what a pre-generation chat needs.
    interview_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
```

In `apps/api/app/models/interview.py`, near `brief`:

```python
    # The distilled-and-TUTOR-EDITED planning brief (Part 5) — what the
    # planning chat concluded, in the tutor's own (edited) words. Nullable:
    # the planning chat is optional, and byte-identity of the outline prompt
    # without it is pinned by test. Injected into build_outline_messages as
    # its own block, beside `brief` (the scope step's one-liner) — see
    # app/curriculum/outline.py.
    planning_brief: Mapped[str | None] = mapped_column(Text, nullable=True)
```

(Confirm `Text` is imported in the module; add to the existing sqlalchemy import if not.)

Create `apps/api/alembic/versions/a1b2c3d4e5f6_planning_chat_columns.py` — copy the structure of `e5f6a7b8c9d0_chat_session_root_id.py` including its docstring rationale style:

```python
"""planning chat columns (Part 5): chat_session.interview_id + curriculum_interview.planning_brief

Hand-written additive ADD COLUMN NULL (safe mid-flight; runs in the api
container CMD at boot), not autogenerated — see e5f6a7b8c9d0 for why.
"""
import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_session", sa.Column("interview_id", sa.Uuid(as_uuid=True), nullable=True))
    op.add_column("curriculum_interview", sa.Column("planning_brief", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("curriculum_interview", "planning_brief")
    op.drop_column("chat_session", "interview_id")
```

(Verify the real table names on the models' `__tablename__` before committing; verify current alembic head is still `f1a2b3c4d5e6` with `ls apps/api/alembic/versions/` — if a new head appeared, chain to it.)

- [ ] **Step 4: Run tests**

Run: `cd apps/api && .venv/bin/python -m pytest tests/test_chat_models.py tests/test_curricula_manage.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/alembic/versions/a1b2c3d4e5f6_planning_chat_columns.py apps/api/app/models/chat.py apps/api/app/models/interview.py apps/api/tests/test_chat_models.py
git commit -m "feat(interview): planning-chat columns — ChatSession.interview_id + CurriculumInterview.planning_brief"
```

---

### Task 2: Interview chat-session endpoint + light interview-context steering

**Files:**
- Modify: `apps/api/app/routers/curriculum.py` (new route near `get_or_create_curriculum_chat_session`, ~line 545)
- Modify: `apps/api/app/routers/chat.py` (add `_inject_interview_context`, call it beside `_inject_curriculum_context` at both call sites ~line 621/682)
- Test: `apps/api/tests/test_planning_chat.py` (new)

**Interfaces:**
- Consumes: Task 1's columns; `_get_interview_or_404` (`routers/curriculum.py:140`); `ChatSessionCreated` schema (the root chat-session route returns it — reuse).
- Produces: `GET /curricula/interview/{interview_id}/chat-session` → `{"session_id": UUID}` (get-or-create, most-recent-first, exactly mirroring the root version at `curriculum.py:545-583`). `_inject_interview_context(db, session, wire) -> list[dict]` in chat.py. Task 5's frontend wrapper calls this endpoint.

- [ ] **Step 1: Write the failing tests** — `apps/api/tests/test_planning_chat.py`:

```python
"""Part 5 planning chat: interview-bound sessions. The binding column is
interview_id (never root_id), so every root_id-gated revise behavior in
routers/chat.py must NOT fire for these sessions — pinned here."""
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models.chat import ChatSession
from app.models.interview import CurriculumInterview
from app.routers.chat import _inject_curriculum_context, _inject_interview_context

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
```

(As in Task 1: fix `CurriculumInterview` constructor kwargs only if required.)

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/api && .venv/bin/python -m pytest tests/test_planning_chat.py -q`
Expected: FAIL — ImportError on `_inject_interview_context` / 404→405 on the route.

- [ ] **Step 3: Implement.**

3a. In `apps/api/app/routers/curriculum.py`, next to the root chat-session route (mirror its shape, docstring style, and most-recent-first select — read `curriculum.py:545-583` and keep the two textually parallel):

```python
@router.get("/curricula/interview/{interview_id}/chat-session", response_model=ChatSessionCreated)
def get_or_create_interview_chat_session(
    interview_id: UUID, request: Request, db: Session = Depends(get_db)
) -> dict:
    """The planning chat's session (Part 5) — one per interview, get-or-create,
    exactly the shape of the curriculum revise-drawer's route above. Bound via
    ChatSession.interview_id (never root_id: no course Block exists yet), so
    none of routers/chat.py's root_id-gated revise behaviors apply."""
    _get_interview_or_404(db, interview_id)
    session = db.scalars(
        select(ChatSession)
        .where(ChatSession.interview_id == interview_id)
        .order_by(ChatSession.created_at.desc())
    ).first()
    if session is None:
        session = ChatSession(interview_id=interview_id, locale=_locale_from(request))
        db.add(session)
        db.commit()
    return {"session_id": session.id}
```

**Adapt to the real neighbors:** the root route's locale handling (an `X-App-Locale` read — find its exact helper/parameter shape at `curriculum.py:545-583` and copy it verbatim; if it uses a `Header` param instead of `Request`, do the same), its response model name, and the file's import list. IMPORTANT — route ordering: this path starts with the literal `interview` segment, and FastAPI tries routes in registration order; register it next to the other `/curricula/interview/...` routes (~line 160-186) OR next to the root chat-session route — either works because `/curricula/{root_id}/chat-session` requires `root_id: UUID` and "interview" is not a UUID; confirm with the tests.

3b. In `apps/api/app/routers/chat.py`, directly after `_inject_curriculum_context` (~line 290):

```python
def _inject_interview_context(db: Session, session: ChatSession, wire: list[dict]) -> list[dict]:
    """Part 5: the planning chat's light steer — same transient tail-append
    contract as `_inject_curriculum_context` above (new list, new dict, never
    persisted, re-applied every turn), but deliberately MINIMAL: a title and a
    role, no tree (nothing is materialized yet) and no revise-tool steering
    (there is no root_id to revise)."""
    if not getattr(session, "interview_id", None):
        return wire
    interview = db.get(CurriculumInterview, session.interview_id)
    if interview is None:
        return wire
    ctx = (
        f"\n\n[PLANNING CONTEXT — the tutor is planning a NEW course titled "
        f"\"{interview.title}\" that does not exist yet. Help him think it "
        f"through: goals, topics, emphasis, sequencing, what to avoid. Ground "
        f"answers in his library where relevant. Do NOT call "
        f"propose_curriculum_revision or apply_curriculum_revision — there is "
        f"no curriculum to revise yet.]"
    )
    wire = list(wire)
    for i in range(len(wire) - 1, -1, -1):
        if wire[i].get("role") == "user":
            wire[i] = {**wire[i], "content": (wire[i].get("content") or "") + ctx}
            break
    return wire
```

Import `CurriculumInterview` following the file's import style. Then call it at BOTH message endpoints, immediately after the existing injection (`chat.py` ~line 621 and ~682):

```python
    wire = _inject_curriculum_context(db, session, wire)
    wire = _inject_interview_context(db, session, wire)
```

(The two injectors are mutually exclusive by data — root_id vs interview_id — so ordering between them is irrelevant; keep this order for readability. The guard fix from Part 1-4 already ensures injected context never reaches the named-song guard.)

- [ ] **Step 4: Run tests**

Run: `cd apps/api && .venv/bin/python -m pytest tests/test_planning_chat.py tests/test_chat_router.py tests/test_chat_stream_router.py -q -k "not live"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/routers/curriculum.py apps/api/app/routers/chat.py apps/api/tests/test_planning_chat.py
git commit -m "feat(interview): interview-bound chat session + light planning-context steer"
```

---

### Task 3: Distillation endpoint + tutor-editable prompt + brief storage

**Files:**
- Modify: `apps/api/app/curriculum/interview.py` (constants + distill function)
- Modify: `apps/api/app/routers/curriculum.py` (two routes)
- Modify: `apps/api/app/schemas/curriculum.py` (request/response models)
- Modify: `apps/api/app/prompts/registry.py` (new PromptEntry + call_site pin)
- Test: `apps/api/tests/test_planning_chat.py` (append), `tests/test_prompts_registry.py` (must pass unchanged — it enforces the registration)

**Interfaces:**
- Consumes: Task 1's `planning_brief` column, Task 2's session binding; `get_provider().guided_json(messages, schema, role="chat")`; `resolve_text` (`app.prompts.overrides.resolve` — check how chat.py imports it); `_ordered_messages`/message loading (see how the suggestions endpoint reads a session's transcript in `routers/chat.py`).
- Produces:
  - `POST /curricula/interview/{interview_id}/distill` → `{"brief": str}` (does NOT store — the tutor edits first).
  - `PUT /curricula/interview/{interview_id}/planning-brief` body `{"brief": str}` → 204 (stores the tutor-approved text; empty string clears to None).
  - `DISTILL_SYSTEM` constant + `DISTILL_SLICE_ID = "interview.planning_distill"` in `app/curriculum/interview.py`; `distill_planning_brief(db, interview) -> str`.

- [ ] **Step 1: Write the failing tests** — append to `apps/api/tests/test_planning_chat.py`:

```python
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


def test_distill_409_when_no_chat_or_empty_transcript():
    db = SessionLocal()
    try:
        interview = _mk_interview(db)
        r = client.post(f"/curricula/interview/{interview.id}/distill")
        assert r.status_code == 409
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
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/api && .venv/bin/python -m pytest tests/test_planning_chat.py -q`
Expected: new tests FAIL (404/405 — routes missing).

- [ ] **Step 3: Implement.**

3a. In `apps/api/app/curriculum/interview.py` (top-level, near the other constants; confirm/extend imports for `get_provider` and the overrides resolver following how `outline.py` does it):

```python
# Part 5: transcript -> editable brief, ONE cheap guided_json call
# (role="chat": no thinking, non-streaming — same tier as chat.suggestions,
# and for the same reason: a short synchronous summarization on the request
# path, well inside the Cloudflare edge cap). Tutor-editable via the registry
# (interview.planning_distill); {language} and {transcript} are filled by
# distill_planning_brief — the tutor's edit is the TEMPLATE, same contract as
# chat.suggestions.
DISTILL_SYSTEM = (
    "You read a planning conversation between a guitar TUTOR and an "
    "assistant about a course the tutor wants to create. Distill what the "
    "TUTOR actually wants into a brief IN {language}, written as if the "
    "tutor wrote it himself, structured as short lines under these headers: "
    "goals, topics to cover, emphasis/priorities, teaching preferences, "
    "things to avoid. Keep ONLY conclusions the tutor stated or clearly "
    "agreed to — dead ends and rejected ideas stay out. No preamble, no "
    "commentary; return ONLY the JSON the schema describes.\n\n"
    "THE CONVERSATION:\n{transcript}"
)
DISTILL_SLICE_ID = "interview.planning_distill"

DISTILL_SCHEMA = {
    "type": "object",
    "properties": {"brief": {"type": "string"}},
    "required": ["brief"],
    "additionalProperties": False,
}


def distill_planning_brief(db, interview) -> str:
    """The planning chat's exit: transcript in, tutor-voiced brief out.
    Raises ValueError when there is nothing to distill (no bound session or
    no user turns) — the router maps that to a 409 the UI can explain."""
    from app.models.chat import ChatSession, Message  # local: avoid cycles if any

    session = db.scalars(
        select(ChatSession)
        .where(ChatSession.interview_id == interview.id)
        .order_by(ChatSession.created_at.desc())
    ).first()
    if session is None:
        raise ValueError("no planning chat session for this interview")
    messages = db.scalars(
        select(Message).where(Message.session_id == session.id).order_by(Message.created_at)
    ).all()
    visible = [m for m in messages if m.role in ("user", "assistant") and (m.content or "").strip()]
    if not any(m.role == "user" for m in visible):
        raise ValueError("planning chat has no tutor turns to distill")

    transcript = "\n".join(f"{m.role.upper()}: {m.content.strip()}" for m in visible)
    who = (interview.answers or {}).get("who") or {}
    language = "Greek" if (who.get("language") or "el") == "el" else "English"

    prompt = resolve_text(db, DISTILL_SLICE_ID, DISTILL_SYSTEM).format(
        language=language, transcript=transcript,
    )
    result = get_provider().guided_json(
        [{"role": "user", "content": prompt}], DISTILL_SCHEMA, role="chat",
    )
    return (result.get("brief") or "").strip()
```

**Adapt:** the overrides resolver import/name (`outline.py` spells it `resolve`, `chat.py` aliases `resolve_text` — match this file's neighbors or import `resolve` from `app.prompts.overrides` and call it that); the transcript loader (if `routers/chat.py` has a reusable `_ordered_messages(db, session_id)`, prefer importing/duplicating per the codebase's "small deliberate duplication" precedent — do NOT import a `_`-private from another module, copy the two-line query instead, as the codebase does elsewhere); how `answers["who"]["language"]` is normalized (read `interview.py:337` and use the same accessor).

3b. Schemas in `apps/api/app/schemas/curriculum.py`:

```python
class PlanningBriefIn(BaseModel):
    brief: str = Field(max_length=20000)


class PlanningBriefOut(BaseModel):
    brief: str
```

3c. Routes in `apps/api/app/routers/curriculum.py` beside the other interview routes:

```python
@router.post("/curricula/interview/{interview_id}/distill", response_model=PlanningBriefOut)
def distill_interview_planning_brief(interview_id: UUID, db: Session = Depends(get_db)) -> dict:
    """Distill the planning chat into an EDITABLE brief. Deliberately does
    NOT store: the tutor reviews/edits first, then PUT planning-brief saves
    the approved text — approve-before-spend, the input-side mirror of the
    revise engine's approve-before-apply."""
    interview = _get_interview_or_404(db, interview_id)
    try:
        brief = distill_planning_brief(db, interview)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"brief": brief}


@router.put("/curricula/interview/{interview_id}/planning-brief", status_code=204, response_model=None)
def put_interview_planning_brief(
    interview_id: UUID, payload: PlanningBriefIn, db: Session = Depends(get_db)
) -> None:
    interview = _get_interview_or_404(db, interview_id)
    interview.planning_brief = payload.brief.strip() or None
    db.commit()
```

(Import `distill_planning_brief` and the new schemas following the file's style. If the interview routes use a `Depends(require_llm_configured)` guard for provider-dispatching endpoints — check how `/curricula/generate` does it — add it to the distill route.)

3d. Registry entry in `apps/api/app/prompts/registry.py` — model it on the verbatim `chat.suggestions` entry (`registry.py:1220-1257`): `id="interview.planning_distill"`, `flow="curriculum"`, `kind="prompt"`, `source_ref="app/curriculum/interview.py:<line of DISTILL_SYSTEM>"`, Greek `title_el` («Η απόσταξη της συζήτησης σχεδιασμού»), `what_it_does_el`/`when_it_runs_el` (real Greek sentences: what the distillation does — reads the planning chat and writes the tutor's brief; when — when the tutor presses «Χρησιμοποίησε αυτό το σχέδιο» in the wizard), `source_of_truth=lambda: DISTILL_SYSTEM`, a `build` sample function (mirror `_build_chat_suggestions`'s shape: render with a small fake transcript), `call_sites=("curriculum/interview.py:<line of the guided_json call>",)`, and one `Slice(id=DISTILL_SLICE_ID, default=DISTILL_SYSTEM, kind="replace", label_el="Το κείμενο της οδηγίας")`. **Get the two line pins from the actual committed file** (`grep -n "DISTILL_SYSTEM = \|guided_json" app/curriculum/interview.py`) — `tests/test_prompts_registry.py` verifies both against the source and counts every provider call site (this task adds the 16th).

- [ ] **Step 4: Run tests**

Run: `cd apps/api && .venv/bin/python -m pytest tests/test_planning_chat.py tests/test_prompts_registry.py -q`
Expected: PASS — including the registry's call-site count and pin checks.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/curriculum/interview.py apps/api/app/routers/curriculum.py apps/api/app/schemas/curriculum.py apps/api/app/prompts/registry.py apps/api/tests/test_planning_chat.py
git commit -m "feat(interview): transcript->brief distillation (tutor-editable prompt, approve-before-store)"
```

---

### Task 4: Thread the brief into outline generation (with byte-identity pin)

**Files:**
- Modify: `apps/api/app/curriculum/outline.py` (new block constant + param through `generate_outline` → `build_outline_messages`)
- Modify: `apps/api/app/curriculum/interview.py` (`generate_interview_outline` passes `planning_brief=interview.planning_brief`, ~line 545-554)
- Test: `apps/api/tests/test_planning_chat.py` (append)

**Interfaces:**
- Consumes: Task 1's `planning_brief` column; the existing `course_brief_block` pattern (`outline.py:157` template, `:193` format).
- Produces: `build_outline_messages(..., planning_brief: str | None = None)` and `generate_outline(..., planning_brief: str | None = None)` — keyword-only, default None. The non-interview path (`generate_curriculum`) is NOT changed (its callers never have a planning chat).

- [ ] **Step 1: Write the failing tests** — append to `apps/api/tests/test_planning_chat.py`:

```python
def test_outline_prompt_byte_identical_without_planning_brief():
    """Spec §5 regression pin: no planning brief -> the outline prompt is
    EXACTLY today's. Same discipline as the blueprint byte-identity test."""
    from app.curriculum.outline import build_outline_messages

    kwargs = dict(
        title="Ήχος", brief="σύντομο", language="el",
        shape={"weeks": 4, "sessions_per_week": 1, "minutes_per_session": 60},
        library=None, student_brief=None, gap_policy="general_knowledge",
    )
    # >>> ADAPT kwargs to build_outline_messages' REAL signature (read
    # outline.py:161) — every arg identical between the two calls is the point.
    baseline = build_outline_messages(**kwargs)
    with_default = build_outline_messages(**kwargs, planning_brief=None)
    assert baseline == with_default


def test_outline_prompt_includes_planning_brief_when_set():
    from app.curriculum.outline import build_outline_messages

    kwargs = dict(
        title="Ήχος", brief="σύντομο", language="el",
        shape={"weeks": 4, "sessions_per_week": 1, "minutes_per_session": 60},
        library=None, student_brief=None, gap_policy="general_knowledge",
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
    # >>> ADAPT: match generate_interview_outline's actual call/return contract
    # (read interview.py:524-560) — stub at the interview module's import site.
    monkeypatch.setattr(interview_mod, "generate_outline", _fake_generate_outline)

    db = SessionLocal()
    try:
        interview = _mk_interview(db)
        interview.planning_brief = "Πρακτική πρώτα."
        # >>> ADAPT: set whatever answers/step state generate_interview_outline
        # requires (who/duration/scope/structure/sources) — mirror an existing
        # test of generate_interview_outline if one exists (grep tests/ for it).
        db.commit()
        interview_mod.generate_interview_outline(db, interview)
        assert captured.get("planning_brief") == "Πρακτική πρώτα."
    finally:
        db.close()
```

The `>>> ADAPT` markers are instructions to the implementer, not shippable comments: resolve them against the real signatures BEFORE the first test run, then delete the markers. If `build_outline_messages` requires a non-None `library` (a `LibraryContext`), construct the minimal one the existing outline tests use — `grep -rn "build_outline_messages" tests/` and mirror.

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/api && .venv/bin/python -m pytest tests/test_planning_chat.py -q -k outline`
Expected: FAIL — `unexpected keyword argument 'planning_brief'`.

- [ ] **Step 3: Implement.** In `apps/api/app/curriculum/outline.py`:

3a. Next to `OUTLINE_COURSE_BRIEF_BLOCK` (~line 157), following its exact formatting convention:

```python
# Part 5: the planning chat's distilled-and-tutor-edited conclusions. A
# SEPARATE block from OUTLINE_COURSE_BRIEF_BLOCK (the scope step's one-liner):
# the two answer different questions — "what course is this" vs "what did the
# tutor and the assistant conclude when they talked it through" — and the
# model weighs an explicit agreed plan differently from a one-line wish.
# Empty string when absent — byte-identity without a brief is pinned by test.
OUTLINE_PLANNING_BRIEF_BLOCK = (
    "WHAT THE TUTOR CONCLUDED IN THE PLANNING DISCUSSION (edited and approved "
    "by him — follow it closely where it is specific):\n{planning_brief}"
)
```

3b. `build_outline_messages(...)` (~line 161): add keyword-only `planning_brief: str | None = None`; build `planning_brief_block` exactly the way `course_brief_block` is built from `brief` (same empty-string-when-absent mechanics, same separator/joiner — read `:161-200` and replicate); interpolate it into `OUTLINE_TAIL`'s format directly AFTER the course-brief block. **If `OUTLINE_TAIL` has no placeholder for it**, follow how `student_brief_block` was added (it is the precedent for a second optional block) — most likely a `{planning_brief_block}` placeholder in `OUTLINE_TAIL` (~line 131) that renders to `""` when absent. CAUTION: `OUTLINE_TAIL` is registry-managed (`curriculum.outline` entry, slice default) — adding a placeholder changes the constant, so: (i) keep the change minimal, (ii) run `tests/test_prompts_registry.py` — if a baseline/span test pins the old text, update per that test's own documented update procedure (it will name it), and (iii) note that tutors with a STORED override of `curriculum.outline` won't have the new placeholder — `.format` must therefore use a defaulting mechanism that tolerates a missing `{planning_brief_block}` key... **it does not**: `str.format` raises on missing keys only, not extra ones — an old override lacking the placeholder formats fine (extra kwarg ignored). Verify that direction explicitly in a quick REPL check and say so in the report.

3c. `generate_outline(...)` (~line 263): accept and pass through `planning_brief`.

3d. `apps/api/app/curriculum/interview.py` `generate_interview_outline` (~line 545-554): add `planning_brief=interview.planning_brief` to the `generate_outline(...)` call.

- [ ] **Step 4: Run tests**

Run: `cd apps/api && .venv/bin/python -m pytest tests/test_planning_chat.py tests/test_prompts_registry.py -q` plus whatever outline test files exist (`ls tests/ | grep -i outline`) — all green.
Expected: PASS, including byte-identity.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/curriculum/outline.py apps/api/app/curriculum/interview.py apps/api/tests/test_planning_chat.py
git commit -m "feat(interview): planning brief threads into outline generation (byte-identical when absent)"
```

---

### Task 5: Wizard frontend — the planning phase

**Files:**
- Modify: `apps/web/src/components/curriculum/interview-dialog.tsx` (new `"planning"` phase between intro and interview)
- Create: `apps/web/src/components/curriculum/planning-chat.tsx`
- Modify: `apps/web/src/lib/api.ts` (three wrappers)
- Modify: `apps/web/src/messages/el.json`, `en.json`
- Test: `apps/web/tests/interview.spec.ts` (extend: intro shows the planning button; skipping keeps today's flow)

**Interfaces:**
- Consumes: Task 2's `GET /curricula/interview/{id}/chat-session`, Task 3's `POST .../distill` + `PUT .../planning-brief`; `ChatPanel` (`sessionId` prop ONLY — no `rootId`, so no revise behavior), `ChatSessionsProvider` wrapper (both per `revise-drawer.tsx:240-259`); `startInterview` (`interview-dialog.tsx:136`).
- Produces: the tutor-facing flow — intro gains a secondary button «Θέλεις να το συζητήσουμε πρώτα;» which starts the interview AND enters the planning phase; the phase shows the chat, a «Χρησιμοποίησε αυτό το σχέδιο» action (distill → editable textarea → «Συνέχεια στον οδηγό» saves + proceeds), and a «Παράλειψη» that proceeds without saving. The primary intro button is unchanged (skipping = today's flow).

- [ ] **Step 1: api.ts wrappers** (beside `getOrCreateCurriculumChatSession`, matching its exact shape at `api.ts:1526`):

```ts
export function getOrCreateInterviewChatSession(interviewId: string): Promise<{ session_id: string }> {
  return request<{ session_id: string }>(`/curricula/interview/${interviewId}/chat-session`);
}

export function distillInterviewBrief(interviewId: string): Promise<{ brief: string }> {
  return request<{ brief: string }>(`/curricula/interview/${interviewId}/distill`, { method: "POST" });
}

export function putInterviewPlanningBrief(interviewId: string, brief: string): Promise<void> {
  return request<void>(`/curricula/interview/${interviewId}/planning-brief`, {
    method: "PUT",
    body: JSON.stringify({ brief }),
  });
}
```

(Return-shape note: if `getOrCreateCurriculumChatSession` unwraps to a string or a named type, mirror it exactly.)

- [ ] **Step 2: The planning phase component** — `apps/web/src/components/curriculum/planning-chat.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Loader2, MessageCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ChatPanel } from "@/components/chat/chat-panel";
import { ChatSessionsProvider } from "@/components/chat/chat-sessions-context";
import {
  ApiError, distillInterviewBrief, getOrCreateInterviewChatSession, putInterviewPlanningBrief,
} from "@/lib/api";

interface PlanningChatProps {
  interviewId: string;
  onDone: () => void; // proceed to the interview steps (with or without a saved brief)
}

/** Part 5 — «Συζήτησέ το πρώτα»: chat -> distill -> EDIT -> save -> proceed.
 * The brief is an artifact BETWEEN the chat and the generator: the tutor
 * audits what the machine understood before generation spends time and money
 * (the input-side mirror of the revise engine's approve-before-apply). */
export function PlanningChat({ interviewId, onDone }: PlanningChatProps) {
  const t = useTranslations("curricula.planning");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [phase, setPhase] = useState<"chat" | "edit">("chat");
  const [brief, setBrief] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getOrCreateInterviewChatSession(interviewId)
      .then((r) => setSessionId(r.session_id))
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("sessionError")));
  }, [interviewId, t]);

  const handleDistill = async () => {
    setBusy(true); setError(null);
    try {
      const r = await distillInterviewBrief(interviewId);
      setBrief(r.brief);
      setPhase("edit");
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 409
          ? t("distillEmpty")
          : err instanceof ApiError ? err.detail : t("distillError"),
      );
    } finally {
      setBusy(false);
    }
  };

  const handleSaveAndContinue = async () => {
    setBusy(true); setError(null);
    try {
      await putInterviewPlanningBrief(interviewId, brief);
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("saveError"));
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col gap-3" data-testid="planning-chat">
      {phase === "chat" && (
        <>
          <p className="text-sm text-muted-foreground">{t("chatHint")}</p>
          <div className="min-h-0 flex-1">
            {sessionId ? (
              <ChatSessionsProvider>
                <ChatPanel key={sessionId} sessionId={sessionId} />
              </ChatSessionsProvider>
            ) : (
              !error && <Loader2 className="animate-spin" aria-label={t("loading")} />
            )}
          </div>
          <div className="flex items-center justify-between gap-2">
            <Button type="button" variant="ghost" disabled={busy}
                    data-testid="planning-skip" onClick={onDone}>
              {t("skip")}
            </Button>
            <Button type="button" disabled={busy || !sessionId}
                    data-testid="planning-distill" onClick={handleDistill}>
              {busy ? <Loader2 className="animate-spin" /> : <MessageCircle />}
              {t("usePlan")}
            </Button>
          </div>
        </>
      )}
      {phase === "edit" && (
        <>
          <p className="text-sm text-muted-foreground">{t("editHint")}</p>
          <Textarea value={brief} onChange={(e) => setBrief(e.target.value)}
                    rows={14} disabled={busy} data-testid="planning-brief-editor" />
          <div className="flex items-center justify-between gap-2">
            <Button type="button" variant="outline" disabled={busy} onClick={() => setPhase("chat")}>
              {t("backToChat")}
            </Button>
            <Button type="button" disabled={busy || !brief.trim()}
                    data-testid="planning-continue" onClick={handleSaveAndContinue}>
              {busy && <Loader2 className="animate-spin" />}
              {t("continueToWizard")}
            </Button>
          </div>
        </>
      )}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
```

**Adapt to the real components before writing:** `ChatPanel`'s minimal-props render (verify `sessionId`-only works — read `chat-panel.tsx:51-75`; if a required prop exists, supply its neutral value); the `ChatSessionsProvider` import path (find it via `revise-drawer.tsx`'s imports); whether `Textarea` exists in `components/ui` (it does — used by block-card).

- [ ] **Step 3: Wire the phase into `interview-dialog.tsx`.** Phase union becomes `"intro" | "planning" | "interview"`. On the intro screen, under the primary Start button, add a secondary full-width button:

```tsx
<Button type="button" variant="outline" disabled={!title.trim() || starting}
        data-testid="interview-plan-first"
        onClick={() => handleStart("planning")}>
  {t("planFirst")}
</Button>
```

`handleStart` gains an optional target: it already calls `startInterview({ title })` and sets `phase` — parameterize the destination phase (`"interview"` default, `"planning"` from the new button). In the dialog body, render `phase === "planning" && interviewId && (<PlanningChat interviewId={interviewId} onDone={() => setPhase("interview")} />)`. The dialog's content area may need a taller layout for the chat — reuse whatever the revise drawer does for chat sizing if the dialog looks cramped (`revise-drawer.tsx`'s container classes); keep the change minimal. Read the component and follow its state naming (the interview id state variable may be named differently — adapt).

- [ ] **Step 4: i18n.** `el.json`, new `curricula.planning` object (and `curricula.interview`'s intro section gains `planFirst` — put it wherever the intro's strings live; read the component's `t()` scope first):

```json
"planning": {
  "chatHint": "Συζήτησε την ιδέα σου — στόχους, θέματα, τι να αποφύγουμε. Όταν είσαι έτοιμος, πάτα «Χρησιμοποίησε αυτό το σχέδιο».",
  "usePlan": "Χρησιμοποίησε αυτό το σχέδιο",
  "skip": "Παράλειψη — κατευθείαν στον οδηγό",
  "editHint": "Αυτό κατάλαβα από τη συζήτηση. Διόρθωσέ το ελεύθερα — αυτό θα καθοδηγήσει τη δημιουργία.",
  "backToChat": "Πίσω στη συζήτηση",
  "continueToWizard": "Συνέχεια στον οδηγό",
  "loading": "Φόρτωση...",
  "sessionError": "Η συνομιλία δεν άνοιξε — δοκίμασε ξανά.",
  "distillEmpty": "Δεν υπάρχει ακόμα συζήτηση για να αποσταχθεί — γράψε πρώτα τι θέλεις.",
  "distillError": "Η απόσταξη απέτυχε — δοκίμασε ξανά.",
  "saveError": "Η αποθήκευση απέτυχε — δοκίμασε ξανά."
}
```

plus `"planFirst": "Θέλεις να το συζητήσουμε πρώτα;"`. Mirror all keys in `en.json` ("Want to talk it through first?", "Use this plan", "Skip — straight to the wizard", etc.).

- [ ] **Step 5: Playwright** — extend `apps/web/tests/interview.spec.ts`: (a) the intro shows `interview-plan-first` alongside the primary start; (b) the primary start still goes straight to the first server step (mock unchanged — this pins "skipping = today's flow"); (c) if the spec's API mocks make it cheap, a planning-phase smoke: click plan-first → `planning-chat` visible → `planning-skip` → first interview step renders. Mock the three new endpoints the way the spec mocks the others (read its route-mock helpers first).

- [ ] **Step 6: Verify + commit**

Run: `cd apps/web && npx tsc --noEmit && npm run build && npx playwright test tests/interview.spec.ts`
Expected: clean build; interview spec green.

```bash
git add apps/web/src/components/curriculum/planning-chat.tsx apps/web/src/components/curriculum/interview-dialog.tsx apps/web/src/lib/api.ts apps/web/src/messages/el.json apps/web/src/messages/en.json apps/web/tests/interview.spec.ts
git commit -m "feat(interview): planning chat phase — Θέλεις να το συζητήσουμε πρώτα;"
```

---

### Task 6: Full verification pass

- [ ] **Step 1: Backend suite** — `cd apps/api && .venv/bin/python -m pytest tests/ -q`; adjudicate every failure by name against the known pre-existing/environmental set (30 failed + 2 errors baseline from the Parts 1–4 run; `*live*` ConnectError class). Zero NEW failures allowed.
- [ ] **Step 2: Frontend** — `npx tsc --noEmit && npm run build && npx playwright test`; failure set must remain exactly the 3 known confirm.spec.ts pre-existing ones.
- [ ] **Step 3: Migration check** — `docker exec guitar_tutor-postgres-1 psql -U guitar -d guitar -c "\d chat_session"` currently lacks `interview_id`; note in the report that the migration runs at api-container boot on deploy (per the migration docstring pattern) — do NOT restart containers; deployment is the human's call.
- [ ] **Step 4: Report** deploy-readiness (in-flight jobs check via generation_job query) — commit nothing beyond what tasks committed; no deploy.
