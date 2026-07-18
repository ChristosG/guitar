# Curriculum Authoring Control — Design

**Date:** 2026-07-18
**Goal:** Give the tutor ultimate, legible control over how curricula are crafted — the prompts, the lesson structure, and the ability to revise a finished curriculum in conversation — without breaking the machinery that makes lessons hit their target length and cite real pages.

**Status:** Approved in direction by Chris (build all four units, order A→C→D→B). This spec decomposes into four plans, each independently buildable and testable.

---

## Global Constraints (bind every unit)

- **CPU-only, no GPU, ever.** The app ships on a 2022+ laptop/tablet. No local LLM/VL (Qwen-VL is dropped). All AI goes through the provider layer (`get_provider()` / `LLM_PROVIDER`): `claude -p` now, a real Claude API key when the app ships. Embeddings (e5-small ONNX) and BM25 stay (CPU). Nothing added may require a GPU. See memory `guitar-tutor-cpu-only-deployment`.
- **Grounding is reused, never re-invented.** Every new generation path calls `build_curriculum_context(db, source_ids)` (`app/curriculum/corpus.py:263`) + `prefix_messages(...)` (`corpus.py:399`) so it rides the same warm 90K-token cache and the same `page_index` citation validation (`draft.py:260-292`). `source_ids` comes from `course.meta["source_ids"]`, preserving the `None`-vs-`[]` distinction as `extend.py:322` does.
- **Non-destructive by default.** No feature silently re-drafts or overwrites existing curriculum content. Structural change → explicit tutor action, with a preview and (where it costs tokens) an approval gate.
- **`Block.meta` is plain `sa.JSON` (no `MutableDict`).** Every write reassigns the whole dict (`block.py:39-44`).
- **Migrations are additive only.** `alembic upgrade head` runs in the api boot CMD; the DB must never be ahead of the image.
- **Provider is swappable.** No feature hardcodes `claude_cli`; all go through `get_provider()`.

---

## Data Model Recap (from exploration)

A whole curriculum is one self-referential `block` tree (`app/models/block.py`), no `Course`/`Lesson`/`Section` tables:

```
course (is_template=True, parent_id=None)  →  module  →  lesson  →  segment (== a lesson SECTION)
```

- `order:int` sequences **siblings only** (`_renormalise` is scoped to one `parent_id`, `edit.py:42`).
- `meta:JSON` carries `brief`, `shape`, `source_ids`, `library`, `gap_policy`, per-block `tier`/`draft_status`/`word_count`/`prev_body`/`citations`, and `meta["section"]` (machine key) on segments.
- "Who is this course for" = `meta["brief"]` (free text; reaches the outline prompt AND every lesson-draft prompt) + `target_profile` (audience JSON), both on the course root.
- **"Week" is not stored** — derived: `lessons_total = weeks × sessions_per_week`; a lesson == a session. Teaching order = `module.order` then `lesson.order`.
- Sections on disk = child `segment` blocks, exploded by `persist_lesson` (`draft.py:482-555`). In flight = a JSON object matching `LESSON_DRAFT_SCHEMA` (`depth.py:137`).
- Everything expensive is async: `202` + a `GenerationJob` row + poll `GET /jobs/{id}`.

---

## Unit A — Curriculum Screen Redesign

**Problem:** One page (`app/[locale]/(cockpit)/curricula/page.tsx`) shows all curricula as an always-visible, unbounded `flex-wrap` of cards; selecting one expands `TreeBoard` inline. It doesn't scale and there's no per-curriculum home for the revise/chat features.

**Design:** Follow the existing detail-route convention used by `lessons/[lessonId]`, `students/[id]`, `library/[sourceId]`:

- **New route** `app/[locale]/(cockpit)/curricula/[rootId]/page.tsx` — client component, reads `useParams<{ rootId }>()`, `getCurriculum(rootId)` (already returns the full `BlockNode` tree), renders `TreeBoard` + `DraftProgressBar`, with a back `<Link href={`/${locale}/curricula`}>`. This becomes the home for Units C (a "curriculum prompts" affordance) and D (the revise chat drawer).
- **List page becomes an index:** the "Build a curriculum" trigger (`InterviewDialog`) + a searchable/filterable grid of curriculum cards. Each card is a `<Link>` to `/${locale}/curricula/${id}` (replacing the inline `handleSelectTemplate`/`activeTree` state). Add a client-side search box (title filter) so many curricula stay navigable.
- **Preserve** all existing `TreeBoard`/`BlockCard` edit affordances (rename, edit body, reorder, add lesson/module, deepen, delete, segment, attach artifact, extend-with-chat) — they move to the detail screen unchanged.

**Testing:** Playwright — index lists cards, clicking navigates to `/curricula/[id]`, back button returns; the tree renders on the detail route; search filters the list.

---

## Unit C — Prompt & Blueprint Visibility

Two coordinated pieces: **C1** surfaces the curriculum-shaping *prompts*; **C2** is the *lesson blueprint* (the section skeleton, made tutor-editable and per-curriculum).

### C1 — Curriculum prompt group in Settings

- **A "Curriculum" filter/section** in the prompts settings (`components/settings/prompt-list.tsx`) that surfaces only the curriculum-crucial registry entries: `curriculum.outline`, `lesson.draft` (and its spliced parts — `lesson.tier_library`, `lesson.tier_web`, `lesson.gap`, `lesson.deepen`, `lesson.repair`), `curriculum.extend`, and the shared prefix (`curriculum.system`, `curriculum.library`).
- **Assembled/rendered view, not raw templates.** The registry already renders prompts with example values (baseline fixture). Present the rendered prompt with `{var}` slots shown as **read-only chips** (e.g. «the tutor's brief», «the library»), and the static prose between them **editable**. Editing writes a prompt override (existing `prompt_override` mechanism) on the static segments; variables stay intact so interpolation is safe. This satisfies "no `{var}` noise, edit on demand" while keeping the render honest.
- **Wizard step-3 button:** on `interview-scope-step.tsx`, a "See / edit the prompts that will build this curriculum" button that deep-links to the Curriculum prompt group (or opens it in a modal).

**Testing:** the filter shows exactly the curriculum subset; editing a static segment persists an override and the mutated text reaches the model (mutation proof, as done for the prompt-transparency work); the step-3 button opens the group.

### C2 — The Lesson Blueprint (per-curriculum, non-destructive)

**The core change.** The 8-section skeleton is hardcoded today in `depth.py` (`SECTION_WEIGHTS` :64, `LESSON_DRAFT_SCHEMA` :137) + `draft.py:425` (`SECTION_LABELS`), forced via `guided_json`. Lift it into **data**:

- **Blueprint shape** (stored JSON):
  ```json
  {
    "version": 1,
    "sections": [
      {"key": "warm_up", "label": {"el": "Ζέσταμα", "en": "Warm-up"},
       "description": "5 minutes of playing to open the session...",
       "weight": 0.07, "kind": "prose", "audience": "teacher", "enabled": true},
      {"key": "exercises", "label": {...}, "description": "...",
       "weight": 0.22, "kind": "exercises", "audience": "student", "enabled": true},
      {"key": "qa_prompts", "label": {...}, "description": "...",
       "weight": 0.06, "kind": "qa", "audience": "teacher", "enabled": true}
    ]
  }
  ```
  `kind` ∈ `prose` | `exercises` | `qa`. `exercises` and `qa` keep their structured `items[]` schema (the `exercises.items[]` and `qa_prompts.items[]` shapes from `depth.py`); only `prose` sections are freely add/remove/rename/reweight. `audience` drives the teacher-script vs student-handout split (currently hardcoded by key).
- **Default blueprint** = today's 8, built once from `depth.py` constants. Stored in a new single-row table `blueprint_default` (additive migration) with restore-to-code-default (mirror the prompt-override restore pattern). This is the settings-level default for *new* curricula.
- **Per-curriculum storage:** the chosen blueprint is written to `course.meta["blueprint"]` at `materialize_outline` time (`outline.py:369`). **Courses without `meta["blueprint"]` fall back to the code default** → existing curricula are untouched and backward-compatible by construction.
- **Schema + measurement built from the blueprint:**
  - `build_lesson_schema(blueprint) -> dict` replaces the module-level `LESSON_DRAFT_SCHEMA` constant (prose sections via `_prose_section(description)`; `exercises`/`qa` via their fixed structured builders keyed by `section.key`; `required` = enabled section keys).
  - `SECTION_WEIGHTS`/`SECTIONS`/`SECTION_LABELS` become functions of the blueprint. `measure(lesson, blueprint, teaching_minutes)` reads weights from the blueprint.
  - `persist_lesson` (`draft.py:482`) titles/keys segments from the blueprint.
  - `draft_lesson`/`build_lesson_messages` receive the blueprint (threaded through the draft fan-out payload in `curriculum_draft.py`, alongside `course_brief`), and the per-section descriptions flow into the prompt.
- **Wizard:** a new optional, skippable **"Lesson structure"** step (pre-filled with the default), whose result is stored in `interview.answers` → `materialize_outline`. Most tutors skip it.
- **Settings:** the same blueprint editor edits the `blueprint_default` (the default for future curricula), with restore-to-default + confirm modal.
- **Re-draft is opt-in:** editing a curriculum's blueprint after drafting does NOT re-draft. A "Re-draft lessons under the current structure" button (reusing the resume/deepen requeue machinery) is the only path that rewrites existing lessons, behind a count-aware confirm.

**Testing:** default blueprint reproduces today's exact 8-section schema (golden test against current `LESSON_DRAFT_SCHEMA`); a course with no `meta["blueprint"]` drafts identically to before (regression); adding a prose section makes the model emit it and `measure` counts it; existing curricula are unchanged when the default is edited; re-draft is only triggered by the explicit button.

---

## Unit D — Revise Engine + Per-Curriculum Chat

Two pieces: **D1** the revision backend; **D2** the chat surface that drives it.

### D1 — Revision backend (`POST /curricula/{root_id}/revise`)

Currently MISSING: no endpoint reads a whole curriculum and revises it; no LLM decides *where* to insert; resequencing is within one parent only.

- **Endpoint:** `POST /curricula/{root_id}/revise` (async, `202` + `GenerationJob(kind="curriculum_revise")`). Body: `{instruction: str, mode?: "plan" | "apply", plan?: RevisionPlan}`.
- **Planner** (`app/curriculum/revise.py`): serialize the current tree compactly (module/lesson `id` + `title` + `objective` + `summary`; NOT full bodies unless a `modify` targets one) + `build_curriculum_context(source_ids)` + `prefix_messages` + the tutor's instruction → `guided_json(REVISION_PLAN_SCHEMA, role="plan")`. Returns a **plan**, mutates nothing.
- **`REVISION_PLAN_SCHEMA`** → `{summary: str, ops: RevisionOp[]}` where `RevisionOp` is a tagged union:
  - `insert_lesson {module_id, after_lesson_id|null, title, objective, reason}`
  - `insert_module {after_module_id|null, title, objective, tier, lessons:[{title,objective}], reason}`
  - `modify_lesson {lesson_id, instruction, reason}` (feeds `refine`/`deepen`)
  - `move_lesson {lesson_id, to_module_id, after_lesson_id|null, reason}` (**new** cross-parent capability)
  - `remove_lesson {lesson_id, reason}`
  Every op carries a human-readable `reason` (the "why"). All `*_id`s validated against real blocks under this root before the plan is returned (drop invalid ops + log).
- **Apply** (`mode:"apply"` with an approved `plan`): execute ops in order in one transaction — reuse `edit.add_lesson`/`add_module` (positional `after` + `_renormalise`); extend `edit.py` with `move_block(block_id, new_parent_id, after)` that renormalizes BOTH the old and new parent. New/changed lessons are marked `queued`; then chain the ordinary draft fan-out (`run_curriculum_draft_job`) so the progress bar fills them in. Weeks re-derive automatically (no week column).
- **Grounding & citations:** the same `page_index` validation applies to any drafted content. "Why it belongs" is answered from the actual curriculum + the tutor's own books.

**Testing:** planner returns a valid plan whose ids all resolve; apply inserts at the right position and renormalizes; `move_lesson` across modules renormalizes both parents; invalid ids are dropped not applied; applied lessons enter the draft queue.

### D2 — Per-curriculum chat drawer

Reuse the existing chat infrastructure (`components/chat/chat-panel.tsx`: transcript, `streamChatMessage`, `ApprovalCard` HITL gate, job polling) scoped to one curriculum.

- **Two new agent tools** (`app/agent/tools.py`): `propose_curriculum_revision(root_id, instruction)` → returns the plan (read-only; answers "should I add this?"); `apply_curriculum_revision(root_id, plan)` → **gated by `ApprovalCard`** (existing HITL path for mutating tools) → applies + kicks the draft job. Adding tools re-mints the chat cache prefix once (known, acceptable).
- **Binding:** a chat session opened from a curriculum carries its `root_id` in context; the agent's default `source_ids` = the course's; the current tree + `brief` are injected so the agent reasons about *this* curriculum. It can also `search_knowledge`/`search_concepts` to ground answers.
- **UX:** a right-side **drawer** on `curricula/[rootId]` (not a separate route) so the tutor reads the curriculum while revising it. The plan renders as a `RevisionPlanCard` (reuse `ApprovalCard`) with per-op reasons and Approve/Reject; on approve the tree updates and the progress bar shows new lessons drafting.
- Flow matches Chris's: "should I add this?" → grounded answer → "yes, and also this" → refined plan → "give me the new curriculum" → approval → apply.

**Testing:** the two tools appear in the registry and pass the completeness guard; propose mutates nothing; apply is approval-gated; an end-to-end Playwright run: open drawer, ask to add a topic, approve, see a new lesson appear queued.

---

## Unit B — Quote-Based Citations

**Problem:** the compile model cites the *printed folio* instead of the injected `[p.N]` marker; `strip_printed_folio` fixed 4/5 books but Gallagher (388pp / 366K tok) reconstructs the numbering and stays offset ~+24. It's a *numbering* bug, not OCR quality.

**Design:** the model stops emitting a page *number* and instead emits a verbatim **anchor** quote per claim; Python resolves the page deterministically.

- **Compile prompt (`app/canon/compile.py`):** each claim carries `anchor` = a verbatim quote (8–15 words) copied exactly from the **author's** text (never from inside a `[FIGURE]…[/FIGURE]` region — that's our description, and citing it would be fabrication). The concept text stays "in its own words." The model may still emit `[p.N]` but it is ignored.
- **Resolution pass (`app/canon/resolve.py`, pure CPU, no LLM):** build a per-source index of `page_no → normalized page text` (author text only, figure regions stripped). For each `anchor`, normalize (lowercase, collapse whitespace) and `rapidfuzz` partial-match across pages, sliding across page boundaries; pick the best page above a threshold (start ~88, calibrate). Store `pages=[resolved]` on `concept_claim`. No match above threshold → drop the citation, keep the claim with empty `pages`, and log (this also kills hallucinated pages — a made-up quote matches nothing).
- **Recompile Gallagher** via `claude -p` (`role="compile"`, 1800s). Acceptance = rare-token test: a claim about the "Lacey Act" must resolve to Gallagher physical **p.52** (not ~28). Optionally roll quote-based to all 5 books for uniformity (the other 4 already resolve via `strip_printed_folio`, so this is cleanup, not urgent).
- **No API key** (Chris keeps the subscription); the resolution pass is provider-independent anyway.

**Testing:** unit tests for `resolve` (exact match, whitespace/case noise, page-boundary span, no-match→drop, figure-region exclusion); the Gallagher acceptance test on rare tokens; the existing folio tests stay green.

---

## Decomposition into Plans

Four plans, built A→C→D→B:

1. **Plan A** — Curriculum screen redesign (frontend only).
2. **Plan C** — C1 prompt visibility + C2 lesson blueprint (backend schema-from-blueprint + storage + wizard step + settings default; frontend editor).
3. **Plan D** — D1 revise backend + D2 per-curriculum chat drawer.
4. **Plan B** — Quote-based citations + Gallagher recompile.

Each plan is executed via `superpowers:subagent-driven-development` (fresh implementer per task, task review, final whole-branch review), ledger at `.superpowers/sdd/progress.md`.

---

## Deferred / Open

- **Full re-draft under a changed blueprint for an existing course** ships as an opt-in button (C2); a *bulk* "restructure everything" is out of scope.
- **`remove_lesson`/`move_lesson`** in the revise plan are supported but the UI defaults to additive suggestions; destructive ops always need explicit approval.
- **Rolling quote-based citations to all 5 books** is cleanup after Gallagher is proven.
- **Length control UI:** lesson length is already tutor-controlled via the wizard duration step (derived + Python-enforced); no change needed, but C1 should make that legible.
