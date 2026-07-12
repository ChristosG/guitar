# Lesson Authoring — Implementation Plan (Plan 10)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let the tutor author and edit **lessons** (multi-session units) from his own content, and let the agent edit them for him — split a session, merge two, add one — all HITL-gated.

**Architecture:** A Lesson is `Block(kind="lesson")` with `Block(kind="session")` children — **no new table**. Drafting from a Library selection is an async `GenerationJob(kind="lesson")` grounded in the selected passage, recording its source+page provenance. Splitting reuses `segment.py`'s deterministic `partition_by_minutes` rather than asking an LLM to do arithmetic.

**Tech Stack:** FastAPI · SQLAlchemy 2 · Postgres · Next.js 16 + next-intl · Playwright · pytest

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-07-12-lesson-authoring-design.md`. Decisions B1–B6 are binding.
- **B1 — no new table.** Lesson = `Block(kind="lesson")`, Session = `Block(kind="session")`, content = `Block(kind="item")`. `Block.kind` is documented in the model as "soft, relabelable". Do not create a `Lesson` model.
- **B3 — provenance without a migration.** A drafted lesson records `{"provenance": {"source_id": ..., "page_no": ...}}` inside the existing `Block.target_profile` JSON column.
- **B5 — every agent lesson-tool is `kind="mutation"`** and therefore HITL-gated by the existing approve-before-execute machinery in `app/agent/loop.py`. Never bypass it. It is his work being edited.
- **B6 — split/merge are DETERMINISTIC.** Reuse `app/curriculum/segment.py`'s `partition_by_minutes`. Do NOT ask the model to re-cut a session; it will get the arithmetic wrong and hallucinate durations.
- **TDD throughout.** Failing test → RED → minimal implementation → GREEN → commit.
- **i18n mandatory:** every user-visible string via next-intl, present in BOTH `messages/en.json` and `messages/el.json`.
- **Do not touch the app DB (`guitar`)** — it holds the tutor's OCR'd book and must stay demo-ready. Tests use `guitar_test`.
- Host-side pytest needs `LLM_BASE_URL=http://localhost:6888/v1 EMBED_BASE_URL=http://localhost:8090/v1` exported (`conftest.py` only force-sets `DATABASE_URL`).
- **Migration gotcha (bites every time):** alembic autogenerate falsely emits `op.drop_index('ix_chunk_embedding_hnsw', ...)`. Strip it. Name every constraint explicitly (an unnamed FK once made `downgrade()` uncallable). *This plan should need no migration at all — if you think you need one, stop and re-read B1.*

## Verified Facts (from Plan 9 — do not re-derive)

- `Block(parent_id, order, kind, title, body, est_minutes, language, is_template, target_profile: JSON, student_id, plane)`, `children` relationship with `cascade="all, delete-orphan"`.
- `app/curriculum/segment.py` exposes `partition_by_minutes(leaves: list[Leaf], session_minutes: int) -> list[list[Leaf]]` and `_collect_leaves(db, root)`. `POST /blocks/{id}/segment` already works.
- Existing block routes: `GET /blocks/{id}`, `PATCH /blocks/{id}`, `DELETE /blocks/{id}`, `GET /curricula/{root_id}` → all return `BlockTreeOut`.
- `GenerationJob(kind, status, params: JSON, result_root_id, error, error_kind)` + `GET /jobs/{id}` + `BackgroundTasks` — the established async pattern (`app/jobs/runner.py`, `routers/curriculum.py`).
- `POST /lessons/from-selection` currently returns `201` echoing `{selection_id, source_id, source_title, page_no, text}` — a **stub** (`app/routers/lessons.py`). This plan makes it real.
- `LLMProvider.guided_json(messages, schema, *, temperature=0.2) -> dict` — schema-constrained decoding.
- Agent tool registry: `app/agent/tools.py`, entries carry `kind="read"|"mutation"` and `async_job: bool`. Mutations suspend for approval (`app/agent/loop.py`).
- **KEY TEST GOTCHA:** Starlette's `TestClient` runs `BackgroundTasks` AFTER the response — an enqueue test MUST monkeypatch the runner, or a real multi-minute LLM call fires during the test.

---

### Task 1: Draft a lesson from a selection (async, grounded, with provenance)

**Files:**
- Create: `apps/api/app/lessons/__init__.py`, `apps/api/app/lessons/draft.py`
- Modify: `apps/api/app/jobs/runner.py` (add `run_lesson_job`)
- Modify: `apps/api/app/routers/lessons.py` (stub → 202 enqueue)
- Test: `apps/api/tests/test_lesson_draft.py` (create)

**Interfaces:**
- Produces: `draft_lesson_from_selection(db, *, source_id, page_no, text, language="en") -> uuid.UUID` (returns the lesson root Block id); `run_lesson_job(job_id)`; `POST /lessons/from-selection` → `202 {job_id}`.

**Design:** one `guided_json` call producing `{title, sessions: [{title, est_minutes, items: [{title, body}]}]}`. Persist as `Block(kind="lesson")` → `Block(kind="session")` → `Block(kind="item")`, `plane="content"`, `is_template=False`. Record provenance per B3 on the lesson root:
`target_profile = {"provenance": {"source_id": str(source_id), "page_no": page_no}}`.
The prompt MUST instruct the model to ground the lesson in the supplied passage and NOT invent facts, citations, or URLs (same anti-invention posture as `app/agent/prompts.py`).

- [ ] **Step 1: Write the failing tests**

```python
# apps/api/tests/test_lesson_draft.py
import uuid
import pytest
from app.lessons.draft import draft_lesson_from_selection
from app.models.block import Block
from app.models.knowledge import KnowledgeSource


class _FakeProvider:
    """guided_json is schema-constrained, so a fake returns a valid tree."""
    def __init__(self):
        self.messages = None

    def guided_json(self, messages, schema, *, temperature=0.2):
        self.messages = messages
        return {
            "title": "Pick Thickness and Tone",
            "sessions": [
                {"title": "Session 1: What a pick does", "est_minutes": 45,
                 "items": [{"title": "Thin vs heavy", "body": "A thin pick is brighter."}]},
                {"title": "Session 2: Choosing yours", "est_minutes": 30,
                 "items": [{"title": "The three-pick test", "body": "Buy thin, medium, heavy."}]},
            ],
        }


def _source(db) -> KnowledgeSource:
    src = KnowledgeSource(type="pdf", title="Getting Great Guitar Sounds", status="ready")
    db.add(src); db.commit()
    return src


def test_drafts_a_lesson_with_sessions_and_items(db, monkeypatch):
    fake = _FakeProvider()
    monkeypatch.setattr("app.lessons.draft.get_provider", lambda: fake)
    src = _source(db)

    lesson_id = draft_lesson_from_selection(
        db, source_id=src.id, page_no=19,
        text="You will notice that the tone is much thinner and brighter with the lighter pick.",
    )

    lesson = db.get(Block, lesson_id)
    assert lesson.kind == "lesson"
    assert lesson.title == "Pick Thickness and Tone"
    assert lesson.plane == "content"

    sessions = db.query(Block).filter_by(parent_id=lesson.id).order_by(Block.order).all()
    assert [s.kind for s in sessions] == ["session", "session"]
    assert sessions[0].est_minutes == 45
    items = db.query(Block).filter_by(parent_id=sessions[0].id).all()
    assert items[0].kind == "item"
    assert "thin pick" in items[0].body.lower()


def test_records_page_provenance_so_the_lesson_can_cite_its_source(db, monkeypatch):
    # B3 — this is what makes a lesson traceable back to the scan it came from
    monkeypatch.setattr("app.lessons.draft.get_provider", lambda: _FakeProvider())
    src = _source(db)

    lesson_id = draft_lesson_from_selection(db, source_id=src.id, page_no=19, text="passage")

    prov = db.get(Block, lesson_id).target_profile["provenance"]
    assert prov["source_id"] == str(src.id)
    assert prov["page_no"] == 19


def test_the_selected_passage_is_actually_given_to_the_model(db, monkeypatch):
    # a "grounded" draft that never sees the passage is not grounded
    fake = _FakeProvider()
    monkeypatch.setattr("app.lessons.draft.get_provider", lambda: fake)
    src = _source(db)

    draft_lesson_from_selection(db, source_id=src.id, page_no=19,
                                text="UNIQUE_PASSAGE_MARKER about pick thickness")

    sent = " ".join(m["content"] for m in fake.messages)
    assert "UNIQUE_PASSAGE_MARKER" in sent


def test_unknown_source_raises(db, monkeypatch):
    monkeypatch.setattr("app.lessons.draft.get_provider", lambda: _FakeProvider())
    with pytest.raises(ValueError):
        draft_lesson_from_selection(db, source_id=uuid.uuid4(), page_no=1, text="x")
```

- [ ] **Step 2: Run to verify RED**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_lesson_draft.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.lessons'`

- [ ] **Step 3: Implement `draft.py`**

Write `app/lessons/draft.py` with a `LESSON_SCHEMA` (guided-JSON schema for `{title, sessions[{title, est_minutes, items[{title, body}]}]}`), a grounding prompt that embeds the passage and forbids invention, and persistence into the `Block` tree with `order` set sequentially and provenance per B3. Raise `ValueError` on an unknown `source_id`.

- [ ] **Step 4: Add `run_lesson_job` to `app/jobs/runner.py`**

Mirror `run_curriculum_job` exactly: own `SessionLocal()`, `status` pending→running→succeeded|failed, `error_kind` in `{upstream, timeout, internal}` (`GuidedJSONError`→upstream, `openai.APIConnectionError`/`httpx.TransportError`→timeout, else internal), the same best-effort nested failure-recording guard, `result_root_id = lesson_id` on success.

- [ ] **Step 5: Make the route real**

`POST /lessons/from-selection` becomes: validate the source (404 if unknown), create `GenerationJob(kind="lesson", params={...})`, **commit**, then `background.add_task(run_lesson_job, job.id)`, return `202 {"job_id": ...}`. Add a router test that monkeypatches `app.routers.lessons.run_lesson_job` (see the KEY TEST GOTCHA above) and asserts 202 + a real job id, plus 404 on unknown source and 422 on blank text (those two already exist — keep them).

- [ ] **Step 6: GREEN + full suite + commit**

```bash
cd apps/api && ./.venv/bin/python -m pytest tests/test_lesson_draft.py tests/test_lessons_seam.py -v
cd apps/api && ./.venv/bin/python -m pytest -m "not integration" -q
git add apps/api/app/lessons apps/api/app/jobs/runner.py apps/api/app/routers/lessons.py apps/api/tests
git commit -m "feat(lessons): draft a grounded lesson from a Library selection (async job)"
```

---

### Task 2: Deterministic split / merge / add-session

**Files:**
- Create: `apps/api/app/lessons/edit.py`
- Modify: `apps/api/app/routers/lessons.py`
- Test: `apps/api/tests/test_lesson_edit.py` (create)

**Interfaces:**
- Produces: `split_session(db, session_id, *, session_minutes: int) -> list[Block]`, `merge_sessions(db, session_ids: list[uuid.UUID]) -> Block`, `add_session(db, lesson_id, *, title, est_minutes=None, after: uuid.UUID | None = None) -> Block`.
- Routes: `POST /lessons/{lesson_id}/sessions/{session_id}/split {session_minutes}`, `POST /lessons/{lesson_id}/sessions/merge {session_ids}`, `POST /lessons/{lesson_id}/sessions {title, est_minutes, after}`.
- `GET /lessons` → `list[LessonListItem(id, title, created_at, provenance)]`; `GET /lessons/{id}` → existing `BlockTreeOut`.

**B6 is binding: split is DETERMINISTIC.** Reuse `partition_by_minutes` from `app/curriculum/segment.py`. Read that module first and use its real `Leaf` shape. Do not call an LLM here.

- [ ] **Step 1: Write the failing tests**

```python
# apps/api/tests/test_lesson_edit.py
import pytest
from app.lessons.edit import add_session, merge_sessions, split_session
from app.models.block import Block


def _lesson_with_one_long_session(db):
    lesson = Block(kind="lesson", title="Barre Chords", plane="content", order=0)
    db.add(lesson); db.commit()
    s = Block(kind="session", title="Everything", parent_id=lesson.id, order=0, est_minutes=120)
    db.add(s); db.commit()
    for i, mins in enumerate([30, 30, 30, 30]):
        db.add(Block(kind="item", title=f"Item {i+1}", body=f"body {i+1}",
                     parent_id=s.id, order=i, est_minutes=mins))
    db.commit()
    return lesson, s


def test_split_cuts_one_session_into_several_by_minutes_and_keeps_every_item(db):
    lesson, session = _lesson_with_one_long_session(db)

    new_sessions = split_session(db, session.id, session_minutes=60)

    assert len(new_sessions) == 2                 # 4x30min at 60min/session
    assert all(s.kind == "session" for s in new_sessions)
    assert all(s.parent_id == lesson.id for s in new_sessions)
    # NOTHING may be lost in a split — this is the tutor's work
    titles = [i.title for s in new_sessions
              for i in db.query(Block).filter_by(parent_id=s.id).order_by(Block.order)]
    assert titles == ["Item 1", "Item 2", "Item 3", "Item 4"]
    # the original over-long session is gone, replaced by its parts
    assert db.get(Block, session.id) is None
    # ordering is contiguous under the lesson
    orders = [s.order for s in db.query(Block).filter_by(parent_id=lesson.id).order_by(Block.order)]
    assert orders == list(range(len(orders)))


def test_merge_folds_adjacent_sessions_into_one_preserving_item_order(db):
    lesson = Block(kind="lesson", title="L", plane="content", order=0)
    db.add(lesson); db.commit()
    a = Block(kind="session", title="A", parent_id=lesson.id, order=0, est_minutes=30)
    b = Block(kind="session", title="B", parent_id=lesson.id, order=1, est_minutes=45)
    db.add_all([a, b]); db.commit()
    db.add(Block(kind="item", title="a1", parent_id=a.id, order=0))
    db.add(Block(kind="item", title="b1", parent_id=b.id, order=0))
    db.commit()

    merged = merge_sessions(db, [a.id, b.id])

    assert merged.kind == "session"
    assert merged.est_minutes == 75                       # summed
    items = db.query(Block).filter_by(parent_id=merged.id).order_by(Block.order).all()
    assert [i.title for i in items] == ["a1", "b1"]       # order preserved across the seam
    assert db.query(Block).filter_by(parent_id=lesson.id).count() == 1


def test_merging_sessions_from_different_lessons_is_rejected(db):
    l1 = Block(kind="lesson", title="L1", plane="content", order=0)
    l2 = Block(kind="lesson", title="L2", plane="content", order=0)
    db.add_all([l1, l2]); db.commit()
    s1 = Block(kind="session", title="S1", parent_id=l1.id, order=0)
    s2 = Block(kind="session", title="S2", parent_id=l2.id, order=0)
    db.add_all([s1, s2]); db.commit()

    with pytest.raises(ValueError):
        merge_sessions(db, [s1.id, s2.id])


def test_add_session_appends_and_can_insert_after_a_given_session(db):
    lesson, first = _lesson_with_one_long_session(db)

    appended = add_session(db, lesson.id, title="Warm-up", est_minutes=10)
    assert appended.parent_id == lesson.id
    assert appended.order == 1

    inserted = add_session(db, lesson.id, title="Intro", est_minutes=5, after=first.id)
    sessions = db.query(Block).filter_by(parent_id=lesson.id).order_by(Block.order).all()
    assert [s.title for s in sessions] == ["Everything", "Intro", "Warm-up"]
    assert [s.order for s in sessions] == [0, 1, 2]
```

- [ ] **Step 2: Run to verify RED**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_lesson_edit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.lessons.edit'`

- [ ] **Step 3: Implement `edit.py` (deterministic, transactional)**

Reuse `partition_by_minutes`. A split must be all-or-nothing: either the new sessions exist with every item re-parented, or nothing changed. Re-normalise sibling `order` to `0..n-1` after every operation.

- [ ] **Step 4: Wire the routes + `GET /lessons` list + `GET /lessons/{id}` tree**

`GET /lessons/{id}` reuses the existing `BlockTreeOut` serializer from `app/schemas/curriculum.py` — do not write a second tree serializer. `GET /lessons` lists `Block`s where `kind == "lesson"` and `plane == "content"`, newest first, surfacing `target_profile["provenance"]` so the UI can show "from *the book*, p.19".

- [ ] **Step 5: GREEN + full suite + commit**

```bash
cd apps/api && ./.venv/bin/python -m pytest tests/test_lesson_edit.py -v
cd apps/api && ./.venv/bin/python -m pytest -m "not integration" -q
git commit -am "feat(lessons): deterministic split/merge/add-session + lesson routes"
```

---

### Task 3: Agent tools for lessons (HITL-gated)

**Files:**
- Modify: `apps/api/app/agent/tools.py`, `apps/api/app/agent/prompts.py`
- Test: `apps/api/tests/test_agent_lesson_tools.py` (create)

**Interfaces:**
- Four new registry entries, all `kind="mutation"`: `draft_lesson_from_selection` (`async_job=True` — it enqueues a job like `generate_curriculum` does), `split_session`, `merge_sessions`, `add_session` (all `async_job=False` — cheap deterministic writes).

**B5 is binding.** `loop.py` and `chat.py` are already generic over `kind == "mutation"` — **do not touch them.** Read them first to confirm, then register the tools. Every one of these edits the tutor's own work, so it suspends for approval by construction.

- [ ] **Step 1: Write the failing tests**

Mirror the existing conventions in `tests/test_agent_mutation_tools.py` (direct function tests: happy path + graceful `{"error": ...}` dicts for malformed/missing ids) and `tests/test_agent_hitl.py` (registry-shape tests + one suspend test per new tool proving no mutation executes before approval).

```python
# apps/api/tests/test_agent_lesson_tools.py  (sketch — follow the existing files' style exactly)
from app.agent.tools import TOOLS

def test_lesson_tools_are_registered_as_mutations():
    for name in ["draft_lesson_from_selection", "split_session", "merge_sessions", "add_session"]:
        assert TOOLS[name].kind == "mutation"      # HITL-gated: it is HIS work being edited

def test_split_session_tool_splits_a_real_session(db):
    ...  # happy path through the tool wrapper

def test_split_session_tool_returns_a_graceful_error_for_an_unknown_id(db):
    out = TOOLS["split_session"].fn(db, session_id="not-a-uuid", session_minutes=60)
    assert "error" in out                          # never raise into the loop
```

- [ ] **Step 2: RED** — `KeyError: 'split_session'`

- [ ] **Step 3: Implement the tool wrappers** — thin wraps over `app/lessons/edit.py` + `draft.py`, catching `ValueError` into `{"error": ...}` dicts (the established convention — never raise into the ReAct loop). Add "lessons, sessions," to the existing tool-trigger sentence in `prompts.py`. Keep `SYSTEM_PROMPT` one paragraph — a longer prompt empirically SUPPRESSES tool-calling on this model.

- [ ] **Step 4: GREEN + full suite + commit**

---

### Task 4: The Lesson editor UI

**Files:**
- Create: `apps/web/src/app/[locale]/(cockpit)/lessons/page.tsx` (list), `lessons/[lessonId]/page.tsx` (editor)
- Create: `apps/web/src/components/lessons/{lesson-outline,session-card,provenance-chip}.tsx`
- Modify: `apps/web/src/lib/api.ts`, `apps/web/src/components/app-shell.tsx` (nav), `messages/{en,el}.json`
- Test: `apps/web/tests/lessons.spec.ts` (create)

**Design bar:** Chris is a guitar teacher and a computer novice who called the old cockpit *"too chaotic, too many boxes to fill"*. **Invoke the `frontend-design` skill and follow it.** The editor is an OUTLINE, not a form: the lesson, its sessions in order, the items inside each. Inline rename. Split / merge / add as obvious buttons on a session. Nothing else.

The **provenance chip** ("from *Getting Great Guitar Sounds*, p.19") links straight into the Reader at that page — that is the payoff of the entire Library project, so it must actually work.

- [ ] **Step 1: Write the failing Playwright tests** (stub the API with `page.route()`, per the established pattern): the outline renders lesson → sessions → items; split calls the API and re-renders; the provenance chip links to `/library/{source_id}?page=19`; rename PATCHes the block.
- [ ] **Step 2: RED** (route 404s)
- [ ] **Step 3: Build it.** Client components (the browser calls the API directly — `page.route()` must intercept). Every string via next-intl in **both** locales.
- [ ] **Step 4: GREEN** — `npx playwright test tests/lessons.spec.ts && npx tsc --noEmit && npm run lint && npx playwright test` (don't break the existing 32)
- [ ] **Step 5:** Rebuild web, LOOK at it, commit.

---

### Task 5: Wire "Author a lesson from this" end-to-end + live acceptance

**Files:**
- Modify: `apps/web/src/components/library/selection-action.tsx` (202 + poll, then navigate to the editor)
- Create: `apps/api/tests/test_lesson_live.py` (`@pytest.mark.integration`)
- Modify: `.superpowers/sdd/progress.md`, `README.md`

The Reader's selection action currently POSTs to a stub that returns `201` and echoes. It must now handle **`202 {job_id}`**, poll `GET /jobs/{job_id}` behind the existing spinner (reuse the curriculum-generation poll pattern), and on success navigate to `/lessons/{result_root_id}`.

**ACCEPTANCE TEST — the point of this plan. Do not mark it complete on unit tests.**

> Open the real book in the Reader. Select the real pick-thickness passage on **p.19**. Hit **"Author a lesson from this"**. Get back a real multi-session lesson whose content is **grounded in that passage**, carrying a **working citation chip back to p.19's scan**. Then tell the agent *"split session 2, it's too long"*, approve the HITL card, and watch the session **actually split**.

Drive it against the REAL model and the REAL app data. Print the drafted lesson. **Read it.** If it's generic filler that ignores the passage, that is a genuine finding — report it honestly and do not force it green.

Screenshot to `lesson-e2e.png`. Full regression: backend `-m "not integration"` + `npx playwright test`.

---

## Self-Review

| Spec | Task |
|---|---|
| B1 Lesson=Block(kind=lesson), no new table | T1 (drafting), T2 (edit) |
| B2 plane="content" | T1 |
| B3 provenance in target_profile JSON | T1 (test), T4 (chip), T5 (round-trip) |
| B4 async GenerationJob(kind="lesson") | T1 |
| B5 agent tools HITL-gated | T3 |
| B6 split/merge deterministic via partition_by_minutes | T2 |
| Acceptance: grounded lesson + working citation + agent split | T5 |

**Type consistency:** `draft_lesson_from_selection` returns the lesson root `uuid` (T1) and is consumed by `run_lesson_job` → `GenerationJob.result_root_id` (T1) → the FE poll → `/lessons/{id}` (T5). `split_session`/`merge_sessions`/`add_session` signatures are defined in T2 and wrapped verbatim by T3's tools.

**Known risk to watch:** the model may draft a *generic* lesson that ignores the passage — "grounded" is asserted in T1 only by proving the passage reaches the prompt. T5's live test is what actually proves grounding, by reading the output. Do not let T5 be satisfied by a green assertion alone.
