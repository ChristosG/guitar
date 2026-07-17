# Concept Canon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The brain of curriculum crafting — compile each book once at ingest into a concept ledger with real page citations, reconcile concept names across books, and merge into a canon that surfaces where the authors **agree** and, more importantly, where they **disagree**.

**Architecture:** Compile-at-ingest, not at curriculum time. Each book alone fits the window; the canon that results is ~50K tokens and **flat in book count**. It replaces `library.text` inside the existing cached prefix — a swap of what goes in the cached block, not a rewrite of the curriculum engine. At draft time we *dereference a citation the compile produced* rather than searching, which is why this is not the RAG `corpus.py` rejected on measured evidence.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic + Postgres; Sonnet 5 via the existing provider seam; `rapidfuzz` for reconcile blocking; the existing PyStemmer/BM25 for concept search.

**Spec:** `docs/superpowers/specs/2026-07-17-library-scaling-design.md` § Part B. Read it — it carries the reasoning; this plan carries the steps.

## Global Constraints

- **Chris's words on why this exists:** *"10 books all of them talking for guitar TONE, with much information repeated, but also some unique perspectives from each writer... whats the plan there to create the ultimate curriculum, combining the knowledge of the 10 books all together?"* — **the divergences are the product.** A canon that only records consensus has failed.
- **Sonnet 5 for everything.** Chris: *"use sonnet 5 on everything its just 5 books."* Do not add a Haiku path; Haiku's 200K window cannot hold Gallagher (271K) anyway.
- **THE `[FIGURE]` CONTRACT IS TOTAL AND LOAD-BEARING** (`brain/ocr.py:38-52`): text **inside** a `[FIGURE]...[/FIGURE]` region is **ours** (a description of a picture); everything **outside** one is **the page's own words**. Quoting our description as the author's words is a fabricated citation with a real page number on it — *worse than no description at all*. The compile MUST respect this. Use the existing helper in `brain/ocr.py`; do not re-implement the split.
- **Citations are validated, never trusted.** `concept_claim.pages` must exist in that source's real page set — the `page_index` contract (`curriculum/corpus.py`). A hallucinated citation must not be able to enter the canon.
- **The canon is USER DATA** (Chris: *"every generation, library, curriculum, etc.. every user data, has to be persisting! even backup-able! and e.g. when i send him an update of the app, the data of the user must be the same!"*): Postgres, in `pg_dump`, additive migrations, and **never auto-recompiled** — an update must not silently re-spend his money re-reading books it already read.
- **Wasted or duplicated LLM spend is the top severity class.**
- **The tutor is a total beginner with computers.** Greek (`el`) is default; every user-facing string in BOTH `apps/web/src/messages/{el,en}.json` (note `src/`). Errors are a machine-readable `code` → one Greek sentence.
- **Migrations additive only.** Head: `b7e2c9a04f31`.
- **Tests:** `cd apps/api && ./.venv/bin/python -m pytest -m "not integration" -q`. Baseline **1452 passed, 51 deselected**. Run ONCE, let it finish (~90s) — concurrent runs contend on `guitar_test` and look like a hang.
- **Deploy or it isn't done.** `docker compose up -d --build web` has `depends_on: [api]` — use `--no-deps` for web. **An OCR run is LIVE in `api` right now** (Hunter, then Gallagher, ~10-20h): do NOT restart/rebuild `api` while pages are `pending`/`ocr_running`, and do NOT touch `knowledge_source`/`page`/`chunk` rows.

---

## Why this is not the RAG that already failed

`corpus.py:16-20` rejected retrieval on measured evidence: *"'Tube Screamer' appears verbatim in 7 chunks of his book; the dense arm's top hit for that exact query scores 0.844 and contains none of them."* A silent miss during an unattended 20-lesson run becomes a false *"your library doesn't cover this"* — the original bug.

The canon does the searching **once, offline, with the whole book in context**, and writes down where things are. Draft time then **dereferences a pointer**: no embedding, no similarity threshold, no top-k, no silent miss. That is the load-bearing distinction of this entire design.

---

## Current state (measured 2026-07-17)

| Book | Pages | Chars | Tokens | Compile-ready? |
|---|---:|---:|---:|---|
| Modern guitar rigs (Kahn) | 176 | 378,722 | 94,918 | **yes** |
| Getting Great Guitar Sounds | 77 | 194,727 | 48,804 | **yes** |
| Guitar Exercises (Powers) | 57 | 92,934 | 23,292 | **yes** |
| Tone Manual (Hunter) | 184 | — | — | OCR running |
| Guitar tone (Gallagher) | 388 | — | — | OCR queued |

Build and prove against the three that are ready. Hunter and Gallagher compile when their OCR lands.

---

### Task C1: Data model

**Files:** `apps/api/app/models/canon.py`, `alembic/versions/<rev>_concept_canon.py`, test.

**Produces:** `Concept`, `ConceptClaim`, `ConceptAlias`, `BookCompile`.

```
concept        id uuid PK, key varchar(120) UNIQUE, label_en, label_el, created_at, updated_at
concept_claim  id uuid PK, concept_id -> concept ON DELETE CASCADE,
               source_id -> knowledge_source ON DELETE CASCADE,
               text text, pages int[], stance varchar(40), depth varchar(20), created_at
concept_alias  id uuid PK, concept_id -> concept, alias text, source_id -> knowledge_source
book_compile   source_id -> knowledge_source PK ON DELETE CASCADE,
               status varchar(20), model varchar(40), compiled_at, token_count int,
               concept_count int, error text
```

`concept_alias` exists so a bad merge is **reversible without recompiling** — claims keep their
`source_id` and the alias records what each book called it. `ON DELETE CASCADE` everywhere: deleting a
book must take its claims with it, or the canon cites a book that is gone.

- [ ] Failing test: models round-trip; `pages` is a real int[]; deleting a source cascades its claims; `concept.key` is unique.
- [ ] Run it, see it fail. Implement. Green. Migration (`down_revision = "b7e2c9a04f31"`).
- [ ] **Do NOT apply the migration to the live DB while OCR runs** — the api image would then be behind the DB and its boot `alembic upgrade head && exec uvicorn` would crash-loop under `restart: unless-stopped`, killing the OCR. Verify on a scratch DB (fresh→head, down→up round-trip, `alembic check`) and let it land at the next api build.
- [ ] Commit.

### Task C2: Pass 1 — compile one book

**Files:** `apps/api/app/canon/compile.py`, test.

**Produces:** `compile_book(db, source_id) -> BookCompile`.

The model reads the whole book and emits concept entries **in its own words**, each with `[p.N]`
citations. No pre-fixed vocabulary — that decision is load-bearing and the spec explains why: a fixed
taxonomy is a filter, and a filter's failure mode is **dropping the unique take that justified buying the
tenth book**. Free-form naming means reconciliation can only ever merge synonyms; it has no mechanism for
silent discard.

Binding details:
- **Strip `[FIGURE]` regions before the model sees the page, OR mark them explicitly** — a claim whose
  text comes from a figure description must record that, so it can never be quoted as the author's words.
  Decide which, and say why.
- Validate every emitted `pages` entry against the source's real page set BEFORE writing. Drop-and-log an
  invalid citation; do not let it into `concept_claim`.
- One structured call per book (`guided_json`), Sonnet 5, via the existing provider seam.
- `BookCompile` records the model and token count — so "was this compiled by the good model?" is a
  question the data can answer.

- [ ] Failing test: a fake provider returns two concepts, one citing a page that does not exist → only the valid claim is stored, and the invalid one is logged.
- [ ] Failing test: a claim sourced from inside a `[FIGURE]` region is marked as ours, not as the book's words.
- [ ] Run, fail, implement, green, commit.
- [ ] **LIVE PROOF:** compile Powers (57pp, smallest, already OCR'd). Report the concepts it found, spot-check 3 claims against the real pages, and report the measured cost from the provider's usage.

### Task C3: Pass 2 — reconcile names across books

**Files:** `apps/api/app/canon/reconcile.py`, test.

**Produces:** `reconcile(db) -> list[Concept]`.

~10 lists totalling maybe 1,000 concept *names*. **This pass reads names, not books** — it is tiny.
`rapidfuzz` blocks obvious candidates deterministically before the model sees anything (cheap, and it
shrinks the model's job); the model only adjudicates the near-misses.

- [ ] Failing test: "pickup height" / "pickup adjustment" / "adjusting pickup height" collapse to ONE concept with three aliases, and each alias keeps its `source_id`.
- [ ] Failing test: two genuinely different concepts do NOT merge.
- [ ] Failing test: reconcile is stable — same inputs, same canonical keys.
- [ ] Run, fail, implement, green, commit.

### Task C4: The canon block — and the divergences

**Files:** `apps/api/app/canon/render.py`, test.

**Produces:** `build_canon_context(db, source_ids) -> CanonContext` — same shape as `LibraryContext` so it drops into `corpus.prefix_messages` unchanged.

```
CONCEPT: pickup_height_adjustment
  consensus:  what 7 of 9 agree on            → S2 p.47, S5 p.112, S7 p.88
  divergence: Hunter X (S5 p.113) vs Gallagher Y (S3 p.201)     ← THE PRODUCT
  coverage:   9/10 books
  depth:      S5 pp.110-118 goes deepest
```

- [ ] Failing test: two books making **opposing** claims about one concept render a `divergence` section naming both, with both citations. **This is the test that proves the feature exists at all** — a canon that silently averages them has failed Chris's actual request.
- [ ] Failing test: `CanonContext` exposes `page_index` and `ref_to_source_id` compatible with `draft.py`'s citation validation.
- [ ] Failing test: canon size grows with CONCEPTS, not pages — adding a second book covering the same concepts must not double it.
- [ ] Run, fail, implement, green, commit.

### Task C5: Route curriculum through the canon above the threshold

**Files:** `apps/api/app/curriculum/corpus.py`, `apps/api/app/config.py`, test.

```
selected sources → count_tokens
   ≤ canon_threshold (300K) → full-context verbatim   (today's path, unchanged)
   > canon_threshold        → canon + citation-directed hydration
```

- [ ] Failing test: 299K → the library block ships; 301K → the canon block ships. **Assert at the PROMPT layer, not on a flag** — the `fits` bug was precisely a flag nobody read.
- [ ] Failing test: a selected book whose compile has not finished → full-context if it fits; if it does not, say so plainly and refuse. **Never silently.**
- [ ] Failing test: the cache invariant holds — canon prefix byte-identical across two calls, second call reports non-zero `cache_read_input_tokens`.
- [ ] Run, fail, implement, green, commit.

### Task C6: Compile at ingest, resumable and explicit

**Files:** `apps/api/app/jobs/canon_compile.py`, `apps/api/app/routers/library.py`.

A background job, sibling to OCR, behind the same `SELECT…FOR UPDATE` in-flight guard. Runs when a book's
OCR completes. **Never auto-recompiles** an already-compiled book — an app update must not silently
re-spend his money.

- [ ] Failing test: pressing compile twice yields ONE job; an already-compiled book is not re-billed.
- [ ] Run, fail, implement, green, commit.

### Task C7: The tutor can SEE the canon

Chris: *"that would also be nice to see somewhere in the library, i mean the canon generations."*
And: *"i dont see any canon component, or text anywhere."*

**Files:** `apps/web/src/components/library/source-row.tsx`, a canon view, `src/messages/{el,en}.json`.

- Per-source: compile status, concept count, when. Mirror the existing OCR-status pattern.
- A canon view: browse concepts; each shows coverage, the consensus, and **the divergences** — every
  citation a chip that deep-links into the Reader at the real page, exactly as lesson citations already do.
- **Divergence is the headline, not a footnote.** "Hunter and Gallagher disagree here" is what ten books buy.

- [ ] Failing spec, implement, green, `tsc` clean, **`docker compose up -d --build --no-deps web`**, commit.

### Task C8: Concepts are searchable

Chris: *"those concepts might be nice to be searchable bro, by the search and from the chat screen too!"*

- **Library search:** concepts as a searchable kind (BM25 over labels + claim text; reuse PyStemmer — and per the retrieval memory, BM25 is load-bearing here, not optional).
- **Chat:** a `search_concepts` read tool in `agent/tools.TOOLS` (today 21 schemas → 22). It answers *"what do my books say about pickup height?"* with cross-book synthesis and real citations — which `search_knowledge` structurally cannot, because it returns chunks from one book and has no notion that two books disagree.
- Accept deliberately: adding a tool changes the tool list, which is part of the cache prefix. It re-mints the chat prefix ONCE.
- **Register the new tool in `app/prompts/registry.py`** — its completeness test will otherwise fail, which is the test doing its job.

- [ ] Failing tests, implement, green, commit.

---

## Self-Review

**Spec coverage:** compile ✅ C2 · reconcile ✅ C3 · merge/divergence ✅ C4 · threshold ✅ C5 · at-ingest ✅ C6 · durability ✅ C1 · Library UI ✅ C7 · search ✅ C8 · `[FIGURE]` contract ✅ C2 · citation validation ✅ C2.

**Deferred:** citation-directed hydration (Pass 5) — C5 lands the routing; hydration is its own task once the canon is real. The 300K threshold experiment (150K/300K/590K scored on citation accuracy) — still reasoned, not measured; C5 makes it one setting.

**Placeholder scan:** none.

**Type consistency:** `CanonContext` mirrors `LibraryContext` (`text`, `token_count`, `fits`, `page_index`, `ref_to_source_id`, `sources`) so C5's swap needs no downstream change. `compile_book`/`reconcile`/`build_canon_context` consistent across C2–C6.
