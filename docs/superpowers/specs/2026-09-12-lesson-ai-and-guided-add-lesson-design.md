# Lesson-scoped AI, hand-edit propagation, and guided add-lesson

**Date:** 2026-09-12 · **Branch:** `desktop` · **Status:** approved by Chris (this session)

## 0. Why

The tutor hand-edited the Theory section of «Τα εμβληματικά μοντέλα: Stratocaster,
Telecaster, Les Paul, SG» in curriculum «edited test» (a faithful copy of his
«Guitar Tone (1) (αντίγραφο)»), then asked the revise chat («Αναθεώρηση με AI») to
rewrite the lesson's other sections to match. Nothing happened. He also asked why
«Προσθήκη μαθήματος» produces a random lesson when he has a page-long brief for it.

Verified root causes (live webapp logs + DB, 2026-09-11 17:00 UTC):

1. The chat's first move was `get_curriculum`, a read tool that returns **every
   block body** in the course. `agent/loop.py::_stringify` serialises tool results
   with `json.dumps(result, default=str)` — `ensure_ascii=True` — so 488,708 chars
   of Greek became 2,310,878 chars of `\uXXXX`. The next model call was
   "~1,826,053 tokens (limit 1,000,000)". The bridge classified it `upstream`,
   the router returned 502, the panel polled `/pending` for 10 minutes for a
   reply that could never arrive. No assistant row, no approval row.
2. Even had the planner run, `compact_tree_text` shows it **titles and section
   names only** — it cannot see one character of the edited theory. The only
   path that shows a model the edited theory next to the sections it must
   rewrite is `modify_lesson`, which deletes every segment and regenerates all
   of them from a prompt promise ("keep the rest intact").
3. Hand edits leave **no trace**: `PATCH /blocks/{id}` is a bare `setattr`. No
   previous-body snapshot, no tutor-edited marker, no word-count refresh (hence
   the stale «3.056 λέξεις»). Every redraft path silently overwrites his work.
4. «Προσθήκη μαθήματος» sends only `title: "Νέο μάθημα"`. The draft prompt gets
   `POSITION: lesson 3 of 4 in module 2 of 5` and **no sibling titles**, so it
   cannot avoid repeating them. The lesson `objective` already flows end-to-end
   into `LESSON_TAIL.{lesson_objective}`; no UI collects it.
5. The planner runs **synchronously inside the chat HTTP request** (up to
   1200 s under `claude_cli`), behind nginx (60/300 s) and Cloudflare (~100 s).
   The async plan-mode job (`jobs/curriculum_revise.py`, `params["plan"] is None`)
   exists and the chat never uses it.

Also settled this session: the webapp containers run `desktop` HEAD (eaa7dd3,
2026-08-07); the tutor's dmg and Chris's deb are byte-identical to
`desktop-v0.4.0` (54747ce, 2026-08-03). Nothing needs porting backwards. The
tutor's data was ported webapp → local deb (see memory
`guitar-tutor-data-port-2026-09-12`); the Mac still needs a Settings → Restore.

## 1. Product decisions (Chris, this session)

- Propagation is **preview → one approve**, with a free-text box on the card so
  he can add something for the model before approving or re-planning.
- The per-lesson surface is an **instruction box + plan card**, not a chat. If
  he wants a change he types in the card's box and either re-plans or applies
  with the note. After apply, «Τι άλλαξε;» + restore toggle, and he can run again.
- Test on the **webapp first** (rebuild compose), then tag `desktop-v0.5.0`.
- The «Έτοιμο» and «N λέξεις» badges on the lesson row are replaced by the
  AI button. Status pill stays only while not ready (queued/drafting/failed).
  Word count moves inside the expanded lesson body and is kept live.

## 2. Non-goals

- No chat history inside the lesson panel (no transcript, nothing to poison).
- No new lesson sections invented by the lesson panel (blueprint changes stay
  in the curriculum-level revise drawer).
- No cross-lesson propagation ("update other lessons that mention X") — the
  panel is scoped to one lesson by construction.
- No worker process; everything stays `BackgroundTasks` + boot sweeps.
- No change to plain chat streaming; only the revise drawer's turns go async.

## 3. Architecture overview

```
Lesson row ──[AI στο μάθημα]──▶ LessonAiPanel (fixed aside, like ReviseDrawer)
                                   │ instruction (+ chip: propagate my edits)
                                   ▼
                   POST /blocks/{lesson}/ai/plan ──▶ job lesson_ai (mode=plan)
                                   │ poll GET /jobs/{id} → progress.plan
                                   ▼
                              LessonPlanCard  (per-section rewrite/keep, note box,
                                   │           «Ξαναφτιάξε το πλάνο» / «Εφαρμογή»)
                                   ▼
                   POST /blocks/{lesson}/ai/apply ─▶ job lesson_ai (mode=apply)
                                   │ snapshot prev_segments → ONE partial draft call
                                   │ (schema = ticked sections; others fixed text)
                                   ▼ persist in place (ids kept) → word count → board refresh

Module ⋯ «Προσθήκη μαθήματος» ─▶ AddLessonDialog (brief, title?, position)
        └─ «Δημιουργία με AI» ─▶ POST /blocks/{module}/lessons/generate
                                  ─▶ job lesson_generate: plan {title, objective}
                                     → _add_lesson(meta.brief) → chained
                                     curriculum_draft {root_id, lesson_ids:[id]}
```

Foundations both flows stand on: tutor-edit tracking on `PATCH` (Unit 1) and
`persist_lesson` writing **around** fixed sections (Unit 1), plus the revise
chat repairs (Unit 0) so the existing drawer is usable again.

## 4. Unit 0 — Revise chat repairs

### 4.1 Tool results
- `agent/loop.py::_stringify`: `json.dumps(result, default=str, ensure_ascii=False)`.
- New constant `TOOL_RESULT_MAX_CHARS = 60_000` in `agent/loop.py`. In
  `_dispatch_read_call`, a longer result is cut at that length and suffixed
  with `\n…[το αποτέλεσμα περικόπηκε στους {n} χαρακτήρες]`. Applied to the
  persisted row too (the persisted transcript must equal what the model saw).
- `agent/tools.py::get_curriculum` becomes **body-free**: same shape as
  `revise.compact_tree_text` (ids, titles, objectives, section titles). Its
  description says so and points at the new tool.
- New read tool `get_lesson(lesson_id)` → `{id, title, objective, module_title,
  sections:[{id, section, title, body, tutor_edited: bool}]}`. Registered with
  `kind="read"`; covered by the registry completeness test.

### 4.2 Transcript window
- `agent/transcript.py::window_wire` gains `max_chars: int = 240_000` alongside
  the 60-message cap. Walk from newest to oldest accumulating `len(content)`;
  stop including once over budget, but **never split a tool-call/tool-result
  pair** (drop both or keep both). The system message and the last user
  message are always kept.

### 4.3 Error taxonomy
- `tools/claude_bridge/bridge.py::_classify`: match `Prompt is too long` (and
  `prompt is too long`/`context length`) → `kind="too_long"`.
- `app/llm/errors.py` (wherever `LLMError` kinds live): add `too_long`.
  `ClaudeProvider._mapped_errors`: the SDK's 400 with `prompt is too long` →
  `too_long`.
- `routers/chat.py::post_message`: `LLMError(kind="too_long")` → HTTP 413,
  `{"code": "conversation_too_long", "message": …}`.
- Web: `chat-panel.tsx` renders `chat.errors.conversationTooLong` (el: «Η
  συζήτηση μεγάλωσε πολύ για το μοντέλο. Ξεκίνα νέα συζήτηση από το κουμπί
  «Καθαρισμός».») and does NOT enter the slow-turn poll for a 413.
  `lib/job-errors.ts` maps `too_long` for jobs.

### 4.4 Drawer turns as a job
- New job kind `chat_turn`, runner `app/jobs/chat_turn.py::run_chat_turn_job`,
  params `{session_id, text, locale}`. The runner performs exactly what
  `post_message` does after persisting the user row (guards, injection,
  `run_agent_turn`, `persist_new_messages`, open `ApprovalRequest`,
  `_validate_pending_revision`) inside its own `SessionLocal()`, and writes
  `progress = {"phase": "done", "status": <ChatTurnOut.status>, "job_id":
  <apply job id if job_pending>}`. Failures record `error_kind` via
  `_record_llm_failure`.
- `POST /chat/{id}/messages?async=1` (query flag; body unchanged): persists the
  user row, enqueues `chat_turn`, returns `202 JobAccepted`. The existing 409
  "turn already pending / approval open" guard applies **before** enqueue, and
  a second `chat_turn` for the same session while one is `pending|running` is
  409 `turn_running` (dedupe helper in the pattern of `enqueue_canon_compile`).
- The invariant "transcript is mutated only on the request path" is relaxed to
  "only by `post_message` or the `chat_turn` job it delegates to". The 409
  guard in `post_message` also treats a running `chat_turn` as a pending turn.
- Web: `chat-panel.tsx` sends async when it has a `rootId` (the revise drawer).
  On 202 it shows the existing "planning…" state, polls the job with the
  existing `waitForJob`, then hydrates history + pending approval exactly like
  the reload path. The 10-minute blind poll of history stays only for the
  non-async chat. Boot sweep: `sweep_orphaned_jobs` already fails the row; add
  a Greek assistant-less recovery on hydration ("η απάντηση διακόπηκε από
  επανεκκίνηση — στείλε ξανά").
- `run_agent_turn` itself is unchanged; the planner stays inline **inside the
  job**, which is now fine (no proxy in front of a background task).

### 4.5 Guard hazard (S8)
`guards.looks_like_named_song_request` is not changed in this unit, but
`raw_user_text` is passed through unchanged by the job (parity with
`post_message`). Documented as a follow-up.

## 5. Unit 1 — Hand edits become first-class

### 5.1 Marker on save
`PATCH /blocks/{block_id}` (`routers/curriculum.py::update_block`), when
`body` changes on a `segment`:

```python
meta = dict(block.meta or {})
te = meta.get("tutor_edited")
if not te:
    te = {"at": now_iso, "prev_body": block.body or "", "count": 0}
te = {**te, "at": now_iso, "count": te["count"] + 1}
block.meta = {**meta, "tutor_edited": te}
```

`prev_body` is the body **as the AI last wrote it** (baseline), never
overwritten by later tutor saves. Then `_recompute_lesson_word_count(parent)`
with `depth.count_words` (replacing the `.split()` counter in `revise.py`) and
`meets_floor = words >= floor_words` from the stored `floor_words`. Lesson
title/objective edits do not set the marker.

The same on a `lesson` body edit (the summary): `tutor_edited` on the lesson
block, no word-count change beyond recount.

### 5.2 Baseline reset on AI writes
Every point where model text becomes a segment body clears `tutor_edited` on
that segment: `refine.refine_block`, `segment_generate` (edit_segment),
`persist_lesson` (for regenerated sections), `restore.restore_lesson_segments`
(restored bodies are AI-era bodies; clear). `undo_refine` leaves it as is.

### 5.3 Draft around fixed sections
`draft.py::persist_lesson` and `build_lesson_schema` gain a notion of **fixed
sections**:

- `build_lesson_schema(blueprint, exclude: set[str] = ())` omits those section
  keys from the guided-JSON schema (`summary`/`title` stay).
- `build_lesson_messages(..., fixed_sections: dict[str, str] | None)` renders
  `LESSON_FIXED_BLOCK` (new slice `lesson.fixed`, registered):
  «Οι παρακάτω ενότητες είναι ΤΟΥ ΚΑΘΗΓΗΤΗ και δεν αλλάζουν — γράψε τις
  υπόλοιπες ώστε να δένουν απόλυτα με αυτές (ορολογία, παραδείγματα, σειρά
  ιδεών):\n{fixed}» where `{fixed}` is `json.dumps({section: body},
  ensure_ascii=False)`, each body capped at `FIXED_SECTION_CHAR_LIMIT = 20_000`
  with the existing truncation-marker convention. Empty when absent → byte-
  identical prompts (baseline test).
- `persist_lesson(..., keep: set[str] = ())`: segments whose `meta.section`
  is in `keep` are **not deleted and not regenerated**; regenerated sections
  are written **in place** when a segment with that `meta.section` exists
  (same row id, body/citations/est_minutes replaced) and created otherwise.
  `meta.custom` survival is unchanged. Order follows the blueprint.
- `_draft_one` computes `keep = {s.section for s in segments if
  tutor_edited}` for `deepen` and `revise_instruction` redrafts, and passes
  the bodies as `fixed_sections`. Resume/redraft of a `queued` lesson with no
  segments keeps nothing. The redraft confirm copy (`el.json` «Ό,τι έχεις
  αλλάξει με το χέρι…») is updated to say hand-edited sections are kept.
- `measure()` counts fixed sections from their stored bodies so the word
  count and `thin_sections` stay honest.

### 5.4 Row/body UI
- Lesson row: status pill only when `draft_status != "ready"`; word-count
  badge removed; new button «AI στο μάθημα» (icon `Sparkles`) in the action
  bar left of Deepen, `data-testid="lesson-ai"`.
- Expanded lesson body: a small line «{n} λέξεις · στόχος {target}» (amber when
  under floor) computed from `meta.word_count` (now live).
- Segment row: a small «επεξεργασμένο από σένα» chip when `tutor_edited`,
  `data-testid="segment-tutor-edited"`, with a «Τι άλλαξε;» that opens
  `WhatChanged` with `before = tutor_edited.prev_body`, `after = body`.

## 6. Unit 2 — «AI στο μάθημα»

### 6.1 Panel (`components/curriculum/lesson-ai-panel.tsx`)
Fixed `aside` + backdrop, copied from `ReviseDrawer` (full-screen toggle,
Escape, close). Opened from the row button via a `LessonAiScope` context
provider mounted in `curricula/[rootId]/page.tsx` beside `ReviseScopeProvider`
(same counter-based `openRequest` pattern). Contents:

1. Header: lesson title, module title.
2. «Αλλαγές σου» strip: one chip per `tutor_edited` section (label + relative
   time), each opening `WhatChanged`. Hidden when none.
3. Quick chips (fill the instruction box, not sent): «Ενημέρωσε τις υπόλοιπες
   ενότητες με βάση τις αλλαγές μου» (only when the strip is non-empty),
   «Κάνε τις ασκήσεις πιο δύσκολες», «Πρόσθεσε παραδείγματα στη θεωρία».
4. Instruction `Textarea` + «Φτιάξε πλάνο» (`data-testid="lesson-ai-plan"`).
5. State machine: idle → planning (spinner + «Σχεδιάζω…», job poll) → plan
   card → applying (spinner + phases) → done («Έγινε — δες «Τι άλλαξε;»» with
   the lesson-level what-changed/restore buttons inline) → idle. Errors via
   `jobErrorText`.

### 6.2 Plan card (`components/curriculum/lesson-plan-card.tsx`)
- Summary paragraph (`plan.summary`).
- One row per blueprint section (enabled ones + existing custom segments):
  checkbox (checked iff `action == "rewrite"`), section label, `reason`,
  and the model's one-line `brief` for that section (editable inline).
  Tutor-edited sections render with the chip and are unchecked by default
  (the model is told they are fixed); the tutor can still tick one to
  override ("rewrite this too").
- «Κάτι ακόμα για το AI;» textarea (`note`).
- Buttons: «Ξαναφτιάξε το πλάνο» → plan again with `instruction + note`,
  `data-testid="lesson-ai-replan"`; «Εφαρμογή» → apply with the ticked set,
  briefs, and `note`, `data-testid="lesson-ai-apply"`. Impact line: «Θα
  ξαναγραφούν {n} ενότητες · ~{words} λέξεις».
- Empty ticked set → apply disabled with «Δεν επέλεξες ενότητες».

### 6.3 API
- `POST /blocks/{lesson_id}/ai/plan` body `{instruction: str, note?: str}` →
  202 `JobAccepted`, kind `lesson_ai`, params `{lesson_id, mode: "plan",
  instruction, note}`. 404 unless a `lesson`. 409 `llm_not_configured`
  (dependency). 409 `lesson_busy` if a `lesson_ai` job for this lesson is
  `pending|running` or `draft_status == "drafting"`.
- `POST /blocks/{lesson_id}/ai/apply` body `{instruction, note?, sections:
  [{section: str, brief: str}]}` → 202, params `{lesson_id, mode: "apply", …}`.
  422 if any `section` is not an existing segment's `meta.section` (custom
  keys allowed) or the list is empty.
- `GET /jobs/{id}` unchanged; plan lands in `progress.plan`.

### 6.4 Job `lesson_ai` (`app/jobs/lesson_ai.py`)
Registered by hand-import in `routers/curriculum.py` (module-level, for the
monkeypatch convention). Progress phases: `preparing → planning|generating →
done`. `result_root_id` = the course root.

**Plan mode** (`curriculum/lesson_ai.py::plan_lesson_change`):
- Context: course title + `meta.brief`; module title/objective; **prev/next
  lesson titles+objectives** (same helper as Unit 3's neighbour block); the
  blueprint's enabled sections (key, label, weight); every segment as
  `{section, title, body}` with body cap 20,000 chars; for each tutor-edited
  segment additionally `{section, before: prev_body}` (cap 20,000) — the model
  is told explicitly «ο καθηγητής άλλαξε αυτές τις ενότητες με το χέρι· το
  "before" είναι όπως ήταν, το τρέχον κείμενο είναι η νέα αλήθεια»; retrieval
  `ground_topic(f"{lesson_title} {instruction}", source_ids, k=6)`; the
  instruction and note **last**; `language_directive` + `curriculum_style` +
  `answer_in`.
- Schema `LESSON_PLAN_SCHEMA`: `{summary: str, sections: [{section: str,
  action: "rewrite"|"keep", reason: str, brief: str}], note_to_tutor: str}`,
  `additionalProperties: false`. Sections must cover every existing section
  exactly once; the server fills gaps with `keep` and drops unknown keys,
  recording `plan["dropped"]` like `validate_ops`.
- Provider `guided_json(role="plan")`. Prompt slices `lesson.ai.plan` (system)
  and `lesson.ai.plan.user` registered; `curriculum_style` injected with the
  override-safety fallback-append from `revise.py:392-400`.
- Server-computed `plan["impact"] = {rewrite_count, est_words}`.

**Apply mode** (`curriculum/lesson_ai.py::apply_lesson_change`):
1. Snapshot: `meta.prev_segments` + `meta.revise_instruction = instruction
   (+ note)` via `restore.snapshot_of` (the existing lesson-level undo).
2. Set `draft_status = "drafting"` (claim with `FOR UPDATE`; sweep-compatible).
3. One `draft_lesson(...)` call in **retrieval grounding** (`build_retrieval_context`;
   no 90K prefix — the lesson exists, the tutor's text is the authority) with
   `build_lesson_schema(blueprint, exclude=not_ticked)`, `fixed_sections =
   every not-ticked section's current body`, the per-section briefs rendered
   as `LESSON_SECTION_BRIEFS_BLOCK` («Για κάθε ενότητα που ξαναγράφεις:
   {section}: {brief}»), `revise_current = None` (superseded by fixed +
   briefs), objective suffixed with «Αναθεώρηση από τον καθηγητή: {instruction}
   {note}». Word target from `_lesson_size`; `deepen` pass allowed.
4. `persist_lesson(..., keep=not_ticked)` — in-place writes, ids kept,
   `tutor_edited` cleared on rewritten sections only, citations merged,
   word count recomputed, `draft_status = "ready"`.
5. On `LLMError`: `_release` to `ready` (the lesson was fine before), record
   `error_kind`; `rate_limit` → job `failed` with kind (no requeue loop here —
   the tutor is watching and can press again).

### 6.5 Sweep
`sweep_interrupted_lessons` already resets `drafting → queued`; for a lesson
that has segments (i.e. an interrupted apply), reset to `ready` instead so an
interrupted panel apply never leaves a finished lesson looking unwritten. The
`prev_segments` snapshot remains for manual restore.

## 7. Unit 3 — Guided «Προσθήκη μαθήματος»

### 7.1 Dialog (`components/curriculum/add-lesson-dialog.tsx`)
Base UI `Dialog` (the primitive with the zoom evidence). Fields: `Textarea`
«Τι θέλεις να διδάσκει αυτό το μάθημα;» (rows 8, placeholder explains he can
paste an outline), optional title `Input`, position `select` «Στο τέλος» /
«Μετά από: {sibling}». Buttons: «Δημιουργία με AI» (disabled until the brief
has ≥ 10 chars), «Κενό μάθημα» (today's behaviour). Testids `add-lesson-brief`,
`add-lesson-title`, `add-lesson-after`, `add-lesson-generate`,
`add-lesson-empty`. While the job runs the dialog shows «Σχεδιάζω τον
τίτλο…» → «Γράφεται…» then closes and the board refreshes; the draft progress
bar takes over.

### 7.2 API and job
- `POST /blocks/{module_id}/lessons/generate` body `{brief: str, title?: str,
  after?: uuid}` → 202, kind `lesson_generate`, params `{module_id, brief,
  title, after}`. 404 unless a module; 422 if `after` is not a sibling; 409
  `llm_not_configured`.
- Runner `app/jobs/lesson_generate.py`:
  1. `progress {"phase": "planning"}`; `curriculum/extend.py::plan_lesson_json`
     — one `guided_json(role="plan")` over `prefix_messages(build_curriculum_context(source_ids))`
     (same cached prefix as add-module) with `LESSON_PLAN_TAIL` (slice
     `curriculum.extend.lesson`): course title + brief, module title/objective,
     **all sibling lessons** (title — objective, in order), the other modules'
     titles (course map), the tutor's brief («ΤΟ ΜΑΘΗΜΑ ΠΟΥ ΖΗΤΗΣΕ Ο
     ΚΑΘΗΓΗΤΗΣ: {brief} — κάλυψέ τα ΟΛΑ και συμπλήρωσε ό,τι λείπει»), the
     course rhythm (minutes/target words), tier honesty, `language_directive`
     + `curriculum_style` + `answer_in`. Schema `{title, objective,
     est_minutes}`. A tutor-supplied title wins over the model's.
  2. `edit._add_lesson(module_id, title, objective, after)`; then
     `meta = {**meta, "brief": brief, "added_by_tutor": True}`; commit;
     `progress {"phase": "drafting", "lesson_id": …}`.
  3. Chain `curriculum_draft` with `params {"root_id", "lesson_ids": [id]}`
     in the same thread while the prefix is warm (the `module_generate.py:79-100`
     pattern); record `progress["draft_job_id"]`.
- `jobs/curriculum_draft.py`: `params.get("lesson_ids")` intersects
  `_queued_lesson_ids` when present (`None` = today).

### 7.3 Draft prompt additions (every draft from now on)
In `LESSON_TAIL`, after the `POSITION` line:
- `LESSON_NEIGHBOURS_BLOCK` (slice `lesson.draft.neighbours`): «ΠΡΟΗΓΟΥΜΕΝΟ
  ΜΑΘΗΜΑ: {prev_title} — {prev_objective}\nΕΠΟΜΕΝΟ ΜΑΘΗΜΑ: {next_title} —
  {next_objective}\nΣΤΗΝ ΙΔΙΑ ΕΝΟΤΗΤΑ: {sibling titles}» — built in
  `curriculum_draft.py` Phase A from the plan's positions (first/last lessons
  get «—»). Present on every draft; the byte-identity baseline is regenerated
  on that commit alone.
- `LESSON_TUTOR_BRIEF_BLOCK` (slice `lesson.draft.brief`): «Ο ΚΑΘΗΓΗΤΗΣ ΖΗΤΗΣΕ
  ΡΗΤΑ αυτό το μάθημα να καλύπτει:\n{brief}\nΚάλυψε κάθε σημείο του με το
  βάθος που του αξίζει και συμπλήρωσε ό,τι λείπει.» — only when
  `lesson.meta.brief` exists (empty otherwise → byte-identical).
Both are appended **after** the cache breakpoint (volatile tail).

## 8. Unit 4 — Ship

1. Rebuild + redeploy the compose stack (`docker compose build api web && up -d`).
2. Live verification on «edited test» with `claude_cli`: (a) the tutor's exact
   sentence in the revise drawer now yields a plan card; (b) the lesson panel
   propagate on the Strat/Tele/LP/SG lesson: plan lists theory as fixed, at
   least warm-up/demo/exercises/Q&A as rewrite, apply completes, «Τι άλλαξε;»
   shows the diff, restore toggles; (c) guided add-lesson on «Η Κιθάρα ως Πηγή
   του Ήχου» with Chris's neck brief produces a lesson whose theory covers
   woods, mass/geometry, frets/nut, truss rod/construction/headstock.
3. Tag `desktop-v0.5.0`; Chris sends the dmg and
   `backups/2026-09-12-port/guitar-backup-webapp-2026-09-12.tar.gz`.

## 9. Prompt registry, style rule, baseline

New registered prompt ids: `lesson.ai.plan`, `lesson.ai.plan.user`,
`lesson.fixed`, `lesson.section_briefs`, `lesson.draft.neighbours`,
`lesson.draft.brief`, `curriculum.extend.lesson`. Every content-writing call
renders `curriculum_style` and passes body text through
`strip_inline_citations` at persistence. The completeness test must see the
new `guided_json` call sites; `call_sites` line pins for `curriculum.revise`
etc. are re-pinned if lines shift. Baseline: `python -m tests.prompt_baseline`
after diffing, on the commits that intentionally change prompt bytes
(neighbours block; `get_curriculum` description is not a prompt).

## 10. Data model

No migration. All state is `Block.meta` (plain JSON, whole-dict reassignment):
`tutor_edited {at, prev_body, count}` on segments/lessons, `brief` on lessons,
`prev_segments`/`revise_instruction` (existing). New `GenerationJob.kind`
values: `chat_turn`, `lesson_ai`, `lesson_generate` (free string column).

## 11. Error handling summary

| Where | Failure | Tutor sees |
|---|---|---|
| chat_turn job | LLM too_long | 413 message «Η συζήτηση μεγάλωσε…», no blind poll |
| chat_turn job | restart | hydration notice «διακόπηκε από επανεκκίνηση — στείλε ξανά» |
| lesson_ai plan | LLM error | `jobErrorText` in the panel, retry button |
| lesson_ai apply | LLM error | lesson back to `ready`, unchanged; error in panel |
| lesson_ai apply | restart mid-apply | sweep → `ready`; snapshot kept |
| lesson_generate | plan ok, draft fails | lesson exists `failed`; Resume/redraft as today |
| add-lesson | brief < 10 chars | button disabled |

## 12. Testing

- API (serialized, never concurrent): `test_tool_results.py` (ensure_ascii,
  cap, get_lesson), `test_transcript_window_chars.py`, `test_chat_turn_job.py`
  (202 → job → history+approval; 409 dedupe; too_long → 413),
  `test_tutor_edited.py` (marker, baseline, word count, clear on AI writes),
  `test_persist_keep.py` (in-place ids, fixed sections excluded from schema,
  byte-identity when absent), `test_lesson_ai.py` (plan validation/gap-fill,
  apply persists only ticked, snapshot, sweep to ready), `test_lesson_generate.py`
  (plan → add → chained draft with `lesson_ids`; neighbours + brief blocks),
  registry completeness + baseline.
- Web (Playwright, port 3100, workers 1): `lesson-ai-panel.spec.ts` (open,
  chips, plan → card → tick/untick → note → replan/apply → done → what-changed),
  `add-lesson-dialog.spec.ts` (brief gate, after-select, job phases, board
  refresh), `revise-async.spec.ts` (202 → poll → approval card; 413 message),
  `tutor-edited.spec.ts` (chip + what-changed after a body edit, live count).
- Live (Unit 4) on the webapp, then the built deb via the verify recipe.
