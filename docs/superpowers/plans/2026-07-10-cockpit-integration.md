# Cockpit Integration Implementation Plan (Plan 6)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Fill out the tutor's daily cockpit — Notes (new model + page + promote-to-Brain), Progress/lesson logging, a student-detail page, the assign-curriculum-to-student UI (deferred from Plan 3), and the Today/Prep lesson-prep view — and wire the 3 deferred agent tools, completing the product's workflow.

**Architecture:** Fill the existing `today`/`notes` nav STUBS + add a student-detail route. Reuse the existing engines. New: a `Note` model; `Progress`/`LessonLog` services (models already exist, `app/models/curriculum.py`). The 3 deferred agent tools (`add_note`/`promote_note_to_knowledge`/`log_progress`) slot into the Plan 5 `TOOLS` registry as `kind="mutation"` (HITL-gated).

**Tech Stack:** FastAPI/SQLAlchemy/Alembic; Next.js/next-intl; the Plan 5 agent registry.

## Global Constraints (verbatim)
- Reuse: `Progress{student_id, block_id, status(not_started|introduced|practicing|mastered), notes}` + `LessonLog{student_id, session_block_id, date, taught, notes, homework}` (exist — add services, not models); `Assignment` + `POST /curricula/{root_id}/assign` (exist — add the UI); `create_source`+`ingest_source` (for promote-to-Brain); the Plan 5 `TOOLS` registry + `ToolEntry(schema, fn, kind)`.
- New agent mutation tools go through the SAME HITL suspend/approve path (Plan 5) — register them `kind="mutation"`, thin fns wrapping the new services; the loop already suspends on any mutation.
- Tests isolate to `guitar_test`. **Bilingual GR/EN.** Web dev/test **port 3100**. Fill the existing `today`/`notes` stub pages (don't create new routes for them).
- **Priority order** (execute highest-value first if the night runs short): T1→T2 (Notes) and T4 (assign UI) and T6 (agent tools) are the concrete gaps; T5 (Today/Prep) is the flagship-but-largest.

## Task 1: Note model + Notes CRUD + promote-to-Brain (backend)
**Files:** `app/models/note.py` + migration + register; `app/schemas/notes.py`; `app/routers/notes.py`; `app/notes/promote.py`; wire `main.py`; tests.
**Produces:** `Note(id, title:str(300), body:Text, tags:JSON list[str], student_id:UUID|None (no FK/decoupled or FK SET NULL), promoted_to_knowledge:bool default False, +mixins)`. Routes: `POST /notes`, `GET /notes` (filter student_id?), `GET /notes/{id}`, `PATCH /notes/{id}`, `DELETE /notes/{id}` (204), `POST /notes/{id}/promote` → create a `SourceCreate(kind="text", title=note.title, text=note.body)` + `ingest_source` → set `promoted_to_knowledge=True` (idempotent — 409/no-op if already promoted). Mirror the artifacts/students router style + error mapping.
- [ ] TDD: Note CRUD round-trips; promote creates a ready KnowledgeSource + flips the flag; re-promote is a no-op. Migration up/down (drop the pgvector `ix_chunk_embedding_hnsw` autogenerate false-positive). RED→GREEN. Commit.

## Task 2: Notes cockpit page
**Files:** fill `app/[locale]/(cockpit)/notes/page.tsx`; `components/notes/*`; extend `lib/api.ts`; messages; tests.
**Produces:** a real Notes page: list notes, create/edit (title/body/tags/optional student), delete, and a **Promote to Knowledge** button (→ `POST /notes/{id}/promote`, shows promoted state). `lib/api.ts` note helpers + types. Bilingual.
- [ ] Playwright (mocked, 3100): create a note → appears; promote → shows promoted; delete. Keep suite green. RED→GREEN. Commit.

## Task 3: Progress + LessonLog services/routes + student-detail data
**Files:** `app/curriculum/progress.py` (or extend); `app/routers/students.py` (add sub-routes) or a new router; schemas; tests.
**Produces:** services + routes: `POST /students/{id}/progress` (upsert Progress for a block: student_id+block_id+status[+notes]); `POST /students/{id}/lessons` (create a LessonLog); `GET /students/{id}/detail` → the student + their assignments (the assigned curriculum roots) + their Progress rows + recent LessonLogs (one aggregate payload for the detail page). Mirror house error mapping (404 unknown student/block).
- [ ] TDD: upsert progress (create then update same block = one row, new status); create a lesson log; the detail aggregate returns assignments+progress+logs for a seeded student. RED→GREEN. Commit.

## Task 4: Student-detail page + assign-to-student UI
**Files:** `app/[locale]/(cockpit)/students/[id]/page.tsx`; `components/students/*` (assign dialog, progress list); extend `lib/api.ts`; extend the students list page (a row → detail link); messages; tests.
**Produces:** a student-detail page (name/level; their assignments as curriculum boards or links; a Progress list with editable status; recent lesson logs; a "log a lesson" quick form). An **Assign curriculum** flow: a dialog (pick a curriculum → `POST /curricula/{root_id}/assign {student_id}`) reachable from the student detail (and/or the curricula board). Bilingual.
- [ ] Playwright (mocked, 3100): open a student's detail → shows assignments/progress; assign a curriculum → the assignment appears; set a progress status → PATCH/POST fires. Keep suite green. RED→GREEN. Commit.

## Task 5: Today/Prep page
**Files:** fill `app/[locale]/(cockpit)/today/page.tsx`; `components/today/*`; extend `lib/api.ts` (or reuse detail); messages; tests.
**Produces:** a lesson-prep view: pick a student → show their active assignment's **next session/lesson** (the first non-`mastered`/next-in-order session Block under their assigned delivery tree) with its content + attached artifacts; quick actions to **generate/attach an artifact** for it (reuse the Plan 4 attach flow) and **log the lesson** (Task 3's LessonLog). A backend helper `GET /students/{id}/next-session` (or compute client-side from the detail payload) picks the next session. Bilingual.
- [ ] Playwright (mocked, 3100): Today for a student shows the next session + its content; attach an artifact; log a lesson. Keep suite green. RED→GREEN. Commit.

## Task 6: the 3 deferred agent tools + real e2e
**Files:** extend `app/agent/tools.py` (register 3 mutation tools) + `app/agent/prompts.py` (mention them briefly, minimally — agentic-gotchas #1); a live-LLM test; real e2e; README.
**Produces:** register `add_note` (→ Note create), `promote_note_to_knowledge` (→ the promote service), `log_progress` (→ Progress upsert) in `TOOLS` as `kind="mutation"` (thin fns wrapping Tasks 1/3 services; they suspend + HITL-approve via the Plan 5 path unchanged). Keep the prompt addition minimal.
- [ ] Live-LLM (`@pytest.mark.integration`): "make a note that Maria struggled with barre chords" → `awaiting_approval` with `tool_name=="add_note"` + plausible args. RED→GREEN.
- [ ] **Real e2e (report):** rebuild api/web to current code; a real browser pass over the new cockpit — create+promote a note, open a student detail + assign + log progress, prep Today, and one chat that proposes `add_note` → approve. Screenshot. Commit + README. Leave the stack healthy.

## Self-Review
Coverage: Note model+CRUD+promote (T1) ✓ · Notes page (T2) ✓ · Progress/Lesson services + detail aggregate (T3) ✓ · student-detail + assign UI (T4) ✓ · Today/Prep (T5) ✓ · deferred agent tools + e2e (T6) ✓. Reuses Plan 5 HITL for the new mutation tools; reuses existing Progress/LessonLog/Assignment models. Deferred: richer prep intelligence (the composite `plan_todays_lesson`/`recreate_tone` agent tools — can layer on later); GR polish.
