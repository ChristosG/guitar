# Full Tutor Control — Curricula (Design)

**Date:** 2026-07-20
**Status:** Approved by Chris (design discussion 2026-07-20)
**Goal:** The app is a lesson-crafting instrument for a Greek-speaking guitar tutor. He must be able to steer curricula end-to-end: shape the idea in conversation *before* generation, revise in conversation *after* generation, control presentation (no citation clutter), manage lifecycle (rename/delete), and take the result out of the app (DOCX for Word/Pages). Everything must remain CPU-only and compatible with the future bundled iMac app.

---

## 1. Fix the fabrication-guard misfire (Revise chat unusable)

### Problem (root-caused)
The named-song guard `looks_like_named_song_request` (`apps/api/app/agent/guards.py:310-337`) runs **pre-model** in both chat entry points (`apps/api/app/agent/loop.py:659-660` and `:895-896`) against `messages[-1]["content"]`. But for revise-drawer sessions, `_inject_curriculum_context` (`apps/api/app/routers/chat.py:253-289`, called at `:621` and `:682`) has already appended the **entire serialized course tree** to that same content. A guitar curriculum's lesson titles reliably contain trigger words (*intro, riff, solo, verse, chorus, bridge, outro*), so the guard fires on **every** turn, deterministically, regardless of what the tutor typed. The model is never called; the hardcoded `NAMED_SONG_DECLINE_MESSAGE` (`guards.py:340-348`) is returned verbatim. This is why the Greek "remove the inline citations" request got an identical tab-refusal twice.

The invariant "the guard must judge the tutor's own words" exists only as a comment (`loop.py:641-643`) and only protects against the loop's *own* grounding tail — not against upstream injection by the router.

### Fix
- The guard evaluates **only the raw tutor message**. The chat router passes the pre-injection user text alongside the enriched message (same pattern `loop.py` already uses to keep `last` bound to the pre-grounding dict). Both `run_agent_turn` and `stream_plain_turn` use the raw text for `looks_like_named_song_request`.
- The injected block keeps its `[CURRICULUM CONTEXT …]` sentinel; as defense-in-depth the guard also strips any such sentinel-delimited tail if it ever receives enriched text.

### Regression tests
1. Greek revise request ("αφαίρεσε τα inline citations") + injected curriculum whose lesson titles contain *intro/solo/riff* → **must reach the model** (guard does not fire).
2. Genuine fabrication request ("γράψε μου το tab για το Nothing Else Matters") → still declined with `NAMED_SONG_DECLINE_MESSAGE`, both endpoints (REST + stream).
3. Same as (2) but in a revise session with curriculum injection → still declined (injection must not *mask* real triggers in the raw text).

---

## 2. Citations: remove per-section pills, add per-lesson "Πηγές" modal

### Facts (from code exploration)
Inline citation markers do **not** exist in lesson prose. The generator emits structured per-section citations (`{source_id, page}`) validated against the library page index (`apps/api/app/curriculum/draft.py:261-316`), expanded and stored on `segment.meta.citations`, with the per-lesson union on `lesson.meta.citations` (`draft.py:542-589`). The frontend renders them as `ProvenanceChips` pills under **every section** (`apps/web/src/components/curriculum/provenance-chips.tsx`, used at `apps/web/src/components/curriculum/block-card.tsx:506`). The visual clutter is a display choice, not content.

### Change (frontend-only)
- Stop rendering `ProvenanceChips` at segment level in the curriculum board.
- Each **lesson** card gets one compact "Πηγές" button (rendered only when `lesson.meta.citations` is non-empty). Clicking opens a modal:
  - Deduped by source: "«Τίτλος βιβλίου» — σελ. 56, 57, 61".
  - Each page entry deep-links to `/{locale}/library/{source_id}?page={page}` (existing reader route) — the verification story is preserved, one click away instead of everywhere.
- No backend change, no data migration. `meta.citations` remains the single source of truth (also consumed by the modal and untouched by DOCX export).
- i18n: EL + EN strings for button and modal.

---

## 3. Rename & delete curricula

### Facts
A curriculum is a root `Block` (`kind="course"`, `apps/api/app/models/block.py:7`); there is no dedicated curriculum table. Generic block routes already support rename (`PATCH /blocks/{id}`) and cascading delete (`DELETE /blocks/{id}`) (`apps/api/app/routers/curriculum.py:723`, `:758`), but nothing validates course-root-ness, and rows pointing at the root via FK-less `root_id` columns would be orphaned: `ChatSession.root_id` (`models/chat.py:81`), `CurriculumInterview.root_id` (`models/interview.py:107`), `GenerationJob.result_root_id` (`models/generation_job.py:44`).

### Change
- **`PATCH /curricula/{root_id}`** — rename. Validates the target is a template course root; rejects empty titles (consistent with `BlockUpdate` 422 behavior).
- **`DELETE /curricula/{root_id}`** — validates course root, deletes the block subtree (existing cascade), and cleans up associated `ChatSession` rows (with their messages), `CurriculumInterview` rows, and nulls/marks `GenerationJob.result_root_id` pointers. Refuses to delete while a draft job for that root is actively running (clear Greek error) — deleting mid-draft would strand the worker.
- **UI:** "⋯" menu on each curricula-index card (`apps/web/src/app/[locale]/(cockpit)/curricula/page.tsx`) and on the detail page header:
  - *Μετονομασία* → inline dialog, pre-filled with current title.
  - *Διαγραφή* → confirmation modal naming the curriculum ("Θα διαγραφεί οριστικά το «X» και όλα τα μαθήματά του."), destructive-styled confirm.
- Frontend API wrappers (`renameCurriculum`, `deleteCurriculum`) in `apps/web/src/lib/api.ts`; index list updates optimistically after either action.

---

## 4. DOCX export

### Decision
Backend-generated via **python-docx** (new dependency; pure-Python, CPU-only). Backend generation beats a frontend JS docx library because it is one source of truth, testable server-side, and works identically inside the future bundled (Tauri) app.

### Endpoint
`GET /curricula/{root_id}/export.docx` → streams a `.docx` (proper `Content-Disposition` filename from the curriculum title, ASCII-safe fallback + UTF-8 `filename*`).

### Document shape (approved: clean, **no sources anywhere**)
- **Title page:** curriculum title, target profile / description, module & lesson counts, total estimated time.
- **Heading 1** per module (page break before each).
- **Heading 2** per lesson, with estimated minutes.
- **Heading 3** per section using the blueprint label (Θεωρία, Ασκήσεις, …), followed by the section's prose (`segment.body` — already rendered text with exercises/Q&A folded in by `_render_section`, `draft.py:460`).
- No citation data of any kind in the document.
- Greek text throughout; styles chosen for Word *and* Apple Pages compatibility (standard built-in heading styles, no exotic features).
- Lessons still drafting/failed: exported with a one-line Greek placeholder note rather than silently skipped, so the document structure always matches the curriculum.

### UI
"Λήψη DOCX" button on the curriculum detail page header.

### Tests
Round-trip test: generate a small fixture curriculum → export → re-open with python-docx → assert heading hierarchy, order, Greek content intact, zero occurrences of source titles/page numbers.

---

## 5. Planning chat — "Συζήτησέ το πρώτα" (pre-generation)

### Decision (approved)
Optional conversational step at the **start** of the Generate wizard (the interview, `apps/web/src/components/curriculum/interview-dialog.tsx`, server-driven steps `who → duration → scope → structure → sources → outline → confirm`). The chat's conclusion is distilled into an **editable brief** — the tutor audits and corrects what the machine understood *before* generation spends time and money. (Alternative "chat drafts the outline live" was considered and rejected: much bigger build, overlaps the blueprint editor and post-gen revise.)

### Flow
1. Interview intro offers, alongside the normal path: *"Θέλεις να το συζητήσουμε πρώτα;"* — entirely optional; skipping it is exactly today's flow.
2. Opens a chat (reusing the existing chat infra/panel that the revise drawer uses) bound to the interview, Greek-first, grounded in the tutor's library (retrieval on, same guards — which after §1 judge only his words).
3. When ready, **"Χρησιμοποίησε αυτό το σχέδιο"**: one LLM call distills the transcript into a structured Greek brief — goals, topics to cover, emphasis/priorities, teaching preferences, things to avoid.
4. The brief renders as **editable text**; the tutor corrects it, then continues to the normal interview steps.
5. The approved brief is stored on the `CurriculumInterview` row and injected into outline generation context (the `brief` seam already exists in the generation path — `CurriculumGenerateRequest.brief`, `schemas/curriculum.py:16`; the interview's outline job gets the same input).
6. The distillation prompt joins the prompt registry (tutor-editable in Settings, like the other 30+ prompts, with restore-default).

### Data
- `CurriculumInterview` gains a nullable `planning_brief` (text) — one small migration.
- Chat session bound via a new nullable `ChatSession.interview_id` pointer, mirroring the existing `ChatSession.root_id` binding used by the revise drawer (same migration as `planning_brief`).

### Error handling
- Distillation failure → error surfaced in the chat, transcript preserved, retry offered; never blocks the skip path.
- Brief too long → soft warning, hard cap consistent with prompt-budget realities.

### Tests
- Interview with brief → brief text present in outline-generation prompt assembly.
- Interview without brief → byte-identical prompt assembly to today (regression guard, same discipline as the blueprint's byte-identity test).
- Distillation endpoint: transcript in → structured Greek brief out (schema-validated).

---

## 6. Out of scope (this round)

- **iMac bundling (Tauri/docker-compose):** sketches exist; nothing here blocks it. The DOCX decision (backend-side) deliberately keeps export working in that future.
- **Unit B quote-anchor resolver:** deliberately deferred, not just postponed by default. Rationale: with citations now behind a per-lesson modal and absent from DOCX, the payoff of exact-quote highlighting (vs. the existing page-level deep-links) has shrunk; it also still gates on the Gallagher recompile spend decision. Promote it only if page-level links prove insufficient for the tutor in practice.

---

## Build order

1. **§1 guard fix** — small, unblocks the already-built revise engine; highest value per line.
2. **§2 citations UX + §3 rename/delete** — small, independent, can land together.
3. **§4 DOCX export.**
4. **§5 planning chat** — the biggest piece, last.

Each stage lands, tests green (backend suite + Playwright), and deploys separately.
