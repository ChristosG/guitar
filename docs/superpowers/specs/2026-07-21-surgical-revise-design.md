# Surgical Revise Engine (Design)

**Date:** 2026-07-21
**Status:** Approved by Chris ("yes bro go with the surgical revise plan, A-D")
**Problem:** The revise engine's smallest op inside an existing lesson is `modify_lesson` = full from-scratch re-draft (persist deletes every segment). The planner cannot see the course blueprint, so it proposes plans that are structurally impossible (2026-07-20 homework incident: 8 expensive rewrites, zero homework sections — `homework.enabled=false` meant the draft schema had no slot). The approval card doesn't tell the tutor what an approval operationally costs.
**Goal:** Intelligent, targeted, on-demand changes — one section or the whole curriculum — with Claude's help, never rewriting what the tutor already likes, and never approving blind.

Grounding: seam map 2026-07-21 (revise.py, draft.py, jobs/curriculum_revise.py, refine.py, revision-plan-card.tsx — file:line refs below are from it).

---

## A. Surgical ops: `add_segment`, `edit_segment`, `remove_segment`

Extend `_OP_ENUM` (`revise.py:58-61`) and `REVISION_PLAN_SCHEMA`/`_REQUIRED`:

- **`add_segment {lesson_id, title, instruction, section_key?}`** — append ONE new segment to an existing lesson.
- **`edit_segment {segment_id, instruction}`** — regenerate ONE segment's body per the instruction; everything else untouched.
- **`remove_segment {segment_id}`** — delete one segment (+ order renormalise). No LLM.

**Generation happens in the job, never in the transaction.** `apply_revision` keeps its zero-LLM contract: `add_segment` creates the segment Block with placeholder body and `meta={segment_status:"queued", segment_instruction, ...}`; `edit_segment` marks the target `segment_status:"queued"` with the instruction and stashes `prev_body`/`prev_title` using the **refine flow's exact meta contract** (`refine.py:169-175`) so the existing `POST /blocks/{id}/undo` works on surgically edited segments for free. After the apply transaction commits, `run_curriculum_revise_job` generates each queued segment **sequentially** (one small guided_json call each, `role="draft"`, refine-style two-message shape — no library prefix), grounded via `ground_topic(db, f"{lesson_title} {instruction}", course source_ids, k=4)` with the lesson's existing segments (titles + bodies) as context. Citations validated the same way refine does. Per-segment failure marks that segment `segment_status:"failed"` with the error and continues — never destructive, the placeholder/previous body stays.

**New prompt** `SEGMENT_SYSTEM`/`SEGMENT_TAIL` (slice id `segment.generate`, tutor-editable, registry entry with real pins; schema `{title, body}` like `REFINE_SCHEMA`). Registry caution: this adds a second provider call site to whichever module hosts it — put it in a NEW module `app/curriculum/segment.py` so `revise.py`'s single pinned call site (`:326`) doesn't shift.

**The custom-segment survival rule** (closes the wipe gotcha): `persist_lesson`'s delete-all (`draft.py:520-522`) is amended to **preserve segments with `meta.custom == true`**, re-appending them after the regenerated blueprint sections in their prior relative order.
- `add_segment` WITHOUT `section_key` → `meta.custom=true`, `meta.section="custom:<slug>"` → survives any future lesson redraft.
- `add_segment` WITH `section_key` (must name a section enabled in the blueprint — validated at apply time) → `meta.section=section_key`, no `custom` flag → wiped and regenerated on redraft **by design** (the blueprint owns it now; no duplicates).

**Lesson meta freshness:** after any segment op, recompute lesson `word_count` (sum of segment body words) and `meets_floor` (against existing `floor_words` if present) so the board badges don't lie. `thin_sections`/`target_words` left as-is (no live reader).

**Chaining fix:** `jobs/curriculum_revise.py:108-132` chains the draft fan-out **only when lessons were actually queued** (today it chains unconditionally, spawning no-op draft jobs for blueprint-only or segment-only plans).

## B. Blueprint-aware planner

- `build_revise_messages` (`revise.py:189-213`) gains `REVISE_BLUEPRINT_BLOCK`: the course blueprint's sections with key, Greek label, and enabled/disabled state.
- `compact_tree_text` (`revise.py:128-152`) gains segment lines — one indented `[id] title` per segment under each lesson — so `edit_segment`/`remove_segment` can be targeted by id. (Compact: one line each.)
- `REVISE_TAIL` (`revise.py:156-177`, slice `curriculum.revise`) updated: **prefer surgical segment ops over `modify_lesson`** whenever the request doesn't require re-teaching the lesson; for a NEW recurring section across lessons, propose `update_blueprint` (enable/add the section) **plus** per-lesson `add_segment` with the matching `section_key` in the SAME plan; if a request is impossible under the current blueprint, say so explicitly in `summary` instead of silently planning around it. (Slice default changes ⇒ regenerate the byte-identity baseline for `curriculum.revise` per the documented procedure.)

## C. Honest approval card

- **Server-computed impact** — never trusted from the LLM. In `_validate_pending_revision` (`routers/chat.py:401-431`), after `validate_ops`, compute and attach `plan["impact"]`: `{rewrites, segment_additions, segment_edits, segment_removals, lesson_removals, lessons_added, blueprint_changed, destructive}` where `destructive = rewrites > 0 or lesson_removals > 0 or segment_removals > 0`. Riding inside `tool_args.plan` means no schema change and it reaches `RevisionPlanCard` automatically (`chat-panel.tsx:540`).
- **`RevisionPlanCard`** (`revision-plan-card.tsx`) renders an impact banner above the op list: destructive → amber, Greek, explicit: «Θα ξαναγραφτούν N μαθήματα από την αρχή — το υπάρχον κείμενο θα αντικατασταθεί.» / removals named; surgical-only → calm: «Στοχευμένη αλλαγή: +N ενότητες, N διορθώσεις — τα υπόλοιπα μένουν ως έχουν.» New `opLabel` entries + i18n (el primary) for the three ops; `blockTitles` map (from `revise-drawer.tsx`'s tree traversal) extended to include segment titles.

## D. Content-preserving `modify_lesson`

When a lesson re-drafts with a `revise_instruction` and has existing ready segments, the current content reaches the model: `_draft_one` (`jobs/curriculum_draft.py:209-211`) builds `previous` from the lesson's live segments (section → title/body dict) and `build_lesson_messages` renders a new `LESSON_REVISE_BLOCK` (modeled on `LESSON_DEEPEN_BLOCK`, `draft.py:163-172`; own slice `lesson.revise`, registry entry): "the lesson's current content follows — apply the requested revision and keep everything else substantively intact." Plain re-drafts (no instruction) and first drafts are byte-identical to today (no block).
- The dead `prev_body` write (`revise.py:392`, never read — confirmed) is **removed** in the same change; `previous` is built from live segments, which is strictly better data.

## Out of scope

- Planner-side automatic plan splitting/batching; multi-level undo; recomputing `est_minutes` distribution; suggestions-chip awareness of revise state (carried minors list).

## Ordering & interactions

Build order **A → B → C → D** (A is usable alone via chat once ops exist; B makes the planner produce them; C is safety UX; D completes `modify_lesson`). A's custom-survival rule must land WITH or BEFORE B (B's coupled `update_blueprint`+`add_segment` plans depend on `section_key` semantics). Tests throughout: ops schema validation, apply-side segment queuing, job-side generation with fake provider, custom-segment survival through `persist_lesson`, impact computation, planner-prompt blocks (byte-identity where unchanged), content-preservation block gating, i18n parity, Playwright for the card banner.
