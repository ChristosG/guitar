# Lesson Authoring — Design (Plan 10)

**Sub-project B of the 3-part redesign.** A (Library) → **B (Lesson Authoring)** → C (Copilot rebuild).
**Status:** authored 2026-07-12 during the autonomous overnight run, against the real interfaces Plan 9 produced.

---

## 1. Why this exists

Chris, in his own words:

> "what the tutor needs to do the most is to tweak his lectures and craft more, based on HIS content
> (the book, the urls i shared, and more data that he will add). the rest we've done is just a bonus.
> Also an agent that actually checks his lectures, his materials, and does stuff for them, e.g.
> change/add something, or split/segment the sessions. So his top prio (at least for starters) is to
> organize and author his lessons."

And the definition that reframes the data model:

> "when i say lesson i not mean 1-hour session but more like the whole lesson which might take
> multiple sessions."

Plan 9 put his content **in** the app — the book is OCR'd, page-addressable, searchable, and citable.
This plan is what he *does* with it: turn it into lessons he teaches.

## 2. The key realisation: no new table

A "Lesson that spans several Sessions" is **already representable**. `Block` (`app/models/block.py`) is a
recursive tree with a deliberately soft, relabelable `kind`:

```
Block(kind="lesson",  title="Barre Chords")          <- the whole lesson (multi-session)
 ├─ Block(kind="session", title="Session 1: Anatomy of the shape", est_minutes=60)
 │   ├─ Block(kind="item", title="Why the index finger fails", body="...")
 │   └─ Block(kind="item", title="Exercise: the E-shape", body="...")
 ├─ Block(kind="session", title="Session 2: The F chord")
 └─ Block(kind="session", title="Session 3: Endurance + changes")
```

`segment.py` already packs leaves into sessions by minutes (`partition_by_minutes`), and
`POST /blocks/{id}/segment` already exists. **We are surfacing machinery that is already there and
tested, not building a new engine.** That is why this plan is small.

## 3. Scope

**In scope**
- **Author a lesson from a Library selection** — make the Plan 9 stub (`POST /lessons/from-selection`)
  real: draft a `kind="lesson"` Block tree grounded in the selected passage, citing its source + page.
- **A Lesson editor** — see the lesson as an outline (lesson → sessions → items), and edit it directly:
  rename, reorder, split a session, merge sessions, add/delete, adjust minutes.
- **Agent tools that act on a lesson** — so he can say "split session 2, it's too long" or "add a
  warm-up to session 1" and have it happen, HITL-gated like every other mutation.
- **Grounding + citation** — a lesson drafted from his book carries the page it came from, and the
  citation is clickable back into the Reader.

**Out of scope**
- The copilot rebuild (forced RAG, markdown, streaming, chat surfaces) — that is **sub-project C**.
- Scheduling / calendar / Today-Prep. Still deferred, still needs Chris.
- Student assignment flows — already exist (Plan 6), unchanged.

## 4. Decisions

| # | Decision | Rationale |
|---|---|---|
| B1 | **Lesson = `Block(kind="lesson")`, Session = `Block(kind="session")`, content = `Block(kind="item")`.** No migration. | `kind` is already documented in the model as "soft, relabelable". The tree, the cascade, the ordering, and the segmentation all already work and are tested. Inventing a `Lesson` table would duplicate a working structure and orphan every existing tool that speaks `Block`. |
| B2 | **`plane="content"`** for authored lessons (the existing curriculum/delivery split is untouched). | Keeps assignment-to-student (Plan 6) working exactly as-is: a lesson is content; assigning clones it into a student's plane. Nothing to re-teach. |
| B3 | **A lesson drafted from a selection stores its provenance** on the lesson Block: `source_id` + `page_no` (in the existing `target_profile` JSON column, under a `provenance` key — no migration). | The whole point of Plan 9 was verifiable citation. A lesson that came from p.47 of his book must be able to say so, and link back to the scan. Reusing the JSON column avoids a migration for a PoC-scale need. |
| B4 | **Drafting is an async `GenerationJob(kind="lesson")`**, reusing the Plan 8 job + poll infrastructure. | Drafting a lesson tree is a guided-JSON call in the same class as curriculum generation (measured 49–179s). No new infra; the UI polls behind the same spinner it already has. |
| B5 | **The agent's lesson tools are `kind="mutation"`** and therefore HITL-gated by the existing approve-before-execute machinery. | Plan 5's reviewer traced that gate as airtight; every mutation already suspends for approval. Splitting a session is a real edit to his work — he approves it. Free, by construction. |
| B6 | **Split/merge reuse `segment.py`'s `partition_by_minutes`** rather than asking the LLM to re-cut a session. | Segmentation is DETERMINISTIC and already tested. Asking a model to do arithmetic it can get wrong, when a tested function exists, is how you get a bad split and a hallucinated duration. |

## 5. What gets built

**Backend**
- `app/lessons/draft.py` — `draft_lesson_from_selection(db, *, source_id, page_no, text, language) -> uuid` .
  Guided-JSON call producing a lesson tree (lesson → sessions → items), grounded in the selected
  passage, with provenance recorded per B3. Runs as a `GenerationJob(kind="lesson")`.
- `app/routers/lessons.py` (exists as a stub) — grows:
  - `POST /lessons/from-selection` → now **202 {job_id}** (was a 201 echo stub)
  - `GET /lessons` → list authored lessons
  - `GET /lessons/{id}` → the lesson tree (reuses `BlockTreeOut`)
  - `POST /lessons/{id}/sessions/{sid}/split` → deterministic re-segmentation of one session
  - `POST /lessons/{id}/sessions/merge` → merge adjacent sessions
  Reordering / renaming / deleting reuse the **existing** `PATCH|DELETE /blocks/{id}` — no new routes.
- `app/agent/tools.py` — four new `kind="mutation"` tools: `draft_lesson_from_selection`,
  `split_session`, `merge_sessions`, `add_session`. All HITL-gated (B5).

**Frontend**
- `/[locale]/lessons` — the lesson list (his lessons, with the source each came from).
- `/[locale]/lessons/[id]` — **the Lesson editor**: the outline (lesson → sessions → items),
  inline rename, drag-reorder, split/merge buttons, per-session minutes. A "from *Getting Great
  Guitar Sounds*, p.47" citation chip that opens the Reader at that page.
- The Library Reader's **"Author a lesson from this"** now really drafts one, shows the job spinner,
  and lands him in the editor when it's ready.

## 6. Error handling

Drafting is an LLM call, so it can fail: the job records `error_kind` (`upstream`/`timeout`/`internal`)
exactly as curriculum generation already does, and the UI shows a plain retry — never a half-built
lesson. Split/merge are deterministic and transactional: a split either produces the new sessions or
changes nothing. An agent-proposed edit shows the tutor exactly what will change **before** it happens
(the existing approval card), because it is his work being edited.

## 7. Testing

TDD throughout, `guided_json` faked in unit tests as it already is elsewhere. Deterministic split/merge
get real assertions on the resulting tree shape (this is where bugs would hide).

**Acceptance test — the one that matters:**

> Open the book in the Reader. Select a real passage about pick thickness. Hit "Author a lesson from
> this". Get back a real, multi-session lesson whose content is **grounded in that passage** and which
> **cites the page it came from** — click the citation and land on the scan of that page. Then tell the
> agent "split session 2, it's too long", approve it, and watch the session actually split.

If that doesn't work end-to-end, this plan is not done.
