# Surgical Revise Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Segment-level revise ops (`add_segment`/`edit_segment`/`remove_segment`), a blueprint-aware planner, a server-computed honest impact banner on the approval card, and content-preserving lesson rewrites.

**Architecture:** Spec: `docs/superpowers/specs/2026-07-21-surgical-revise-design.md` (approved; its section A-D letters are referenced below). All backend seams verified in the 2026-07-21 seam map; key facts are inlined per task. `apply_revision` keeps its zero-LLM one-transaction contract; segment prose generation runs in `run_curriculum_revise_job` AFTER commit; the segment-generation prompt lives in a NEW module `app/curriculum/segment.py` so `revise.py`'s single pinned provider call site (`curriculum/revise.py:326`) never shifts.

**Tech Stack:** FastAPI/SQLAlchemy/pytest; Next.js/next-intl/Playwright.

## Global Constraints

- Greek-first: new UI strings in both `apps/web/src/messages/{el,en}.json`, Greek primary.
- Registry law (`tests/test_prompts_registry.py`): every new provider call site needs a `PromptEntry` with exact `source_ref`/`call_sites` line pins taken from the COMMITTED file via grep; changed slice defaults (e.g. `REVISE_TAIL`) require regenerating only that key in `tests/fixtures/prompt_renders_baseline.json` via `tests/prompt_baseline.py` (precedent commits 530be6f, bfbdb0f). Line-shift: after editing a file, grep `app/prompts/registry.py` for that file's pins and refresh stale ones in the same commit.
- Backend tests: `cd apps/api && .venv/bin/python -m pytest <files> -q`; ignore `*live*` ConnectError failures (pre-existing). Frontend: `cd apps/web && npx tsc --noEmit && npm run build`; if running Playwright after a production build, `rm -rf .next` first (stale-build contamination — see task-6 report 2026-07-21).
- `apply_revision` (`revise.py:347-410`): ONE transaction, ONE commit, rollback-on-exception, ZERO provider calls — preserve all four properties.
- Meta JSON columns have no MutableDict: always whole-dict reassignment (`block.meta = {**(block.meta or {}), ...}`).
- Commit per task, `feat(revise): ...` style.

---

### Task 1: Ops schema + apply + custom-segment survival (Spec A, apply side)

**Files:**
- Modify: `apps/api/app/curriculum/revise.py` (`_OP_ENUM:58-61`, `REVISION_PLAN_SCHEMA:63-113`, `_REQUIRED:118-125`, `validate_ops:~230-289`, `apply_revision:347-410`)
- Modify: `apps/api/app/curriculum/draft.py` (`persist_lesson` delete-all at `:520-522`)
- Test: `apps/api/tests/test_surgical_revise.py` (new)

**Interfaces:**
- Produces op vocabulary (Task 2 generates what apply queues; Task 3's planner emits these; Task 4 counts them):
  - `add_segment {lesson_id, title, instruction, section_key?}` → apply creates `Block(kind="segment", parent=lesson, order=last, title=title, body="")` with meta `{segment_status:"queued", segment_instruction: instruction}` plus EITHER `{section: section_key}` (when `section_key` given — validated: must be an ENABLED section key of the course blueprint, else `validate_ops` rejects the op with a clear message) OR `{custom: True, section: f"custom:{slug(title)}"}`.
  - `edit_segment {segment_id, instruction}` → target must be `kind=="segment"` inside this course (walk parents; else reject). Meta gains `{segment_status:"queued", segment_instruction, prev_body: seg.body, prev_title: seg.title, refined: True, refine_instruction: instruction}` — the refine-flow meta contract verbatim (`app/curriculum/refine.py:169-175`) so `POST /blocks/{id}/undo` (`undo_refine`, `refine.py:179-190`) works unchanged on surgically edited segments. Confirm `undo_refine`'s exact key set by reading it first and match it.
  - `remove_segment {segment_id}` → same course-membership validation; delete + sibling `_renormalise` (reuse `edit._renormalise` the way `remove_lesson:393-399` does).
- After ANY segment op on a lesson, recompute lesson meta: `word_count` = sum of `len(body.split())` over remaining/queued segments, and `meets_floor = word_count >= floor_words` when `floor_words` is set (whole-dict reassignment).
- `persist_lesson` (`draft.py:520-522`): before the delete-all, collect children with `(b.meta or {}).get("custom")` truthy; delete only the rest; after recreating blueprint sections, re-append the preserved customs in prior relative order with sequential `order` continuing after the new sections.

**Steps (TDD):**
- [ ] **1. Failing tests** — `tests/test_surgical_revise.py`: build a course→module→lesson tree with 2 segments (mirror `tests/test_curricula_manage.py::_mk_course`'s construction, adding segments with `meta={"section": "theory"}` etc. and a course `meta={"blueprint": ...}` — copy a valid blueprint dict from `tests/test_blueprint_storage.py`'s fixtures or `blueprint.py`'s default). Tests:
  - `validate_ops` accepts the three new ops with required fields; rejects `add_segment` with a `section_key` not enabled in the blueprint; rejects `edit_segment`/`remove_segment` targeting a lesson id or a segment outside the course.
  - `apply_revision` with one `add_segment` (no section_key): new segment exists, `body == ""`, meta has `segment_status=="queued"`, `custom is True`, `section == "custom:..."`; lesson `word_count` recomputed.
  - `apply_revision` with `add_segment(section_key="theory")` (enabled): meta `section=="theory"`, no `custom` key.
  - `apply_revision` with `edit_segment`: meta carries queued status + instruction + `prev_body`/`prev_title`/`refined`/`refine_instruction`; then call `undo_refine(db, segment_id)` and assert body/title restored (proves the contract match).
  - `apply_revision` with `remove_segment`: gone, sibling orders renormalised `[0..n]`, lesson `word_count` recomputed.
  - `persist_lesson` survival: lesson with one blueprint segment + one `custom:true` segment → run `persist_lesson` with a drafted-lesson dict (mirror an existing `persist_lesson` test in `tests/` — grep for it) → blueprint segments regenerated, custom segment SURVIVES, appended after, `order` sequential.
  - Transactionality: a plan whose second op is invalid at apply time (e.g. remove_segment of a bogus id that passed a weakened validate — simulate by calling `apply_revision` with a hand-built op list) rolls back the first op too.
- [ ] **2. Run** — expect failures on unknown op enum. `pytest tests/test_surgical_revise.py -q`.
- [ ] **3. Implement** per the interface block above. Schema: extend `_OP_ENUM`, add `segment_id`/`section_key` as optional schema fields (flat tagged object, constraint #9 comment at `revise.py:63` — keep that style), `_REQUIRED["add_segment"]=("lesson_id","title","instruction")`, `_REQUIRED["edit_segment"]=("segment_id","instruction")`, `_REQUIRED["remove_segment"]=("segment_id",)`. `validate_ops` resolves segment ids the way lesson ids are resolved today (read `:230-289` and mirror; blueprint via `blueprint_from_course_meta(course)` — `app/curriculum/blueprint.py:121-133`, enabled keys via its helpers `:140-188`). Slug helper: lowercase, spaces→`-`, strip non-word — a tiny local function.
- [ ] **4. Run** — all green plus `pytest tests/test_blueprint_regression.py tests/test_revise_planner.py -q` (or whatever revise test files exist: `ls tests/ | grep -i revise`) to prove no regression.
- [ ] **5. Commit** — `feat(revise): surgical segment ops — add/edit/remove with custom-segment survival`

---

### Task 2: Segment generation in the job (Spec A, generation side)

**Files:**
- Create: `apps/api/app/curriculum/segment.py`
- Modify: `apps/api/app/jobs/curriculum_revise.py` (`:63-132`)
- Modify: `apps/api/app/prompts/registry.py` (new entry `segment.generate`)
- Test: `apps/api/tests/test_surgical_revise.py` (append)

**Interfaces:**
- `segment.py` exposes `generate_segment(db, segment: Block) -> None`: reads `meta.segment_instruction`, the parent lesson (title/objective) and its sibling segments (title+body, truncated per-segment to ~2000 chars), grounds via `ground_topic(db, f"{lesson.title} {instruction}", source_ids, k=4)` (course `meta.source_ids` — how `plan_revision:309-316` reads them), one `get_provider().guided_json(messages, SEGMENT_SCHEMA, role="draft")` with schema `{title, body}` (mirror `REFINE_SCHEMA`, `refine.py:30-44`), two-message shape instruction-last (mirror `build_refine_messages:80-119`), citation handling like refine's (validated against retrieval, stored on `meta.citations`). On success: body/title set, meta `segment_status:"done"`, `segment_instruction` scrubbed, lesson `word_count`/`meets_floor` recomputed. Constants `SEGMENT_SYSTEM`/`SEGMENT_TAIL` + `SEGMENT_SLICE_ID="segment.generate"` resolved via the overrides resolver (import style per `refine.py`).
- `run_curriculum_revise_job` after apply commits: loop segments with `segment_status=="queued"` across the course (query by walking, or collect ids during apply and pass on the job progress — simplest: query `Block` joins like the seam map's lesson query, two levels down then segments). Per segment: `generate_segment`; on `LLMError`/exception mark `segment_status:"failed"` + `segment_error:str(e)` (whole-dict) and continue. Update `job.progress` counts as it goes (`{segments_total, segments_done, segments_failed}`) the way the job already writes progress (read `:63-132`).
- Conditional chaining: chain the `curriculum_draft` job ONLY if apply queued at least one LESSON (`draft_status=="queued"` exists in the course, or track during apply). Blueprint-only/segment-only plans no longer spawn no-op draft jobs.

**Steps:**
- [ ] **1. Failing tests**: (a) `generate_segment` with a monkeypatched provider (capture messages; return `{"title":"Εργασίες για το σπίτι","body":"Άσκηση 1..."}`) fills body, sets `done`, scrubs instruction, recomputes lesson word_count; captured messages contain the lesson title, sibling segment text, and the instruction; (b) provider raises `LLMError` → segment `failed` + error recorded, body untouched; (c) job-level: `run_curriculum_revise_job` in apply mode with a segment-only plan generates the segments and does NOT enqueue a draft job (assert no `GenerationJob(kind="curriculum_draft")` row); a plan with a `modify_lesson` still chains one. Mirror how existing tests invoke the job (grep `tests/` for `run_curriculum_revise_job`; monkeypatch provider + `ground_topic`).
- [ ] **2. Run** — fail (module missing).
- [ ] **3. Implement.** `SEGMENT_SYSTEM`: role statement — writes ONE section of an existing lesson for a Greek guitar course, in the course language, consistent with sibling sections, following the tutor's instruction exactly, grounded in provided passages, cite `{source_id,page}` per the passages given, return only the schema JSON. `SEGMENT_TAIL`: lesson context + sibling sections + retrieved block + the instruction LAST. Write both in the style/length of `REFINE_SYSTEM`/`REFINE_USER` (`refine.py:51-73`) — read them first; reuse their citation-block formatting. Registry entry mirrors `curriculum.refine`'s (id `segment.generate`, Greek metadata: «Η συγγραφή μεμονωμένης ενότητας μαθήματος», when-it-runs: όταν εγκρίνεται σχέδιο με προσθήκη/διόρθωση ενότητας; `build` sample; pins from grep of the committed file).
- [ ] **4. Run** — new tests + `pytest tests/test_prompts_registry.py tests/test_prompts_byte_identity.py -q` (baseline gains the `segment.generate` key — regenerate surgically, same commit).
- [ ] **5. Commit** — `feat(revise): job-side segment generation — grounded, per-segment failure isolation, conditional draft chaining`

---

### Task 3: Blueprint-aware planner (Spec B)

**Files:**
- Modify: `apps/api/app/curriculum/revise.py` (`compact_tree_text:128-152`, `REVISE_TAIL:156-177`, `build_revise_messages:189-213`, `plan_revision:295-327`)
- Test: `apps/api/tests/test_surgical_revise.py` (append) + existing revise planner tests updated where they pin the old tree/prompt text

**Interfaces:**
- `compact_tree_text` gains one indented line per segment: `    [id] title` under its lesson (keep the existing module/lesson format byte-identical otherwise — some tests pin it; update ONLY the ones that must change and name them in the commit).
- New `REVISE_BLUEPRINT_BLOCK` template: "CURRENT LESSON STRUCTURE (blueprint): enabled: <key (label)>...; disabled: <key (label)>..." — built in `build_revise_messages` from `blueprint_from_course_meta(course)`; `plan_revision` passes the course through (it already has it at `:309`).
- `REVISE_TAIL` additions (slice `curriculum.revise` default changes → regenerate its baseline key): prefer `add_segment`/`edit_segment`/`remove_segment` over `modify_lesson` when the request doesn't require re-teaching the whole lesson; a NEW recurring section across lessons = `update_blueprint` (enable/add the section) PLUS per-lesson `add_segment` with the matching `section_key` in the same plan; if the current blueprint makes a request impossible, SAY SO in `summary` — never plan around it silently.

**Steps:**
- [ ] **1. Failing tests**: (a) `build_revise_messages` output contains the blueprint block with an enabled and a disabled key from the course fixture; (b) contains segment id lines; (c) the rendered `REVISE_TAIL` (via the overrides resolver default) mentions `add_segment` and the coupled-blueprint guidance (assert two key phrases); (d) existing planner tests still pass (run and fix pins).
- [ ] **2-4. Implement / run** — including `pytest tests/test_prompts_registry.py tests/test_prompts_byte_identity.py -q` with the `curriculum.revise` baseline key regenerated in the same commit; registry pins for `revise.py:189`/`:326` refreshed if shifted.
- [ ] **5. Commit** — `feat(revise): planner sees the blueprint and the segment tree, prefers surgical ops`

---

### Task 4: Server-computed impact (Spec C, backend)

**Files:**
- Modify: `apps/api/app/curriculum/revise.py` (new pure function `compute_impact(ops) -> dict`)
- Modify: `apps/api/app/routers/chat.py` (`_validate_pending_revision:401-431` — attach `plan["impact"] = compute_impact(validated_ops)` where it already rewrites the plan at `:431`)
- Test: `apps/api/tests/test_surgical_revise.py` (append)

**Interfaces:** `compute_impact` returns `{"rewrites": n(modify_lesson+move_lesson), "segment_additions": n, "segment_edits": n, "segment_removals": n, "lesson_removals": n, "lessons_added": n(insert_lesson)+lessons-in-insert_module, "blueprint_changed": bool, "destructive": rewrites>0 or lesson_removals>0 or segment_removals>0}`. Pure, deterministic, never LLM-derived. Task 5 renders it.

**Steps:**
- [ ] **1. Failing tests**: mixed-plan impact math; the chat seam — mirror the existing `_validate_pending_revision` test (grep `tests/` for it) and assert the stored pending approval's `tool_args["plan"]["impact"]` present and correct; a surgical-only plan → `destructive is False`.
- [ ] **2-4.** Implement; run `pytest tests/test_surgical_revise.py tests/test_chat_router.py -q -k "not live"`. Line-shift: chat.py suggestions pin.
- [ ] **5. Commit** — `feat(revise): server-computed plan impact rides the approval payload`

---

### Task 5: Approval card banner + op labels (Spec C, frontend)

**Files:**
- Modify: `apps/web/src/components/chat/revision-plan-card.tsx` (banner above op list `:97`; `opLabel:38-58` gains three ops)
- Modify: `apps/web/src/components/curriculum/revise-drawer.tsx` (`blockTitles` traversal gains segments)
- Modify: `apps/web/src/lib/api.ts` (`RevisionPlanOp:1433-1457` gains `segment_id?/section_key?`; `RevisionPlan` gains `impact?`)
- Modify: `apps/web/src/messages/{el,en}.json`
- Test: extend the Playwright spec that covers the revision card (grep `apps/web/tests` for `revision-plan` / the card's testids; if none exists, add banner assertions to the drawer's spec)

**Interfaces:** banner (`data-testid="plan-impact"`) renders from `plan.impact`: when `destructive` → amber (`text-amber-...`/destructive styling per the codebase's warning idiom — find one) with el copy «Θα ξαναγραφτούν {rewrites} μαθήματα από την αρχή — το υπάρχον κείμενο θα αντικατασταθεί.» plus removals when present; else calm summary «Στοχευμένη αλλαγή: {additions} νέες ενότητες, {edits} διορθώσεις — τα υπόλοιπα μένουν ως έχουν.» Compose from parts so zero-counts drop out (ICU plural in i18n keys). Op labels: el «Νέα ενότητα σε μάθημα» / «Διόρθωση ενότητας» / «Αφαίρεση ενότητας» (+en mirrors) under `curricula.revise.op.*`.

**Steps:** TDD-ish via Playwright where cheap; `npx tsc --noEmit && npm run build`; commit `feat(revise): honest impact banner + surgical op labels on the approval card`.

---

### Task 6: Content-preserving `modify_lesson` (Spec D)

**Files:**
- Modify: `apps/api/app/jobs/curriculum_draft.py` (`_draft_one:209-211` area; `_claim:138` scrub list)
- Modify: `apps/api/app/curriculum/draft.py` (`LESSON_TAIL:125-147` conditional block; new `LESSON_REVISE_BLOCK` + slice `lesson.revise` modeled on `LESSON_DEEPEN_BLOCK:163-172`; `build_lesson_messages` param)
- Modify: `apps/api/app/curriculum/revise.py:392` (remove the dead `prev_body` write — confirmed never read)
- Modify: `apps/api/app/prompts/registry.py` (entry for `lesson.revise`, mirroring `lesson.deepen`'s)
- Test: `apps/api/tests/test_surgical_revise.py` (append)

**Interfaces:** when `_draft_one` claims a lesson whose meta had `revise_instruction` AND the lesson has existing segments with non-empty bodies, it builds `current = {section_or_title: body}` from the live segment blocks (custom ones included, truncated ~2000 chars each) and passes `revise_current=current` through to `build_lesson_messages`, which renders `LESSON_REVISE_BLOCK` ("Το τρέχον περιεχόμενο του μαθήματος ακολουθεί — εφάρμοσε την αναθεώρηση και κράτησε όλα τα υπόλοιπα ουσιαστικά ανέπαφα.\n{current}") into the tail. First drafts and instruction-less re-drafts: byte-identical to today (block absent) — pinned by test with same-kwargs comparison, the Task-4-of-P5 pattern.

**Steps:**
- [ ] **1. Failing tests**: byte-identity without instruction; block present with instruction+segments (captured provider messages contain a distinctive body string from the fixture segments); `revise.py` no longer writes `prev_body` (assert absent from meta after `apply_revision` modify_lesson).
- [ ] **2-4.** Implement; run new tests + `pytest tests/test_prompts_registry.py tests/test_prompts_byte_identity.py -q` (new `lesson.revise` baseline key; `lesson.draft`/`lesson.deepen` pins refreshed if shifted) + the draft-job test files (`ls tests/ | grep -i "draft\|curriculum_draft"`).
- [ ] **5. Commit** — `feat(draft): modify_lesson preserves current content — revise means revise, not regenerate`

---

### Task 7: Verification pass

- [ ] Full backend suite solo, adjudicate failures by name vs the known environmental baseline (~30, `*live*`/embedding class); zero new.
- [ ] `cd apps/web && npx tsc --noEmit && npm run build && rm -rf .next && npx playwright test` — only the 3 known confirm.spec.ts failures allowed.
- [ ] In-flight job check; report DEPLOY READY (no deploy — human's call; note that no migration is involved this time).
- [ ] Report to `.superpowers/sdd/task-7-report.md`.
