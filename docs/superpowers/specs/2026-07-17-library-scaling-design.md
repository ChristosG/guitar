# Library Scaling — ingest correctness, then the Concept Canon

**Date:** 2026-07-17
**Status:** approved for planning
**Scope:** what happens when the library goes from 1 book to 10 — ingesting them
correctly, and generating a curriculum that combines them.

---

## The one-sentence version

Four books take the corpus from 14% to 98.8% of its own budget, the fallback that
was supposed to catch that doesn't work, and the way to combine ten books is not
to fit them in the window but to compile them once into a concept-keyed canon
that keeps the page citations and makes the authors' disagreements teachable.

---

## Measured facts

Everything below was measured this session, not estimated.

### The library today (live DB)

```
7 sources · 83 pages · 327,569 chars · 378 chunks · 656 blocks
Getting Great Guitar Sounds ......... 194,671 chars ·  77 pages · pdf (a true scan)
5 URL sources + 1 seeded text ....... 132,898 chars ·   6 pages
```

### The four new books (measured with pdfinfo / pymupdf)

| Book | Pages | Text-layer chars | chars/page | OCR needed |
|---|---:|---:|---:|---|
| Guitar Tone (Gallagher) | 388 | 1,079,600 | 2,782 | **no** — full text layer |
| Tone Manual (Hunter) | 184 | 623,767 | 3,390 | **no** — full text layer |
| Modern Guitar Rigs (Kahn) | 176 | 301,966 | 1,716 | **no** — full text layer |
| Guitar Exercises (Powers) | 57 | 31,873 | 559 | **partial — see below** |

Sample pages confirm the text layers are publisher-quality prose with correct
typographic quotes, not embedded OCR mush.

### The wall

`corpus.py` measures its own ratio: *"~359,000 characters ~= 90K tokens"* →
**3.99 chars/token** for this English corpus.

```
library today          327,569 chars     82,120 tok   13.7% of budget
  + Gallagher        1,079,600 chars    270,652 tok
  + Hunter             623,767 chars    156,376 tok
  + Kahn               301,966 chars     75,702 tok
  + Powers              31,873 chars      7,990 tok
                    ─────────────
AFTER the 4 books    2,364,775 chars    592,841 tok   98.8% of budget
                                                      59.3% of the 1M window
headroom: ~7,159 tokens ≈ 11 more book pages
```

`full_context_budget = 600_000` (`config.py:110`). And at the 10-book question:
**10 × 450pp ≈ 3.05M tokens = 5.1× the budget, 3.0× the entire window.**

### Claude vision pricing (from platform.claude.com, verified)

Visual tokens = `⌈w/28⌉ × ⌈h/28⌉`. High-res tier (Opus 4.8/4.7, Sonnet 5): long
edge ≤2576px, cap 4,784 tokens. Standard tier (Haiku 4.5): 1568px / 1,568 tokens.

A 500-page scanned book at 150 DPI (1275×1650 → 2,714 visual tokens/page):

| Model | $/page | 500 pages | + Batch API |
|---|---:|---:|---:|
| Haiku 4.5 ($1/$5) | 0.0047 | **$2.35** | $1.18 |
| Sonnet 5 ($3/$15) | 0.0175 | **$8.77** | $4.39 |
| Opus 4.8 ($5/$25) | 0.0292 | **$14.62** | $7.31 |

**Conclusion: cost must never drive an ingest decision.** The scarce resource is
context, not money.

---

## Part A — Ingest correctness (P0)

These land before the canon. Compiling a canon from content the pipeline never
read would bake the error in permanently.

### A1. The text-layer check is a truthiness test, not a coverage test

`paginate.py:83-89`:

```python
layer = (doc[i].get_text() or "").strip()
page = Page(..., text=layer or None, status="ready" if layer else "pending")
```

Any text at all ⇒ `ready` ⇒ **no model ever sees the page**. Measured on Powers:

```
40/57 pages have BOTH a text layer AND >25% raster coverage
```

A 57-page book of guitar *exercises* enters the library as 32K chars of captions
("This exercise will test your string skipping abilities") with every tab unread.
A lesson then cites p.11 for an exercise no model has seen — precisely the
failure `corpus.py:78-84` names: *"a citation he will trust."*

The tabs are **raster** (75% coverage, 0 vector paths), so image-area coverage
detects them.

**Design.** Default stays free extraction — that is the goto path and it is
right. Add a coverage test:

```python
def page_text_verdict(page, median_chars: int) -> tuple[str, str]:
    text = (page.get_text() or "").strip()
    if not text:
        return "pending", "no_text_layer"
    coverage = raster_area(page) / page_area(page)
    if coverage > 0.25 and len(text) < 0.5 * median_chars:
        return "pending", "text_layer_misses_visual_content"
    return "ready", "text_layer"
```

`median_chars` is the document's own median, so the threshold adapts to the book
rather than to an absolute guess.

Three failure modes, honestly triaged:

| # | Failure | Detection |
|---|---|---|
| 1 | No text layer | automatic — already works |
| 2 | Text layer misses visuals | **automatic — new, measured at 40/57 on Powers** |
| 3 | Text layer is garbage (bad Acrobat OCR) | **manual — the retry button** |

Mode 3 is not reliably detectable. The retry button is the correct answer to it:
a heuristic has a recall ceiling, and a human override converts an undetectable
failure into one click. Chasing a perfect detector is where this dies.

Cost to fix Powers: **40 pages × $0.0175 = $0.70.**

### A2. `Page.text_source` — provenance

New column: `text_layer` | `qwen` | `claude` | `failed`. The Reader shows it per
page. This is what makes the detector auditable: he can *see* which pages fell
back and judge whether the heuristic was right. Without it, A1 is a silent
behaviour change, which is the genre of bug this whole spec is about.

### A3. `OCR_PROVIDER`, decoupled from `LLM_PROVIDER`

Today OCR calls `get_provider()` zero-arg — the same object as chat. There is a
`"ocr"` *role* (`claude.py:98`) but it only tunes max_tokens/effort on the one
selected model. `claude_cli.py:109` is explicit: `"ocr": "low", # unused — OCR
never reaches this provider`, because `ClaudeCLIProvider.vision()` delegates to
Qwen by design (`claude -p` has no image input).

The requirement — chat on `claude_cli` (subscription, free) **and** OCR fallback
on Sonnet 5 (real key, pennies) — is not expressible today. Add an `OCR_PROVIDER`
setting resolving independently, defaulting to `LLM_PROVIDER` so nothing changes
for anyone who doesn't set it.

The encrypted key already exists in `app_setting.anthropic_key_ct`.

### A4. Re-render vision pages at 150 DPI

`RENDER_DPI = 110` is a **Qwen vLLM ceiling**, documented at `paginate.py:11-17`:
150dpi is rejected outright ("image item with length 2080 exceeds pre-allocated
encoder cache size 2048"). It is not a quality choice.

Sonnet 5 is high-res tier: 110dpi = 1,496 visual tokens; 150dpi = 2,714. Pointing
Claude at the stored 110dpi JPEGs feeds a high-res model a deliberately degraded
image and silently caps OCR quality.

Re-render **only pages routed to vision** at `OCR_RENDER_DPI` (150), keyed to the
OCR provider's tier. On Powers that is ~40 pages, not 888. The `image_path` render
for the Reader stays at 110 — it is a UI thumbnail and does not need more.

### A5. Make `fits` actually gate

`corpus.py:46-52` promises:

> Above `settings.full_context_budget` (600K [...]) `fits` goes False and the
> caller degrades to per-module retrieval — WITH AN HONEST BANNER, never
> silently.

**It does not.** `fits` is computed (`corpus.py:208`) and consulted in exactly one
place that matters — `draft.py:268`, which *adds* retrieved passages **on top of**
the still-complete library. `outline.py:148` and `extend.py:160` call
`prefix_messages(library)` unconditionally; the only gate inside it is `is_empty`
(`corpus.py:260-262`).

So above 600K the whole library still ships, plus retrieval — strictly worse than
either path alone — while `outline.py:361` sets `full_context: false` and
`tree-board.tsx:189` renders a banner reporting a degrade that never happened.
Latent at 82K. **At 593K it is one small source away from live.**

This is superseded by the threshold in Part B, but the honest gate lands first
and independently: it is a bug today, and Part B is a project.

### A6. Async upload

`knowledge.py:246` runs `ingest_source` synchronously in the request; 30 MiB cap
(Gallagher is 26.0 MiB — under, but not by much). Measured in the api container:

```
sampled 13 pages @110dpi: 405 ms/page
ALL 388 pages, synchronous in the HTTP request: 157.3 s  (Cloudflare cap: 100 s)
```

Deprioritised, deliberately: the target is a bundled Tauri app on an iMac over
localhost, where there is no CDN timeout and none is observed today. It stays in
scope because 157 s of synchronous work in a request handler is wrong regardless
of who times it out, and the async-job pattern already exists (`jobs/runner.py`,
`GenerationJob`) from the interview-outline work.

---

## Part B — The Concept Canon

### The problem is not that 10 books don't fit

They don't (3.05M vs 1M). But that is the shallow reading. The deep one:

> "10 books all of them talking for guitar TONE, with much information repeated,
> but also some unique perspectives from each writer."

**Even if ten books fit, full-context would be the wrong answer.** It would read
Hunter and Gallagher disagreeing about pickup height and silently average them
into consensus mush. The thing being asked for — "the ultimate curriculum" — *is*
the disagreements. Full-context is structurally incapable of surfacing them,
because nothing in the prompt asks the model to notice that two of its 900,000
tokens contradict each other.

**The redundancy is the resource.** Ten tone books are largely the same content;
keyed by concept, that collapses to near-nothing. What survives is the divergence
— which is exactly the valuable part.

### Compile at ingest, generate from the canon

**Pass 1 — compile each book, free-form.** A background job when a book is
ingested, sibling to OCR. The model reads the whole book and emits concept
entries **in its own words**, each with `[p.N]` citations:

```json
{"concept": "pickup height and its effect on attack",
 "stance": "...", "claims": [{"text": "...", "pages": [47, 48]}],
 "prerequisites": ["pickup types"], "depth": "primary"}
```

~$1.26/book on Sonnet 5 (271K in @ $3/M + ~30K out @ $15/M). **10 books ≈ $13,
one time.**

**The compile ceiling is a different budget from the curriculum threshold, and
must not be confused with it.** The 300K threshold below governs *curriculum
generation*, where prompt length competes with reasoning quality across 20
unattended lessons. The compile is a single, one-shot, read-everything pass whose
output is a few thousand tokens of structured extraction — the task the long
context window is genuinely good at. Its ceiling is the model's practical limit
(~600K, leaving room for output), not 300K.

A book exceeding even that ceiling (>~2.4M chars — none of his do; the largest is
Gallagher at 1.08M) compiles **per-chapter with a shared cached prefix**, then
merges its own ledger. That is the one place a sliding window is the right tool:
within a single book, where reading order is meaningful and there is nothing to
reconcile across authors.

**Pass 2 — reconcile the names.** Ten lists, ~1,000 concept *names* total:
"pickup height" / "pickup adjustment" / "adjusting pickup height". One cheap call
clusters them into a canonical vocabulary. **This pass reads names, not books** —
it is tiny.

**Why free-form-then-reconcile, not taxonomy-first.** The obvious design is to
build a taxonomy up front and compile against it. Rejected, for two reasons:

1. *Measured:* the books' TOCs are too coarse. Gallagher's is "Chapter 2: Wood";
   none of the four PDFs carry embedded bookmarks; Hunter's printed TOC extracts
   out of order. Chapter granularity cannot seed a ~250-concept vocabulary.
2. *Structural, and the real reason:* a pre-fixed vocabulary is a filter, and a
   filter's failure mode is **dropping the unique take that justified buying the
   tenth book**. Free-form naming means reconciliation can only ever merge
   synonyms — it has no mechanism for silent discard.

**Pass 3 — merge ledgers onto canonical concepts** → the canon:

```
CONCEPT: pickup_height_adjustment
  consensus:  what 7 of 9 agree on            → S2 p.47, S5 p.112, S7 p.88
  divergence: Hunter X (S5 p.113) vs Gallagher Y (S3 p.201)     ← the product
  coverage:   9/10 books
  depth:      S5 pp.110-118 goes deepest
```

~250 concepts × ~800 chars ≈ **50K tokens, and flat in book count.** Book 11 adds
a few concepts and more citations to existing ones. Ten books or fifty, the canon
does not grow meaningfully.

**Pass 4 — generate from the canon.** The canon *replaces* `library.text` inside
the existing cached block. `page_index`, citation validation, `cache_control`, the
volatile tail: all unchanged. **This is a swap of what goes in the cached prefix,
not a rewrite of the curriculum engine.**

**Pass 5 — citation-directed hydration.** Drafting a lesson on concept C: the
canon says `S5 pp.110-118`. Pull *those nine pages verbatim* into the volatile
tail so the lesson is written from Hunter's actual prose, not a summary of it.

### Why this is not the RAG that already failed

`corpus.py:16-20` rejected retrieval on measured evidence:

> "Tube Screamer" appears verbatim in 7 chunks of his book; the dense arm's top
> hit for that exact query scores 0.844 and contains none of them.

Pass 5 is **not semantic retrieval**. It dereferences a pointer that a
full-context read already produced. No embedding, no similarity threshold, no
top-k, no silent miss. The searching happened once, offline, by a model with the
whole book in context. Draft time is a lookup.

That is the load-bearing distinction of this entire design: **compile-time search
with everything in context, then draft-time dereference** — rather than
draft-time search with nobody watching, which is the bug that started all of
this.

### The threshold

```
selected sources → count_tokens
   ≤ 300K  → full-context verbatim   (today's path, unchanged)
   > 300K  → canon + citation-directed hydration
```

| Selection | Tokens | Path |
|---|---:|---|
| Library today | 82K | full-context |
| Gallagher alone | 271K | full-context |
| Hunter + Gallagher | 427K | canon |
| All 12 sources | 593K | canon |
| 10 books | 3.05M | canon |

300K rather than the existing 600K: `full_context_budget` was derived from *what
fits in the window*, which is the wrong question. Attention degrades across a
600K prompt long before the API rejects it. 300K is a quality-derived ceiling.

This **replaces A5's gate** with one that actually gates. Above the line the canon
is not a compromise — two tone books is exactly where divergence-surfacing starts
paying. The threshold exists so a small library doesn't trigger a compile it
doesn't need.

**Edge:** a selected book whose compile hasn't finished → full-context if it fits;
if it doesn't, say so plainly and refuse. Never silently.

### Add-module (`extend.py`)

Answering "if I add a module myself, does it search the books again?" — no. A
canon lookup, and the tiering already exists (`outline.py:46-49`: `TIER_LIBRARY`,
`TIER_GENERAL`, `TIER_WEB`, `TIER_GAP`):

- **In the canon** → `tier=library`, cite real pages, hydrate them verbatim.
- **Not in the canon** → `tier=general_knowledge`, honestly labelled.
- **Both** is the normal case: canon-grounded core, general knowledge to bridge,
  each labelled. The tier system was built for exactly this.

### Data model

```
concept
  id, key (canonical slug), label_en, created_at

concept_claim
  id, concept_id -> concept, source_id -> knowledge_source,
  text, pages int[], stance, depth, created_at

book_compile
  source_id -> knowledge_source (PK), status, model, compiled_at, error
```

The canon is *derived state*: rebuildable from `concept_claim` at any time, and
`concept_claim.pages` validates against the existing `page_index` contract, so a
hallucinated citation cannot enter the canon in the first place.

### Testing

- **Citation validity**: every `concept_claim.pages` entry exists in that
  source's real page set. Reuses the `page_index` contract.
- **Compile coverage**: every page of a compiled book is either cited by ≥1 claim
  or explicitly classified non-teachable (front matter, index, ads). This is the
  completeness guarantee RAG can never give, and it is *measurable*.
- **Spot-check**: sample K claims; assert the cited page actually contains the
  claim. Guards Pass 1 hallucination.
- **Reconcile stability**: same inputs → same canonical keys.
- **Threshold**: 299K → full-context; 301K → canon. Both asserted at the prompt
  layer, not just the flag (the A5 bug was precisely a flag nobody read).
- **Cache invariant**: canon prefix byte-identical across calls; second call
  reports non-zero `cache_read_input_tokens`.
- **Incremental**: compiling book 11 does not mutate books 1-10's claims.

---

## Sequencing

```
NOW ─── Part A · ingest correctness
  A1 coverage detector      A2 provenance     A3 OCR_PROVIDER
  A4 150dpi re-render       A5 honest gate    A6 async upload
      ↓  12 sources · 888 pages · 593K tok
      ↓  budget still 600K, so full-context still runs — with 11 pages of headroom
LATER ─ Part B · the canon (its own plan)
      ↓  threshold drops 600K → 300K; >300K selections route to the canon
```

Note the ordering consequence: after Part A the books are ingested correctly and
full-context still serves them, because the 600K budget stands. Part B is what
lowers the line to 300K — so Part A must not be read as "safe forever." It buys
correct ingestion and an honest gate, not headroom.

Part A first because the canon compiles from ingested content: build it on a
pipeline that loses 70% of a tab book and the error is baked in permanently.
Part A is also independently valuable — it is a set of real bugs at today's size.

## Risks

| Risk | Mitigation |
|---|---|
| Pass 1 hallucinates claims/citations | `page_index` validation on write; spot-check test; per-page coverage assertion |
| Reconcile merges two genuinely different concepts | Merges reviewable; claims keep their `source_id`, so a bad merge is reversible without recompiling |
| Canon loses the authors' prose voice | Pass 5 hydrates real pages verbatim — the canon is the index and the synthesis, never the writing source |
| Compile cost surprises | ~$1.26/book, shown before it runs; `book_compile` row makes it re-runnable, not repeated |
| 300K proves to be the wrong line | One setting, one call site, measurable — tune it |
