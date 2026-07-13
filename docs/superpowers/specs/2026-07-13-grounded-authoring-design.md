# Grounded Authoring — Design (Plan 12)

**Status:** authored 2026-07-13 from Chris's live feedback on the deployed app.

---

## 1. What Chris found when he actually used it

He deployed it, used it, and reported — verbatim:

> "on the library when i click on 'Guitar Tone & Gear — Course Spine' i see 'Page not found'. why?
> btw was this extracted from the urls i gave you?"

> "the 'Author a lesson from this' was truly fascinating bro! and it worked, but how it works? i did
> select only a line, did it read some more context? and what happens if i want this feature but from
> 2-3 pages context of that book? i cant select more than one page.. maybe we need a scrolling way of
> pages and not the < > buttons?"

> "lets go with the 'Curricula', from where does it generates? when i click 'Generate a curriculum'
> seems like it just uses the llm general knowledge, but i think it would be beneficial here to select
> things from our library, dont you think? ... We basically need to help the tutor generates his
> lessons and curriculums but many times might need his own sources not llm generic ones. maybe llm can
> be act as an assistant there bro, guiding him, and asking him questions or corrections throughout the
> process."

> "i asked ... 'generate me smells like teen spirit riff TABS' and the first time it took too much time
> then it generated nothing ... the 2nd time it indeed generated them but they were wrong.. it felt
> like playing only one same note."

He is right on every count. Two were bugs (fixed already: the Course Spine had no `Page` rows; nginx
301'd `POST /chat`). The rest are **the product being wrong**, and they share one root cause:

> **The app generates from the model's generic knowledge when it should be generating from HIS material.**

## 2. Honest state of the library

This must be said plainly because it undercuts everything else:

| Source | Reality |
|---|---|
| Getting Great Guitar Sounds (77pp) | **Real.** His book. OCR'd, page-addressable, cited. |
| Guitar Tone & Gear — Course Spine | **Synthetic filler** written by a seed script in Plan 7. Not his. |
| 3 × Wikipedia | **Zero characters.** Wikimedia TLS-blocks our client. |
| His 8 course URLs | **Never ingested.** They were discussed in an early session and never recorded anywhere in the repo. |

So the library is *one book*. Every "grounded" feature is only as good as what's in it. **Getting his real
sources in is a prerequisite, not a nice-to-have** — the first thing this plan does.

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| G1 | **Curriculum generation retrieves from his library, per module.** The generator runs `search()` over his sources for each topic it plans, and drafts that module *from the retrieved passages*, citing source + page. | This is his central complaint. It is also now cheap: Plan 9 made the library page-addressable and Plan 11 proved forced retrieval works. |
| G2 | **Interview first, and the interview reads the book.** Before drafting, the agent asks him a few real questions (who, level, how long, which sources) — and for each topic it proposes, it *retrieves the relevant passages from his library* and shows him what it found. He steers; the book supplies the substance. | Chris: *"Interview first might also be beneficial if we added here, so it is getting the passages needed while reading the book."* Exactly. The interview is not a form — it is a grounded conversation. |
| G3 | **Coverage gaps are stated, never silently filled.** If his library has nothing on a topic, say so, and offer two explicit choices: add a source, or let the model fill it from general knowledge **clearly labelled as not-from-his-material**. | Chris chose this. It is the same principle that made the chat trustworthy in Plan 11: the danger was never general knowledge, it was *unlabelled* general knowledge. |
| G4 | **The Reader becomes a continuous scroll, and selection can span pages.** "Author a lesson from this" takes a *range* (source + page span + text), not a single-page snippet. | Chris: *"i cant select more than one page.. maybe we need a scrolling way of pages and not the < > buttons?"* And the current single-line grounding is thin — a lesson deserves a passage, not a sentence. |
| G5 | **The agent refuses to fabricate a named song's tablature.** Ask for "Smells Like Teen Spirit" and it says it can't reproduce a specific copyrighted recording, and offers what it *can* do (the chord progression, the riff's shape, a scale/exercise). | It cannot recall a specific song note-for-note, so it invents — Chris got one note repeated. A confident wrong tab is worse than an honest refusal, and it is also the right call on copyright. |
| G6 | **An artifact that generates to nothing must fail loudly**, never persist as an empty card. | Chris: *"it generated nothing i could only see a 'TAB' component."* Same class as the `ready`-with-0-chars lie (D6). |

## 4. What gets built

**Ingest his real material (prerequisite)**
- A bulk "add these URLs" path, and a fetch client that survives Wikimedia-class TLS fingerprinting
  (curl/urllib succeed where httpx is blocked — proven in Plan 9).
- Delete the synthetic Course Spine once his real sources are in. It is pretending to be his content.

**Grounded curriculum generation (G1, G2, G3)**
- `app/curriculum/ground.py` — for a planned topic, retrieve the top passages from his library and return
  them with source+page provenance.
- `generate_curriculum` becomes retrieval-augmented: plan the outline → retrieve per module → draft each
  module *from its passages* → record provenance on the module Block (same `target_profile` mechanism
  Plan 10 used for lessons) → mark any module with no supporting passages as a **gap**.
- `POST /curricula/interview` — a short, agent-driven interview (a state machine, not a free chat: the
  model is unreliable at multi-turn book-keeping, so the *questions* are structured in code and only the
  *language* is the model's). It shows what it found in his library for each topic before drafting.
- The curriculum board renders citation chips (→ the Reader at that page) and **gap badges**.

**The Reader (G4)**
- Continuous vertical scroll with page markers; pages virtualise/lazy-load (77 scans is a lot of JPEG).
- Selection may span pages. "Author a lesson from this" sends `{source_id, page_from, page_to, text}`.
- `POST /lessons/from-selection` accepts a range; the drafter grounds on the whole passage.

**Honesty guards (G5, G6)**
- `guards.py` grows a named-song detector → the agent declines and offers what it can actually do.
- Artifact generation validates the produced spec is *substantive*, not just schema-valid, and fails
  loudly if it is empty.

## 5. Out of scope

Today/Prep. Auth. Both still pending, both still need Chris.

## 6. Acceptance

> **Curriculum:** Start a curriculum. It asks who it's for and how long, shows him *which passages from
> his book* it will build each module from, flags the modules his library can't support, and drafts one
> whose modules cite real pages he can click into.
>
> **Reader:** Scroll his book continuously. Select a passage that spans two pages. Author a lesson from
> it, and the lesson is grounded in the *whole* passage.
>
> **Honesty:** Ask for the Smells Like Teen Spirit riff. It declines to fabricate it and offers a real
> alternative — instead of one note repeated seven times.
