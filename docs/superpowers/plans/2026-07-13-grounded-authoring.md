# Grounded Authoring — Implementation Plan (Plan 12)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Make the app author from the tutor's OWN material, with the AI guiding him — not from the model's generic knowledge.

**Architecture:** Retrieval-augmented curriculum generation (retrieve his passages per module, draft from them, cite the page, flag the gaps). A structured interview whose questions live in code and whose language is the model's. A continuously-scrolling Reader with cross-page selection. Honest refusal instead of fabricated song tabs.

**Tech Stack:** FastAPI · SQLAlchemy · Postgres/pgvector · Next.js 16 · Playwright · pytest

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-07-13-grounded-authoring-design.md`. G1–G6 binding.
- **DO NOT WEAKEN THE HITL GATE.** Every `kind="mutation"` agent tool still suspends for approval. Reviewed airtight four times.
- **The SYSTEM_PROMPT stays ONE PARAGRAPH.** A longer prompt empirically suppresses tool-calling on this model. Measured, not opinion.
- **DO NOT DAMAGE THE LIVE APP DB (`guitar`).** It is deployed and Chris is using it: his book (77 pages, 194,671 chars), collection "Tone & Gear", 1 lesson, artifacts. Tests use `guitar_test`. Read-only queries against `guitar` are fine.
- **TDD throughout.** i18n in BOTH `en.json` and `el.json`.
- Migration gotcha: strip the false `op.drop_index('ix_chunk_embedding_hnsw', ...)`; name every constraint.
- Host pytest needs `LLM_BASE_URL=http://localhost:6888/v1 EMBED_BASE_URL=http://localhost:8090/v1`.

## Verified Facts

- `app/brain/retrieve.py::search()` returns hits with `text`, `page`, `page_id`, `source_id`, score. Forced-retrieval already proven (Plan 11).
- `app/curriculum/generate.py::generate_curriculum` is a guided-JSON call, **currently ungrounded** — it never touches the library. This is the bug.
- `app/curriculum/segment.py::partition_by_minutes` is deterministic and tested.
- Provenance convention (Plan 10): stored on a Block's existing `target_profile` JSON under a `provenance` key. **No migration needed.**
- `app/agent/guards.py::looks_like_tablature` exists (Plan 11) — extend it, don't duplicate.
- Curriculum generation runs async as `GenerationJob(kind="curriculum")`; the job/poll machinery is generic (`job_kind` on the tool registry, Plan 10).
- **Chris's 8 course URLs are NOT in the repo.** Task 1 must accept them as input; do not invent URLs.

---

### Task 1: Get his real material in (prerequisite)

**Files:** `app/brain/fetch.py` (create), modify `app/brain/extract.py`, `app/routers/knowledge.py`; test `tests/test_fetch.py`

The library is one book. Everything downstream is only as good as what's in it.

- **A resilient fetch client.** Wikimedia TLS-fingerprints and blocks `httpx` (proven Plan 9 — `curl` and `urllib` succeed with identical headers). Extract the fetch behind a seam and use a client that actually works. **Keep the existing SSRF hardening** (`app/brain/urlsafe.py` — redirect-safe, internal-IP blocked). Do not regress security to fix fetching. Test that an internal URL is still rejected.
- **Bulk add:** `POST /knowledge/sources/bulk {urls: [...]}` → creates + ingests each, returns per-URL status. The UI gets a paste-many-URLs box in the Library.
- Re-ingest the 3 Wikipedia sources through the new client and confirm they now pull real characters.
- **Delete the synthetic "Guitar Tone & Gear — Course Spine"** from the live DB once real sources exist — it is seed filler masquerading as his content. **Ask the controller before deleting; do not do it unprompted.**

Tests: internal/SSRF URL still rejected; a real URL ingests with real chars; bulk endpoint reports per-URL success/failure honestly (a failed URL is `empty`/`failed`, never a green lie — spec D6).

---

### Task 2: Retrieval-grounded curriculum generation (G1, G3)

**Files:** `app/curriculum/ground.py` (create), modify `app/curriculum/generate.py`; test `tests/test_curriculum_grounding.py`

**This is Chris's central complaint: "when i click Generate a curriculum seems like it just uses the llm general knowledge... it would be beneficial here to select things from our library."**

- `ground_topic(db, topic: str, *, source_ids: list[uuid] | None, k=5) -> list[Passage]` where
  `Passage(text, source_id, source_title, page_no, page_id, score)`. Wraps `search()`, optionally scoped
  to the sources the tutor chose.
- `generate_curriculum` becomes two-phase:
  1. **Plan** the module outline (titles only) — guided JSON.
  2. For each module, `ground_topic(...)` → draft that module's content **from its retrieved passages**,
     guided JSON, with an explicit instruction to use the passages and invent nothing.
- **Provenance:** each module Block records `target_profile["provenance"] = {"passages": [{source_id, page_no}, ...]}`.
- **Gaps (G3):** a module whose retrieval returns nothing (or below a relevance floor) is marked
  `target_profile["gap"] = True` and its body says plainly that his library doesn't cover it. It is
  **never silently filled** — the caller decides whether to allow labelled general-knowledge fill via a
  `allow_general: bool` parameter (default False).

Tests (fake provider + fake search):
- a module with retrieved passages cites them in its provenance
- **the retrieved passage text actually reaches the model's prompt** (a "grounded" draft that never saw the passage is not grounded — this is the test that matters)
- a topic with NO hits is marked `gap=True` and is NOT filled with invented content when `allow_general=False`
- with `allow_general=True` the gap module IS filled but is labelled as general knowledge
- regression: the existing async job path still works

---

### Task 3: The guided interview (G2)

**Files:** `app/curriculum/interview.py` (create), `app/routers/curriculum.py`; test `tests/test_interview.py`

**Chris: "maybe llm can act as an assistant there bro, guiding him, and asking him questions or corrections throughout the process. thats actually would be dope."** And: *"Interview first might also be beneficial if we added here, so it is getting the passages needed while reading the book."*

**Design constraint (learned the hard way):** the model is unreliable at multi-turn book-keeping (Plan 10: it needed 3 attempts to track a session id). So **the interview is a state machine in CODE**; the model only supplies natural language. Do not implement this as a free-form chat and hope the model tracks state.

Steps: `who` (student/level) → `duration` (weeks / minutes-per-session) → `sources` (which of HIS sources to draw on; default all) → **`preview`: for each proposed module, show the passages found in his library, and flag the ones with none** → `confirm` → enqueue the grounded generation job.

- `POST /curricula/interview` → `{interview_id, step, question, options?}`
- `POST /curricula/interview/{id}/answer {answer}` → next step, or on the final step a `202 {job_id}`.
- State persisted (reuse `GenerationJob.params` or a small table — your call, justify it).

Tests: the state machine advances deterministically; the preview step really calls `ground_topic` and returns real passages; a module with no passages is flagged in the preview; answers are validated (a bad answer re-asks, it doesn't crash).

---

### Task 4: Reader — continuous scroll + cross-page selection (G4)

**Files:** `apps/web/.../library/[sourceId]/page.tsx`, `components/library/*`; `app/routers/lessons.py`, `app/lessons/draft.py`; tests both sides

**Chris: "i cant select more than one page.. maybe we need a scrolling way of pages and not the < > buttons?"**

- Replace `< >` paging with a **continuous vertical scroll** of the whole source. 77 scans is a lot of JPEG — **lazy-load / virtualise** the images (only render pages near the viewport) or the page will crawl. Keep a page-marker rail so he always knows where he is, and support deep-linking (`?page=21` must still scroll to page 21 — the citation chips everywhere depend on it).
- **Selection may span pages.** On selection, capture `{source_id, page_from, page_to, text}`.
- `POST /lessons/from-selection` accepts `page_from`/`page_to` (keep `page_no` accepted for back-compat — the existing citation chips and tests use it).
- `draft_lesson_from_selection` grounds on the **whole** selected passage and records the range in provenance.

Tests: a selection spanning two pages posts both bounds; deep-link `?page=N` still scrolls there; lazy-loading doesn't break the citation deep-link; the existing reader tests still pass.

---

### Task 5: Honesty guards (G5, G6)

**Files:** `app/agent/guards.py`, `app/agent/prompts.py`, `app/artifacts/generate.py`; tests

**G5 — named-song tabs.** Chris asked for the *Smells Like Teen Spirit* riff and got one note repeated seven times. The model cannot recall a specific copyrighted recording, so it invents. Detect a request for a **named song's** tablature and decline honestly, offering what it genuinely can do (the chord progression, a scale, an exercise in that style). A confident wrong tab is worse than an honest "I can't reproduce that."
Be careful with false positives: "give me a G major scale tab" and "a blues shuffle in E" are NOT named songs and must still work.

**G6 — empty artifacts.** Chris: *"it generated nothing i could only see a 'TAB' component."* An artifact that generates to nothing must **fail loudly**, not persist as an empty card the tutor clicks into. Validate the produced spec is substantive (not merely schema-valid) and raise into the existing retry/failed path. Same class as the `ready`-with-0-chars lie.

Tests: a named-song tab request is declined (and the decline names a real alternative); a generic scale/chord/exercise request still succeeds; an empty/degenerate artifact spec fails loudly and is not persisted.

---

### Task 6: Live acceptance

Drive the REAL model against the REAL book. Do not force green; report honestly.

1. **Curriculum:** run the interview. It asks who/how long/which sources, **shows the passages from his book** for each module, **flags the modules his library can't support**, and drafts one whose modules carry citation chips that open the real scan.
2. **Reader:** scroll his book continuously; select across two pages; author a lesson; the lesson is grounded in the whole passage and its provenance records the range.
3. **Honesty:** ask for the Smells Like Teen Spirit riff → an honest decline + a real alternative. Ask for a G major scale tab → still works and renders.
4. **Regression:** HITL still gates mutations; full backend + Playwright suites green.

Update `progress.md` and `README.md`. Screenshot. Then rebuild and redeploy (the app is LIVE — coordinate with the controller before swapping containers).
