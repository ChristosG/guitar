# Seed Content Implementation Plan (Plan 7)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Make the live site demo-ready — seed the Brain + curricula + artifacts + a sample student with representative tone/gear + beginner content, so the cockpit shows the product's value out of the box.

**Architecture:** An idempotent seed script (`apps/api/scripts/seed.py`) that calls the existing engines DIRECTLY in-process (sync — a script, not an HTTP request, so it bypasses the Plan 8 async-job pattern and just calls `generate_curriculum`/`generate_artifact` directly): create+ingest Brain sources, generate 2 curricula (grounded), generate showcase artifacts, create a student + assign. Re-runnable (skips already-seeded items by title).

**Tech Stack:** the existing `app.brain.ingest`/`app.curriculum.generate`/`app.artifacts.generate` services + `SessionLocal`.

## Global Constraints (verbatim)
- **Representative demo content ONLY.** The tutor's real material is BLOCKED (scanned book needs OCR; 8 course links are paywalled — see ledger MORNING BRIEFING #6). Seed instead: the design-spec §2.2 tone-course-spine (as Brain text + the flagship curriculum), 2-3 freely-ingestible tone/gear wiki pages, a beginner track, showcase artifacts. Flag in the seeded data / README that it's representative, not proprietary.
- **Idempotent:** re-running never duplicates (check by title/a `[seed]`-tagged marker before creating). A `fresh=True` flag first deletes prior seeded rows + the accumulated demo-test curricula for a clean demo.
- Runs against the app `guitar` DB (NOT `guitar_test`). Uses the REAL LLM for the live run (Task 2) — slow (2 curricula @ ~18-179s + several artifacts); the unit test (Task 1) stubs the LLM.
- Seed EN primarily (GR polish of seeded curricula deferred).

## Task 1: the idempotent seed script + fast (LLM-stubbed) test
**Files:** Create `apps/api/scripts/seed.py` (or `app/seed.py`); a test.
**Interfaces — Consumes:** `SessionLocal` (`app/db.py`); Brain `create_source`/`ingest_source` (`app/routers/knowledge.py` / `app/brain/ingest.py:97` `ingest_source(db, source_id, payload: IngestPayload)`; `SourceCreate` for kind="text"/"url"); `generate_curriculum(db, *, title, language, profile, domain=None, target_minutes_total=None) -> UUID` (`app/curriculum/generate.py:193`); `generate_artifact(db, *, kind, prompt, block_id=None, ground=False) -> Artifact` (`app/artifacts/generate.py:115`); student create + `assign_curriculum`/`clone_content_subtree` (`app/curriculum/assign.py`).
**Produces:** `seed(db, *, fresh: bool = False) -> dict` (returns a summary of created-vs-skipped):
- If `fresh`: delete prior `[seed]`-marked rows + the accumulated throwaway demo curricula (by known titles: "Blues Rhythm Basics", "API Contract Check", "Open Chords E2E Browser Check", "Stack Health Check", "Stack Health Check", "Open Chords E2E Browser Check" — and anything tagged seed).
- **Brain sources** (skip if a source with that title exists): (a) a kind="text" source titled "Guitar Tone & Gear — Course Spine" whose body is the design-spec §2.2 12-module signal-chain skeleton (What is tone / The Guitar / Amps / Speakers & Cabs / Signal flow / Gain fx / Modulation / Time fx / Other / Rigs / Iconic tones / Context — expand each to a paragraph of real content); (b) 2-3 kind="url" sources: `https://en.wikipedia.org/wiki/Humbucker`, `.../Distortion_(music)`, `.../Guitar_amplifier`. Ingest each (`ingest_source`).
- **Curricula** (skip if a course Block with that title exists): `generate_curriculum(db, title="Guitar Tone & Gear", language="en", profile={"level":"intermediate"}, domain="tone", target_minutes_total=1500)` (grounded on the ingested tone content); `generate_curriculum(db, title="Zero to Hero — Beginner Guitar", language="en", profile={"level":"beginner"}, target_minutes_total=1200)`.
- **Showcase artifacts** (skip if a titled artifact exists) via `generate_artifact`: `chord_diagram` for G / E minor / C / D major; `scale_diagram` A minor pentatonic; `tone_recipe` "Stevie Ray Vaughan Texas Flood" (ground=True); `signal_chain` a classic pedalboard; `amp_settings` a Fender clean.
- **Student + assignment** (skip if exists): create a `Student` "Maria Ioannou" (beginner, el) + assign the beginner curriculum.
- Structured + logged (print each created/skipped). Guard each step in try/except so one failure doesn't abort the whole seed (log + continue).
- [ ] Test (`guitar_test`, LLM + ingest STUBBED for speed — monkeypatch `generate_curriculum`/`generate_artifact`/`ingest_source` to fast fakes that create minimal rows): `seed(db)` creates the expected source/curriculum/artifact/student/assignment rows; a SECOND `seed(db)` call is idempotent (same counts, no duplicates). RED→GREEN. Commit.

## Task 2: run the full seed live + browser-verify the demo-ready site
- [ ] Ensure the running stack is on current code (rebuild api if stale — one at a time, health-check, leave healthy). Run `python -m scripts.seed --fresh` (or `seed(SessionLocal(), fresh=True)`) against the live `guitar` DB with the REAL LLM — be patient (several minutes). Then browser-verify (Playwright MCP): `/en/curricula` shows the "Guitar Tone & Gear" + "Zero to Hero" curricula as nested boards; `/en/artifacts` shows the showcase artifacts rendering; `/en/knowledge` shows the ingested sources as `ready`; `/en/students` shows Maria + her assignment. Screenshot the seeded cockpit (`seeded-cockpit-e2e.png`). Commit + update `docs/superpowers/plans/README.md`. Leave the stack healthy.

## Self-Review
Coverage: idempotent seed (sources/curricula/artifacts/student/assign) ✓ · representative content (§2.2 spine + wiki + beginner track + showcase artifacts) ✓ · live run + browser-verified demo-ready cockpit ✓. Deferred (flagged): the tutor's real proprietary content (blocked — needs Chris: OCR the book / provide non-paywalled sources); GR polish of the seeded curricula.
