# Plan B — Quote-Based Citations (+ Gallagher recompile)

**Date:** 2026-07-18
**Spec:** `docs/superpowers/specs/2026-07-18-curriculum-authoring-control-design.md` — Unit B ("Quote-Based Citations"), plus its Global Constraints.
**Repo:** `/mnt/nvme2TB/guitar_tutor` (FastAPI `apps/api`).
**Sub-skill:** execute task-by-task via `superpowers:subagent-driven-development` (fresh implementer per task, task review, final whole-branch review). Steps use `- [ ]` checkboxes.

---

## Goal

Stop the compile model from citing a **page number** at all. Instead it emits a verbatim **anchor** quote (8–15 words, copied exactly from the author's own text) per claim, and a new **pure-CPU, no-LLM** resolver (`app/canon/resolve.py`) matches that quote against the source's page text to derive the physical `page_no` deterministically. This kills the residual Gallagher folio drift (`strip_printed_folio` fixed 4/5 books; Gallagher, 388pp/366K tok, reconstructs the numbering and stays offset ~+24) that a numeric validator structurally cannot catch — **a wrong page number is still a valid number, but a made-up quote matches nothing.** It is a NUMBERING fix, not an OCR-quality fix.

The anchor is persisted on `concept_claim` so resolution can be **re-run over stored anchors without recompiling** (re-tune the threshold, fix a resolver bug, roll to more books — all at zero subscription spend). `concept_claim.pages` is still populated exactly as today (`int[]`), so `render.py`/`search.py`/the Reader deep-link are untouched.

The plan splits cleanly into **(a) buildable code with unit tests over SYNTHETIC page/claim fixtures — no real recompile, no subscription spend** (Tasks 1–6), and **(b) one final CONTROLLER-SUPERVISED OPERATIONAL RUN** (Task 7): force-recompile Gallagher via `claude -p` (`role="compile"`, 1800s), then the rare-token acceptance test (a "Lacey Act" claim must resolve to Gallagher **physical p.52**, not ~28), gated before rolling to the other 4 books.

## Architecture

Today (`app/canon/compile.py`): the model reads the whole book as `[p.N] <author-body>` / `[p.N FIGURE] <our-description>` blocks and, per claim, emits `pages: int[]` read from the `[p.N]` marker. `_valid_pages` checks each page is in `ctx.page_index` (the pages the model was shown) — but the **folio** the author printed on the scan is *also* a valid `page_no`, so a citation off by the book's front-matter offset passes the check and opens the wrong page.

After Plan B:

```
compile model  ──►  per claim: { text, stance, depth, grounding, anchor }   (pages ignored)
                                                    │  anchor = verbatim AUTHOR quote, 8–15 words
                                                    ▼
compile_book ──► _persist ──► per claim:
     grounding == "figure"  ─►  pages = _valid_pages(model pages)   (UNCHANGED path; anchor = NULL)
     else (author/unknown)  ─►  pages = resolve.resolve_anchor(anchor, index)  or  []   (drop + log)
                                                    │
                              resolve.build_page_index(db, source_id)
                                   = { page_no -> normalized AUTHOR text }, FIGURE regions stripped,
                                     rapidfuzz partial-match incl. adjacent-page spans
                                                    ▼
                              concept_claim.pages (int[], as today)  +  concept_claim.anchor (NEW, nullable)
```

Two entry points into the same resolver:

- **In-line** at compile time (Task 4): a fresh compile resolves as it persists.
- **`reresolve_source(db, source_id)`** / **`reresolve_all(db)`** (Task 5): re-run resolution over **stored** anchors — **no LLM, no recompile** — updating `pages` in place. This is how the controller re-tunes `RESOLVE_THRESHOLD` and how quote-based rolls forward without re-spending.

`strip_printed_folio` **stays** as a belt-and-suspenders pre-clean before the model sees each page (invariant 7); quote-based no longer depends on it, but removing it would be a gratuitous regression and its tests stay green.

## Tech Stack

- **Backend:** Python 3.13, SQLAlchemy 2.0, Alembic (additive migration only), `pytest`.
- **Fuzzy matching:** `rapidfuzz` (already a dependency — `apps/api/pyproject.toml:45` `rapidfuzz>=3.9`; installed 3.14.5). Same import style as `app/canon/reconcile.py` (`from rapidfuzz import fuzz`). Resolution uses `fuzz.partial_ratio` / `fuzz.partial_ratio_alignment`, **not** `WRatio` (see Global Constraints).
- **LLM:** provider-agnostic via `get_provider()` (`app/llm/factory.py`); `role="compile"` → 1800s timeout on the `claude -p` bridge (`app/llm/claude_cli.py:146` `_GUIDED_TIMEOUT_S["compile"] = 1800.0`). CPU-only; no GPU. The **resolver itself calls no model** — it is pure CPU and provider-independent.
- **Figure contract:** `app/brain/ocr.py` `book_text()` (strips `[FIGURE]...[/FIGURE]` regions) — the executable contract; the resolver **calls it, never re-derives it**.

## Global Constraints (bind every task — these are the point)

1. **Resolution is pure CPU, no LLM.** `app/canon/resolve.py` builds `{page_no -> normalized author text}` per source and fuzzy-matches each anchor. No `get_provider()`, no network, no embeddings. It can run offline over stored anchors any number of times for free.
2. **Author text only.** The resolution index is built from `book_text(page.text)` — `[FIGURE]...[/FIGURE]` regions stripped — so an anchor can **never** resolve to (and thus cite) our own figure description. A claim whose quote matches only inside a figure region resolves to **no page**. This is the fabrication guard.
3. **No-match → drop the citation, keep the claim** with `pages = []`, and **LOG** it. A made-up quote matches nothing, so this kills hallucinated pages for free. The drop rate is **observable**: `compile_book` and `reresolve_source` log a per-book summary — `N anchors, M resolved, K dropped`.
4. **Downstream unchanged.** `concept_claim.pages` is still `int[]`, populated the same way; `render.py`, `search.py`, and the Reader deep-link are **not modified**. (They already tolerate an empty `pages` list — `_page_ranges([]) == ""`, `_cite` degrades gracefully — verified in Task 4.)
5. **Additive migration, chained from the LIVE head.** One new nullable column `concept_claim.anchor`. Get the current head with `cd apps/api && .venv/bin/alembic heads` at build time and set `down_revision` to it — **do NOT hardcode**. Units C and D add migrations before B, so the head at build time will **not** be `c4d9e1f7a230` (that is the head before Unit C); it is expected to be Unit D's `chat_session_root_id` migration.
6. **Provider-agnostic, subscription `claude -p`, CPU-only.** No API key (Chris keeps the Max subscription). The compile call already goes through `get_provider()` with `role="compile"`. Nothing new requires a GPU.
7. **`strip_printed_folio` stays** as a pre-clean before the model sees each page; `tests/test_canon_folio.py` stays **green** unchanged.
8. **Figure-grounded claims keep citing (hybrid — resolved design call below).** Only `grounding == "author"` (and unknown) claims are resolved by anchor; `grounding == "figure"` claims keep today's `_valid_pages(model pages)` path and store `anchor = NULL`. This preserves Powers' 40 pages of tab citations and keeps invariant 2 literally true (figure claims are never resolved against the author index, so they can't fabricate). See "Resolved design calls" #1 — flagged for controller.
9. **No `concept_claim` semantic surprise: a claim may now have empty `pages`.** Today every stored claim has ≥1 page (`_validate_claim` dropped the rest). Invariant 3 changes that: a resolved-to-nothing author claim is **kept** with `pages = []` and logged. The concept is kept (it still has claims). `_prune_orphan_concepts` still only deletes concepts with **zero** claims.

## Resolved design calls (controller — approve or override)

1. **Figure-grounded claims — HYBRID (recommended), flagged as the key call.** The spec/invariants say "the anchor is a verbatim quote from the AUTHOR's text" and "resolution matches author text only." Taken literally and uniformly, that strips **every** figure-grounded claim of its citation (its content is our `[FIGURE]` description, which the author-only index never matches → empty pages). That silently deletes Powers' tab/diagram citations — the exact "silent, confident, wrong" failure this codebase is architected against. **Recommended:** resolve only author/unknown claims by anchor; keep `grounding == "figure"` claims on today's `_valid_pages(model pages)` path (figure blocks carry the injected `[p.N FIGURE]` marker; the folio bug is an author-prose problem, and figure descriptions are our own text so the model reads the marker reliably). This preserves the figure feature, keeps invariant 2 true (figure claims aren't resolved), and is a small, isolated branch in Task 4. **Override option:** pure uniform resolution (figure claims resolve to `[]`) — simpler, but accepts the figure-citation regression. *This plan implements the hybrid; resolve.py itself is uniform and unaware of grounding, so flipping to pure-uniform is a one-line change in `_finalize_claim`.*
2. **`RESOLVE_THRESHOLD = 88.0`, a named constant, calibrate on Gallagher (Task 7).** Measured: a normalized exact/OCR-noise anchor scores 100; a genuinely-unrelated quote scores <50; a boundary-straddling quote scores ~74–89 against *either* single page but 100 against the adjacent-page concatenation (which is why spans are matched). 88 sits above the single-page partial of a straddling quote (forcing the span to win and attribute correctly) and well above hallucinations. The controller re-tunes it for free via `reresolve_source` before rolling out (invariant 1/5).
3. **`concept_claim.anchor` column — YES, add it (nullable).** Justification: the re-resolve entry point (invariant 1, Task 5) needs the anchor persisted, or re-tuning the threshold / fixing a resolver bug / rolling to more books would force a ~$13 recompile. The anchor is cheap text the paid call already produced; storing it makes resolution a free, re-runnable, pure-CPU operation. **Nullable** because pre-Unit-B claims (the 4 already-compiled books) carry no anchor and NOT-NULL would violate the additive-migration/backward-compat invariant; and figure-grounded claims store `NULL` on purpose (invariant 8) so `reresolve_source` skips them (`if not claim.anchor: continue`) and never blanks their model-declared page.

## Migration chain

Run `cd apps/api && .venv/bin/alembic heads` **at build time** and set the new migration's `down_revision` to the id it prints. Expected: Unit D's `<rev>_chat_session_root_id`. **Do NOT hardcode `c4d9e1f7a230`** — that is the head before Units C and D. The one new migration (Task 1) is additive: `add_column("concept_claim", anchor TEXT NULL)`.

---

## Task 1 — `concept_claim.anchor` column (model + additive migration)

**Files**
- `apps/api/app/models/canon.py` (add `anchor` mapped column to `ConceptClaim`)
- `apps/api/alembic/versions/<newrev>_concept_claim_anchor.py` (new, additive)
- `apps/api/tests/test_canon_models.py` (extend — assert the column round-trips + is nullable)

**Interfaces**
- Produces: `ConceptClaim.anchor: Mapped[str | None]` (nullable `Text`).

**Steps**

- [ ] 1.1 **Failing test.** Append to `apps/api/tests/test_canon_models.py`:

```python
def test_concept_claim_anchor_is_nullable_text(db):
    from app.models.canon import Concept, ConceptClaim
    from app.models.knowledge import KnowledgeSource

    src = KnowledgeSource(type="pdf", title="Anchor Book", status="ready")
    db.add(src); db.flush()
    concept = Concept(key="tonewoods", label_en="Tonewoods")
    db.add(concept); db.flush()

    # anchor present
    c1 = ConceptClaim(concept_id=concept.id, source_id=src.id,
                      text="mahogany is warm", pages=[12], grounding="author",
                      anchor="mahogany bodies read warm and thick through the mids")
    # anchor absent (pre-Unit-B / figure claim) — must be allowed to be NULL
    c2 = ConceptClaim(concept_id=concept.id, source_id=src.id,
                      text="see the diagram", pages=[31], grounding="figure",
                      anchor=None)
    db.add_all([c1, c2]); db.commit()
    db.refresh(c1); db.refresh(c2)
    assert c1.anchor.startswith("mahogany bodies read warm")
    assert c2.anchor is None
```

- [ ] 1.2 **Run — RED** (column does not exist yet):

```
cd apps/api && .venv/bin/python -m pytest tests/test_canon_models.py::test_concept_claim_anchor_is_nullable_text -q
```
Expected: fails with `TypeError: 'anchor' is an invalid keyword argument for ConceptClaim` (or a DB `UndefinedColumn` after the model is added but before the migration).

- [ ] 1.3 **Add the column** to `ConceptClaim` in `apps/api/app/models/canon.py`, immediately after the `grounding` column (keep the module's commentary style):

```python
    # THE ANCHOR QUOTE — a verbatim 8–15 word span copied from the AUTHOR's own
    # text, from which `canon/resolve.py` derives `pages` deterministically (no
    # LLM). Persisted, not transient, and that is the point: with the anchor on
    # disk, re-resolving (re-tuning the threshold, fixing a resolver bug, rolling
    # quote-based to another book) is a free, pure-CPU re-run over stored rows —
    # never a ~$13 recompile. NULL for the four books compiled before Unit B, and
    # NULL by design on a `grounding="figure"` claim (whose page is the model's
    # declared `[p.N FIGURE]` marker, not a resolved author quote) — which is why
    # `reresolve_source` skips a claim with no anchor and never blanks its page.
    anchor: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] 1.4 **Get the live head, then write the migration.**

```
cd apps/api && .venv/bin/alembic heads
```
Copy the printed id (e.g. `<unitD_head>`). Create `apps/api/alembic/versions/<newrev>_concept_claim_anchor.py` (pick a fresh 12-hex `<newrev>`), hand-written (this repo does not autogenerate — see the header of `c4d9e1f7a230_concept_canon.py`):

```python
"""concept_claim.anchor — the verbatim quote quote-based citations resolve from (Unit B)

Revision ID: <newrev>
Revises: <unitD_head>          # the id printed by `alembic heads` at build time — NOT hardcoded here by guess
Create Date: 2026-07-18

ADDITIVE — one nullable column on an existing table, nothing else touched, read,
altered or locked. Same deployment constraint as `c4d9e1f7a230`: the api boot CMD
is `alembic upgrade head && exec uvicorn` under `restart: unless-stopped`, so a DB
ahead of the image crash-loops the container. This column is inert against every
row that exists today (they get NULL), and it carries no data migration: the
anchor is produced only by (re)compiling a book with the Unit B prompt, which
costs model time, and an `upgrade` that populated it would be an app update
silently spending the tutor's subscription — the top severity class in this plan.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "<newrev>"
down_revision: Union[str, Sequence[str], None] = "<unitD_head>"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("concept_claim", sa.Column("anchor", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("concept_claim", "anchor")
```

- [ ] 1.5 **Apply on the test DB and run — GREEN.** (The test DB is created/upgraded by the session fixture; if the fixture builds schema from models it passes already, but verify the migration itself is valid against a scratch DB the same way `c4d9e1f7a230` was — never against the live DB.)

```
cd apps/api && .venv/bin/alembic upgrade head            # scratch/test DB only
cd apps/api && .venv/bin/python -m pytest tests/test_canon_models.py -q
```
Expected: the anchor test passes; the rest of `test_canon_models.py` still passes.

- [ ] 1.6 **Commit.** `git add -A && git commit` — message: `feat(canon): add nullable concept_claim.anchor for quote-based citations`.

---

## Task 2 — Anchor in the compile schema + prompt

**Files**
- `apps/api/app/canon/compile.py` (`CONCEPT_SCHEMA` add `anchor`; `COMPILE_TASK` add the anchor instruction; `_validate_claim` no longer required here — see Task 4, but the schema/prompt land here)
- `apps/api/tests/test_canon_compile.py` (extend)
- `apps/api/tests/prompt_baseline.py` regeneration (the `canon.task` render changes)

**Interfaces**
- Produces: each claim in `CONCEPT_SCHEMA` carries an `anchor` string property; `COMPILE_TASK` instructs the model to emit a verbatim author quote.

**Steps**

- [ ] 2.1 **Failing test.** Add to `apps/api/tests/test_canon_compile.py`:

```python
def test_schema_and_prompt_ask_for_an_anchor_quote():
    from app.canon.compile import CONCEPT_SCHEMA, COMPILE_TASK
    claim_props = (CONCEPT_SCHEMA["properties"]["concepts"]["items"]
                   ["properties"]["claims"]["items"]["properties"])
    assert "anchor" in claim_props
    assert claim_props["anchor"]["type"] == "string"
    # the prompt must ask for a VERBATIM quote from the AUTHOR's own words
    low = COMPILE_TASK.lower()
    assert "anchor" in low
    assert "verbatim" in low or "word-for-word" in low or "exactly" in low
    assert "8" in COMPILE_TASK and "15" in COMPILE_TASK  # the 8–15 word window
```

- [ ] 2.2 **Run — RED:**
```
cd apps/api && .venv/bin/python -m pytest tests/test_canon_compile.py::test_schema_and_prompt_ask_for_an_anchor_quote -q
```
Expected: `KeyError: 'anchor'`.

- [ ] 2.3 **Add `anchor` to `CONCEPT_SCHEMA`** (inside the claim `properties`, alongside `pages`; keep `pages` — the model may still emit it, it is now ignored):

```python
                                "anchor": {
                                    "type": "string",
                                    "description": (
                                        "A VERBATIM quote, 8–15 words, copied "
                                        "WORD-FOR-WORD from THIS AUTHOR's own text "
                                        "on the page where he makes this claim — "
                                        "NEVER from inside a [FIGURE] block (that "
                                        "is our description, not his words). This "
                                        "quote, not any page number, is what pins "
                                        "the claim to a page."
                                    ),
                                },
```
Add `"anchor"` to the claim `required` list: `"required": ["text", "anchor", "pages", "stance", "depth", "grounding"]`.

- [ ] 2.4 **Add the anchor instruction to `COMPILE_TASK`.** Insert a new numbered point **before** the existing point 3 (the anti-folio block) — the folio block stays as belt-and-suspenders (invariant 7). Reword point 3's opening so the page number is explicitly no longer what the model reports:

```python
    "3. GIVE A VERBATIM ANCHOR QUOTE, NOT A PAGE NUMBER. For each claim, copy an "
    "`anchor`: 8 to 15 words taken WORD-FOR-WORD from THIS AUTHOR's own text on "
    "the page where he makes the claim — his exact wording, not your paraphrase, "
    "not tidied up. We find the page ourselves by locating that quote in the book, "
    "so the anchor must be a real, distinctive run of his words (avoid a generic "
    "phrase that appears on many pages). NEVER quote from inside a [FIGURE] block: "
    "that text is OUR description of a picture, not his sentence, and quoting it "
    "would fabricate a citation. If a claim has no author sentence you can quote "
    "word-for-word (it rests entirely on a diagram or tab), leave `anchor` empty "
    "and set `grounding` to \"figure\".\n"
    "\n"
```
Then keep the existing anti-folio paragraph but retitle it so the model still ignores in-body numbers (it may fill `pages`, which we discard):

```python
    "4. THE `pages` FIELD IS A HINT WE VERIFY, NOT THE CITATION. You may still "
    "record the [p.N] marker you read the claim under, but WE resolve the real "
    "page from your `anchor` quote — so a wrong number here costs nothing. Read "
    "any page number ONLY from the [p.N] / [p.N FIGURE] marker at the START of a "
    "block, never from a number printed in the page body (a folio like \"62\" at "
    "the foot of the page, a chapter, figure or year). These books have front "
    "matter, so the injected marker and the printed folio routinely disagree.\n"
    "\n"
```
Renumber the remaining points (the old points 4/5/6 — the `[FIGURE]` grounding rule, `depth`, `name_el` — shift down accordingly). **Keep every word of the `[FIGURE]` grounding rule** (it drives `grounding`, which invariant 8 depends on).

> **Do not delete `strip_printed_folio` or its call in `build_book_context`.** Invariant 7: it stays as a pre-clean, and `test_canon_folio.py` stays green.

- [ ] 2.5 **Run the new test + the full compile suite — GREEN:**
```
cd apps/api && .venv/bin/python -m pytest tests/test_canon_compile.py -q
```

- [ ] 2.6 **Regenerate the prompt baseline for `canon.task` only.** Editing `COMPILE_TASK` changes the `canon.task` render, so `tests/test_prompts_byte_identity.py` will go RED. Follow that file's own rule (header lines 22–25): read the diff, confirm the ONLY changed prompt is `canon.task` (and that the change is exactly the anchor edit), then regenerate:
```
cd apps/api && .venv/bin/python -m pytest tests/test_prompts_byte_identity.py -q     # RED — inspect the diff it prints
cd apps/api && .venv/bin/python -m tests.prompt_baseline                             # regenerate on THIS commit only
cd apps/api && .venv/bin/python -m pytest tests/test_prompts_byte_identity.py tests/test_prompts_editable.py -q   # GREEN
```
Confirm no registry entry was added/removed (no new slice — the anchor lives inside the existing `canon.task` slice), so `test_prompts_editable.py` completeness stays satisfied.

- [ ] 2.7 **Commit.** `feat(canon): compile emits a verbatim anchor quote per claim (pages become a hint)`.

---

## Task 3 — `app/canon/resolve.py`: the pure-CPU resolver

**Files**
- `apps/api/app/canon/resolve.py` (new)
- `apps/api/tests/test_canon_resolve.py` (new)

**Interfaces**
- Consumes: `db`, `source_id: UUID`; the `page` table (`page_no`, `text`, `source_id`, `status`); `app.brain.ocr.book_text`.
- Produces:
  - `RESOLVE_THRESHOLD: float` (named constant, `88.0`)
  - `build_page_index(db, source_id: UUID) -> list[_PageText]`
  - `resolve_anchor(anchor: str, index: list[_PageText]) -> int | None`

**Steps**

- [ ] 3.1 **Failing tests** — write `apps/api/tests/test_canon_resolve.py` covering exact match, whitespace noise, case noise, page-boundary span, no-match→drop, figure-region exclusion, and the threshold boundary. These use SYNTHETIC in-memory page objects (a tiny stand-in with `page_no`/`text`/`status`), so nothing touches a provider or the live DB:

```python
"""Quote-based resolution — pure CPU, no LLM, no recompile. Every property here is
a fabrication guard or a folio-drift kill, pinned on synthetic pages so it costs
nothing to prove.
"""
from app.brain.ocr import FIGURE_MARKER, FIGURE_END
from app.canon.resolve import (
    RESOLVE_THRESHOLD, _PageText, _norm, build_page_index, resolve_anchor,
)


def _idx(pages: dict[int, str]) -> list[_PageText]:
    """A resolution index straight from {page_no: raw page text}, running the real
    figure-stripping + normalization the DB path uses."""
    from app.brain.ocr import book_text
    return [_PageText(page_no=n, text=_norm(book_text(t)))
            for n, t in sorted(pages.items()) if _norm(book_text(t))]


_P86 = ("Wiring Options. Though the coils of wire and magnets are the primary "
        "components of a pickup, the way they are wired together shapes the tone "
        "you hear more than any single part.")


def test_exact_match_resolves_the_physical_page():
    idx = _idx({85: "Some earlier prose about capacitors and tone caps.", 86: _P86})
    # the [p.86] marker is physical 86 even though the scan printed "62" on it
    page = resolve_anchor("the way they are wired together shapes the tone you hear", idx)
    assert page == 86


def test_whitespace_and_punctuation_noise_still_resolves():
    idx = _idx({86: _P86})
    assert resolve_anchor("the way they are   wired together, shapes the tone!!", idx) == 86


def test_case_noise_still_resolves():
    idx = _idx({86: _P86})
    assert resolve_anchor("THE WAY THEY ARE WIRED TOGETHER SHAPES THE TONE", idx) == 86


def test_anchor_spanning_a_page_boundary_resolves_to_its_start_page():
    # the quote begins on p.101 and finishes on p.102
    idx = _idx({
        101: "The neck relief you set at the first fret determines how the string",
        102: "clears the frets along its whole length, and a truss rod adjusts it.",
    })
    page = resolve_anchor(
        "determines how the string clears the frets along its whole length", idx)
    assert page == 101          # attributed to the page the quote STARTS on


def test_a_hallucinated_quote_matches_nothing_and_is_dropped():
    idx = _idx({86: _P86})
    assert resolve_anchor(
        "left-hand vibrato widens gradually as the phrase resolves upward", idx) is None


def test_a_quote_only_inside_a_figure_region_never_resolves():
    # the ONLY place these words appear is our own [FIGURE] description
    raw = (f"Fig 4. {FIGURE_MARKER}\nA wiring diagram: the tone capacitor bridges "
           f"the volume pot's third lug to ground.\n{FIGURE_END}\nSee above.")
    idx = _idx({40: raw})
    page = resolve_anchor(
        "the tone capacitor bridges the volume pot's third lug to ground", idx)
    assert page is None         # author-only index — a figure quote resolves to no page


def test_below_threshold_does_not_resolve():
    idx = _idx({86: _P86})
    # a partial, garbled overlap that scores under RESOLVE_THRESHOLD
    weak = resolve_anchor("wired shapes hear part single more than tone components", idx)
    assert weak is None
    assert RESOLVE_THRESHOLD == 88.0
```

- [ ] 3.2 **Run — RED:**
```
cd apps/api && .venv/bin/python -m pytest tests/test_canon_resolve.py -q
```
Expected: `ModuleNotFoundError: No module named 'app.canon.resolve'`.

- [ ] 3.3 **Implement `apps/api/app/canon/resolve.py`:**

```python
"""Quote-based citation resolution — deterministic, pure CPU, NO LLM, NO recompile.

WHY THIS EXISTS. The compile model, told to cite the injected [p.N] marker, cited
the FOLIO printed on the scan instead — the number the AUTHOR put on his own page,
which disagrees with the physical page by the book's front matter. `strip_printed_
folio` (see `compile.py`) fixed four of five books by deleting that number before
the model saw it; Gallagher (388pp/366K tok) reconstructs the numbering across the
book and stays offset ~+24. A numeric validator cannot catch it — `cited + 24` is
still a real `page_no` that passes the existence check. So the model stops
reporting a NUMBER: it copies a verbatim ANCHOR quote from the author's words, and
THIS module finds the page by locating that quote. A wrong number is a valid
number; a made-up quote matches nothing.

PURE CPU, PROVIDER-INDEPENDENT. Nothing here calls a model. The index is the page
text the compile already stored; the match is `rapidfuzz`, the same C-fast library
`reconcile.py` blocks names with. So this runs offline, over stored anchors, for
free, any number of times — which is what makes re-tuning the threshold and
rolling quote-based to another book cost zero subscription spend (`reresolve_*`).

AUTHOR TEXT ONLY — THE FABRICATION GUARD. The index is built from `book_text()`,
the executable `[FIGURE]...[/FIGURE]` contract from `brain/ocr.py`, which strips
our own picture descriptions. An anchor that exists ONLY inside a figure region
therefore matches nothing and resolves to no page — it can never cite our caption
as the author's sentence with a real page number on it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from rapidfuzz import fuzz
from sqlalchemy import select

from app.brain.ocr import book_text
from app.models.canon import ConceptClaim
from app.models.knowledge import Page

log = logging.getLogger(__name__)

# The `rapidfuzz.fuzz.partial_ratio` score (0–100) an anchor must clear to become a
# citation. `partial_ratio` — NOT `WRatio` — because the anchor is SHORT and a page
# is LONG: partial_ratio aligns the anchor against the best-matching SUBSTRING of
# the page, which is exactly "is this quote on this page?". (`reconcile.py` uses
# `WRatio` for the opposite shape — two short names of comparable length.)
#
# 88 is the calibrated floor: a normalized exact/OCR-noise anchor scores ~100; a
# genuinely unrelated quote scores <50 (dropped — invariant 3, which also kills
# hallucinated pages); a quote straddling a page boundary scores ~74–89 against
# EITHER single page but 100 against the adjacent-page concatenation, so 88 forces
# the span match to win and the single-page near-miss to lose. NAMED so Task 7
# re-tunes it on Gallagher for free via `reresolve_source`.
RESOLVE_THRESHOLD = 88.0

_WS = re.compile(r"\s+")
# `re.UNICODE` so Greek/accented author text folds to itself rather than to nothing
# — the same reasoning as `compile._SLUG`. (The anchors are English author prose,
# but the normalizer must not silently empty a non-Latin page.)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def _norm(text: str) -> str:
    """lowercase, strip punctuation, collapse whitespace. The anchor the model
    copied and the page text it copied from must be compared modulo typography and
    OCR spacing noise; nothing that changes MEANING is touched."""
    return _WS.sub(" ", _PUNCT.sub(" ", (text or "").lower())).strip()


@dataclass(frozen=True)
class _PageText:
    page_no: int
    text: str          # normalized AUTHOR text — figure regions already stripped


def build_page_index(db, source_id: UUID) -> list[_PageText]:
    """`[_PageText(page_no, normalized author text)]` for one source, in page
    order, FIGURE REGIONS STRIPPED (invariant 2). Ready pages only — a page that
    is not `ready` has no trustworthy text to match against.

    `book_text()` is the `[FIGURE]` contract, executable; call it, never re-derive
    it (its docstring: the compile is the caller it was written for). A page whose
    author half normalizes to empty (a bare tab page, all figure) contributes no
    entry — a real anchor cannot have come from it.
    """
    pages = db.scalars(
        select(Page)
        .where(Page.source_id == source_id, Page.status == "ready")
        .order_by(Page.page_no)
    ).all()
    out: list[_PageText] = []
    for page in pages:
        author = _norm(book_text(page.text or ""))
        if author:
            out.append(_PageText(page_no=page.page_no, text=author))
    return out


def resolve_anchor(anchor: str, index: list[_PageText]) -> int | None:
    """The physical `page_no` an anchor quote lives on, or None if nothing clears
    `RESOLVE_THRESHOLD`.

    Matches the normalized anchor against each page on its own AND across every
    physically-adjacent page boundary (a claim's quote can straddle two pages),
    attributing a cross-boundary match to the page the quote STARTS on via
    `partial_ratio_alignment.dest_start`. None (drop the citation) when the best
    score is below threshold — which is every hallucinated quote and every quote
    that lives only inside a figure region (invariants 2 & 3).
    """
    query = _norm(anchor)
    if not query or not index:
        return None

    best_score = 0.0
    best_page: int | None = None

    # 1) each page on its own.
    for pt in index:
        score = fuzz.partial_ratio(query, pt.text)
        if score > best_score:
            best_score, best_page = score, pt.page_no

    # 2) each PHYSICALLY-adjacent boundary — the quote may straddle two pages. We
    #    never bridge across a gap in the ready pages (`b == a + 1` only): a real
    #    quote does not span a page that was dropped, and bridging a hole would
    #    invent an adjacency the book does not have.
    for a, b in zip(index, index[1:]):
        if b.page_no != a.page_no + 1:
            continue
        bridge = a.text + " " + b.text
        al = fuzz.partial_ratio_alignment(query, bridge)
        if al is None or al.score <= best_score:
            continue
        best_score = al.score
        boundary = len(a.text) + 1        # index of `b`'s first char in the join
        best_page = a.page_no if al.dest_start < boundary else b.page_no

    return best_page if best_score >= RESOLVE_THRESHOLD else None
```

- [ ] 3.4 **Run — GREEN:**
```
cd apps/api && .venv/bin/python -m pytest tests/test_canon_resolve.py -q
```
Expected: 7 passed.

- [ ] 3.5 **Commit.** `feat(canon): pure-CPU quote resolver (author-text index, page-boundary spans)`.

---

## Task 4 — Wire resolution into the compile (populate `pages` from anchors)

**Files**
- `apps/api/app/canon/compile.py` (`_persist`, `compile_book`, and a new `_finalize_claim`; `_validate_claim` retired/renamed)
- `apps/api/tests/test_canon_compile.py` (extend)

**Interfaces**
- Consumes: `build_page_index`, `resolve_anchor` from `app.canon.resolve`.
- Produces: a fresh compile stores `pages` resolved from the anchor (author path) or the model's declared pages (figure path), stores `anchor`, keeps no-match claims with `pages=[]`, and logs a per-book `N/M/K` summary.

**Steps**

- [ ] 4.1 **Failing tests.** Add to `apps/api/tests/test_canon_compile.py` (reuse the file's `_FakeProvider`, `_book`, `_use` helpers). The provider returns a claim whose `anchor` matches page text, plus a hallucinated-anchor claim, plus a figure claim:

```python
def test_compile_resolves_pages_from_the_anchor_not_the_model_number(db, monkeypatch):
    # physical p.86; the model wrongly reports pages=[62] (the folio) — resolution
    # must land on 86 from the anchor, ignoring the number.
    body = ("Wiring Options. The way the coils are wired together shapes the tone "
            "you hear more than any single component in the guitar.")
    src = _book(db, {86: body})
    payload = {"concepts": [{
        "name": "wiring", "name_el": "καλωδίωση", "claims": [{
            "text": "Wiring shapes tone more than any single part.",
            "anchor": "the way the coils are wired together shapes the tone you hear",
            "pages": [62], "stance": "wiring over parts",
            "depth": "primary", "grounding": "author"}]}]}
    _use(monkeypatch, _FakeProvider(payload))
    compile_book(db, src.id)
    claim = db.query(ConceptClaim).filter_by(source_id=src.id).one()
    assert claim.pages == [86]
    assert claim.anchor.startswith("the way the coils are wired")


def test_unresolvable_anchor_keeps_the_claim_with_empty_pages(db, monkeypatch):
    src = _book(db, {12: "Alternate picking keeps the wrist doing the work."})
    payload = {"concepts": [{
        "name": "myth", "name_el": "μύθος", "claims": [{
            "text": "A claim whose quote is nowhere in the book.",
            "anchor": "sweep arpeggios ascend cleanly when the palm floats free",
            "pages": [999], "stance": "made up",
            "depth": "mention", "grounding": "author"}]}]}
    _use(monkeypatch, _FakeProvider(payload))
    compile_book(db, src.id)
    claim = db.query(ConceptClaim).filter_by(source_id=src.id).one()
    assert claim.pages == []            # citation dropped, claim kept (invariant 3)


def test_figure_grounded_claim_keeps_its_declared_page_and_stores_no_anchor(db, monkeypatch):
    from app.brain.ocr import FIGURE_MARKER, FIGURE_END
    raw = (f"Setup chart.\n{FIGURE_MARKER}\nA table of string gauges vs tension.\n"
           f"{FIGURE_END}")
    src = _book(db, {31: raw})
    payload = {"concepts": [{
        "name": "gauges", "name_el": "πάχη", "claims": [{
            "text": "The gauge/tension table on this page.",
            "anchor": "", "pages": [31], "stance": "see table",
            "depth": "secondary", "grounding": "figure"}]}]}
    _use(monkeypatch, _FakeProvider(payload))
    compile_book(db, src.id)
    claim = db.query(ConceptClaim).filter_by(source_id=src.id).one()
    assert claim.pages == [31]          # UNCHANGED figure path
    assert claim.grounding == "figure"
    assert claim.anchor is None         # so reresolve_source skips it
```

- [ ] 4.2 **Run — RED** (author claim currently keeps `[62]` and drops the hallucinated claim entirely).

- [ ] 4.3 **Replace `_validate_claim` with `_finalize_claim`** (which resolves), and thread a resolution index + a tally through `_persist`. In `apps/api/app/canon/compile.py`:

Add the import at the top:
```python
from app.canon.resolve import build_page_index, resolve_anchor
```

Add a small tally and the new finalizer (keep `_valid_pages` and `_grounding` — the figure path and grounding still use them):
```python
@dataclass
class _ResolveTally:
    """Per-book resolution counts, so the drop rate is OBSERVABLE (invariant 3).
    Only AUTHOR-path claims are counted — a figure claim keeps its declared page
    and never enters resolution."""
    total: int = 0
    resolved: int = 0
    dropped: int = 0


def _finalize_claim(raw: dict, ctx: BookContext, index, tally: _ResolveTally, *,
                    where: str) -> dict | None:
    """One claim, checked into shape and given a resolved page — or None if it is
    not a claim at all (no text). A no-match citation is DROPPED (empty pages) but
    the claim is KEPT (invariant 3)."""
    if not isinstance(raw, dict):
        return None
    text = (raw.get("text") or "").strip()
    if not text:
        return None

    declared = raw.get("grounding")
    stance = (raw.get("stance") or "").strip() or None
    if stance and len(stance) > _STANCE_MAX:
        log.info("canon: truncating an over-long stance for %s: %.60r", where, stance)
        stance = stance[:_STANCE_MAX]
    depth = raw.get("depth")

    if declared == "figure":
        # UNCHANGED path (invariant 8): a figure claim's page is the injected
        # [p.N FIGURE] marker the model reported, validated as today. No anchor is
        # stored, so `reresolve_source` skips it and never blanks this page.
        pages = _valid_pages(raw.get("pages"), ctx, where=where)
        anchor = None
    else:
        # AUTHOR path: the page comes from the verbatim quote, not the number.
        anchor = (raw.get("anchor") or "").strip() or None
        page = resolve_anchor(anchor, index) if anchor else None
        pages = [page] if page is not None else []
        tally.total += 1
        if page is None:
            tally.dropped += 1
            log.warning("canon.resolve: no page for %s — dropping citation; "
                        "anchor=%.70r", where, anchor or "")
        else:
            tally.resolved += 1

    return {
        "text": text,
        "pages": pages,
        "stance": stance,
        "depth": depth if depth in CLAIM_DEPTHS else None,
        # `_grounding` re-derives structurally from the FINAL pages: a resolved
        # author page is an author page, a figure page is a figure page. With
        # empty pages it returns "author" (harmless — there is no page to mis-cite).
        "grounding": _grounding(declared, pages, ctx),
        "anchor": anchor,
    }
```

- [ ] 4.4 **Update `_persist`** to build the index once, thread the tally, use `_finalize_claim`, and STOP dropping a concept merely because a claim lost its citation (invariant 9 — a concept is dropped only if it has no claims at all):

```python
def _persist(db, source: KnowledgeSource, ctx: BookContext, data: dict,
             index) -> tuple[int, _ResolveTally]:
    concepts = (data or {}).get("concepts") or []
    tally = _ResolveTally()
    kept = 0
    for entry in concepts:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        where = f"{source.title!r}/{name!r}"

        claims = [c for c in (
            _finalize_claim(raw, ctx, index, tally, where=where)
            for raw in entry.get("claims") or []
        ) if c]
        if not claims:
            log.warning("canon: %s kept no claim — dropping the concept", where)
            continue

        concept = _get_or_create_concept(db, name, entry.get("name_el"))
        _upsert_alias(db, concept.id, source.id, name)
        for claim in claims:
            db.add(ConceptClaim(concept_id=concept.id, source_id=source.id, **claim))
        kept += 1

    db.flush()
    _prune_orphan_concepts(db)
    return kept, tally
```

- [ ] 4.5 **Update `compile_book`** to build the index before persisting and log the per-book summary (invariant 3). Replace the `_persist` call block:

```python
    _clear_source_ledger(db, source_id)
    index = build_page_index(db, source_id)
    concept_count, tally = _persist(db, source, ctx, data, index)

    record.status = "ready"
    record.concept_count = concept_count
    record.token_count = _measured_input_tokens(provider, ctx.token_count)
    record.compiled_at = datetime.now(timezone.utc)
    record.error = None
    db.commit()
    log.info("canon: compiled %r — %s concepts from %s pages, %s tokens (%s); "
             "resolution: %d anchors, %d resolved, %d dropped",
             source.title, concept_count, len(ctx.page_index), record.token_count,
             model, tally.total, tally.resolved, tally.dropped)
    return record
```

- [ ] 4.6 **Run the new tests + the full canon suite — GREEN, and confirm invariant 4 (downstream tolerates empty pages).** The existing `test_canon_render.py` / `test_canon_search.py` must still pass unchanged (they already handle empty `pages`):
```
cd apps/api && .venv/bin/python -m pytest tests/test_canon_compile.py tests/test_canon_render.py tests/test_canon_search.py tests/test_canon_reconcile.py -q
```
If a render/search test asserts a claim with empty pages appears sanely, add one small assertion rather than modifying `render.py`/`search.py` (invariant 4 — downstream code is NOT touched).

- [ ] 4.7 **Commit.** `feat(canon): resolve claim pages from anchors at compile time; keep+log no-match drops`.

---

## Task 5 — Re-resolve entry point (no LLM, no recompile)

**Files**
- `apps/api/app/canon/resolve.py` (add `ResolveSummary`, `reresolve_source`, `reresolve_all`)
- `apps/api/tests/test_canon_resolve.py` (extend)

**Interfaces**
- Produces:
  - `reresolve_source(db, source_id: UUID) -> ResolveSummary`
  - `reresolve_all(db) -> list[ResolveSummary]`

**Steps**

- [ ] 5.1 **Failing test.** Add to `apps/api/tests/test_canon_resolve.py` (this one uses the real DB via the `db` fixture, storing claims with anchors and re-resolving them — still no provider, no recompile):

```python
def test_reresolve_updates_pages_from_stored_anchors_without_any_model(db):
    from app.canon.resolve import reresolve_source
    from app.models.canon import Concept, ConceptClaim
    from app.models.knowledge import KnowledgeSource, Page

    src = KnowledgeSource(type="pdf", title="Reresolve Book", status="ready")
    db.add(src); db.flush()
    db.add(Page(source_id=src.id, page_no=52, status="ready",
                text=("The Lacey Act makes it unlawful to trade in wood harvested "
                      "in violation of another country's laws.")))
    concept = Concept(key="legal-wood", label_en="Legal wood")
    db.add(concept); db.flush()
    # a claim whose stored `pages` is wrong (folio offset) but whose anchor is right
    author = ConceptClaim(concept_id=concept.id, source_id=src.id,
                          text="Illegally sourced wood is unlawful to trade.",
                          pages=[28], grounding="author",
                          anchor="unlawful to trade in wood harvested in violation")
    # a figure claim with no anchor — must be LEFT ALONE
    figure = ConceptClaim(concept_id=concept.id, source_id=src.id,
                          text="See the map figure.", pages=[7],
                          grounding="figure", anchor=None)
    db.add_all([author, figure]); db.commit()

    summary = reresolve_source(db, src.id)
    db.refresh(author); db.refresh(figure)
    assert author.pages == [52]         # corrected from the anchor, no model call
    assert figure.pages == [7]          # untouched (no anchor)
    assert (summary.total, summary.resolved, summary.dropped) == (1, 1, 0)
```

- [ ] 5.2 **Run — RED** (`ImportError: cannot import name 'reresolve_source'`).

- [ ] 5.3 **Implement** in `apps/api/app/canon/resolve.py`:

```python
@dataclass
class ResolveSummary:
    source_id: UUID
    total: int = 0
    resolved: int = 0
    dropped: int = 0


def reresolve_source(db, source_id: UUID) -> ResolveSummary:
    """Re-run resolution over a source's STORED anchors — NO LLM, NO recompile.

    THE WHOLE REASON `concept_claim.anchor` IS PERSISTED. Re-tuning
    `RESOLVE_THRESHOLD`, fixing a resolver bug, or rolling quote-based to another
    book costs ZERO subscription spend: this reads the anchors already on disk and
    rewrites `pages`. A claim with no anchor (pre-Unit-B, or a figure claim) is
    LEFT ALONE — never blanked. A no-match empties `pages` and is counted
    (invariant 3). Commits, and returns the observable summary.
    """
    index = build_page_index(db, source_id)
    claims = db.scalars(
        select(ConceptClaim).where(ConceptClaim.source_id == source_id)).all()
    summary = ResolveSummary(source_id=source_id)
    for claim in claims:
        anchor = (claim.anchor or "").strip()
        if not anchor:
            continue                    # no anchor -> not a resolvable claim
        summary.total += 1
        page = resolve_anchor(anchor, index)
        if page is None:
            claim.pages = []
            summary.dropped += 1
            log.warning("canon.resolve: no page for stored anchor %.70r "
                        "(source=%s) — dropping citation", anchor, source_id)
        else:
            claim.pages = [page]
            summary.resolved += 1
    db.commit()
    log.info("canon.resolve: source=%s — %d anchors, %d resolved, %d dropped",
             source_id, summary.total, summary.resolved, summary.dropped)
    return summary


def reresolve_all(db) -> list[ResolveSummary]:
    """Re-resolve every source that has at least one stored anchor. The controller
    calls this after re-tuning `RESOLVE_THRESHOLD`; a source with no anchors (the
    four books compiled before Unit B) is a no-op."""
    source_ids = [
        r[0] for r in db.execute(
            select(ConceptClaim.source_id)
            .where(ConceptClaim.anchor.is_not(None))
            .distinct()
        ).all()
    ]
    return [reresolve_source(db, sid) for sid in source_ids]
```

- [ ] 5.4 **Run — GREEN:**
```
cd apps/api && .venv/bin/python -m pytest tests/test_canon_resolve.py -q
```

- [ ] 5.5 **Commit.** `feat(canon): reresolve_source/reresolve_all — re-run resolution over stored anchors, no recompile`.

---

## Task 6 — Folio belt-and-suspenders stays green (invariant 7)

**Files**
- `apps/api/tests/test_canon_folio.py` (no change expected — this task is a guard)
- `apps/api/app/canon/compile.py` (confirm `strip_printed_folio` is still called in `build_book_context`)

**Steps**

- [ ] 6.1 **Confirm the pre-clean is intact.** Grep that `build_book_context` still calls `strip_printed_folio(book_text(raw))` (compile.py ~line 240) and that `strip_printed_folio` is unchanged:
```
cd apps/api && grep -n "strip_printed_folio" app/canon/compile.py
```
Expected: the definition plus the call inside `build_book_context`.

- [ ] 6.2 **Run the folio tests — GREEN, unchanged:**
```
cd apps/api && .venv/bin/python -m pytest tests/test_canon_folio.py -q
```
Expected: 4 passed. If any Task 2 reword accidentally touched `strip_printed_folio`, revert that — the folio pre-clean is orthogonal to the anchor change and must remain byte-identical.

- [ ] 6.3 **Full canon regression before handing off to the operational run:**
```
cd apps/api && .venv/bin/python -m pytest tests/ -q -k "canon or prompt"
```
Expected: all green. No commit needed unless 6.1/6.2 surfaced a regression to fix.

---

## Task 7 — OPERATIONAL: recompile Gallagher + acceptance (CONTROLLER RUNS THIS)

> **GATE — controller-supervised operational run, NOT a blind subagent step.** This is the only task that spends subscription tokens (`claude -p`, `role="compile"`, 1800s, on a 366K-token book). It runs **after** Tasks 1–6 are merged and green. Do not automate it into the SDD loop. The controller runs each step, reads the summary log, and confirms the acceptance test **before** deciding whether to roll quote-based to the other 4 books.

**Preconditions**
- Tasks 1–6 merged; `alembic upgrade head` applied on the target DB (the `anchor` column exists).
- Gallagher's OCR is **complete** (`page_counts(...).pending == 0`, `ready > 0`) — a half-read book must not be compiled (poisons the money guard). Confirm in the Library UI or via `page_counts`.
- The provider is `claude_cli` (subscription) or a real Claude key is configured.

**Files**
- `apps/api/tests/test_canon_resolve_acceptance.py` (new — an `integration`-marked, skipped-by-default acceptance test the controller runs by hand)

**Steps**

- [ ] 7.1 **Force-recompile Gallagher.** Get its `source_id` (Library, or `SELECT id, title FROM knowledge_source WHERE title ILIKE '%gallagher%'` / the actual title), then run the guarded recompile so it goes through `role="compile"` (1800s) with the money-guard bypass:
```
cd apps/api && .venv/bin/python -c "
import uuid
from app.db import SessionLocal
from app.canon.compile import compile_book
db = SessionLocal()
sid = uuid.UUID('<GALLAGHER_SOURCE_ID>')
rec = compile_book(db, sid, force=True)     # role='compile' inside; 1800s bridge timeout
print('status', rec.status, 'concepts', rec.concept_count, 'tokens', rec.token_count)
db.close()
"
```
Watch the log line: `resolution: N anchors, M resolved, K dropped`. **Record the drop rate `K/N`.** A high drop rate (say >15–20%) means the anchors are not landing — inspect a few dropped anchors against the page text and consider re-tuning `RESOLVE_THRESHOLD` down (then `reresolve_source`, free) before proceeding. Reconcile runs separately (`_reconcile_new_book`) via the job path; a direct `compile_book` does not reconcile, which is fine for the acceptance check.

- [ ] 7.2 **Rare-token acceptance test.** Create `apps/api/tests/test_canon_resolve_acceptance.py` — marked so it never runs in the normal suite (no live DB, no spend); the controller runs it explicitly against the recompiled DB:

```python
"""ACCEPTANCE — the rare-token proof that quote-based citations fixed Gallagher's
folio drift. NOT part of the unit suite: it reads the LIVE compiled canon after a
real recompile. The controller runs it by hand after Task 7.1:

    RUN_CANON_ACCEPTANCE=1 GALLAGHER_SOURCE_ID=<uuid> \
      .venv/bin/python -m pytest tests/test_canon_resolve_acceptance.py -q -s
"""
import os
import uuid

import pytest

RUN = os.getenv("RUN_CANON_ACCEPTANCE") == "1"
pytestmark = pytest.mark.skipif(not RUN, reason="operational acceptance; controller runs it")


def test_lacey_act_claim_resolves_to_gallagher_physical_page_52():
    from app.db import SessionLocal
    from app.models.canon import ConceptClaim
    sid = uuid.UUID(os.environ["GALLAGHER_SOURCE_ID"])
    db = SessionLocal()
    try:
        claims = db.query(ConceptClaim).filter(
            ConceptClaim.source_id == sid,
            ConceptClaim.text.ilike("%lacey%")).all()
        assert claims, "no claim mentions the Lacey Act — recompile may have missed it"
        pages = sorted({p for c in claims for p in (c.pages or [])})
        # physical p.52, NOT the folio-offset ~28
        assert 52 in pages, f"Lacey Act resolved to {pages}, expected physical 52"
        assert 28 not in pages, f"still landing on the folio offset: {pages}"
    finally:
        db.close()
```
Run it:
```
cd apps/api && RUN_CANON_ACCEPTANCE=1 GALLAGHER_SOURCE_ID=<GALLAGHER_SOURCE_ID> \
  .venv/bin/python -m pytest tests/test_canon_resolve_acceptance.py -q -s
```
Expected: `1 passed`. If the claim's text does not contain "Lacey", widen the filter to the concept/stance, or spot-check another rare token whose true physical page the controller can verify in the Reader. **Do not weaken the physical-page assertion to make it pass.**

- [ ] 7.3 **GATE — verify before rollout.** Only if BOTH hold: (a) the drop rate from 7.1 is acceptable, and (b) 7.2 passes on physical p.52. Record both in the SDD ledger.

- [ ] 7.4 **(Gated, optional) Roll quote-based to the other 4 books.** The other 4 already resolve correctly via `strip_printed_folio`, so this is **cleanup, not urgent** (spec "Deferred / Open"). Because those books were compiled **before** the Unit B prompt, they carry **no anchors**, so `reresolve_source` is a no-op on them — rolling forward means a **force-recompile of each** (subscription spend), controller-gated one at a time, checking each book's drop rate:
```
cd apps/api && .venv/bin/python -c "
import uuid
from app.db import SessionLocal
from app.canon.compile import compile_book
db = SessionLocal()
for sid in ['<BOOK2_ID>', '<BOOK3_ID>', '<BOOK4_ID>', '<BOOK5_ID>']:
    rec = compile_book(db, uuid.UUID(sid), force=True)
    print(sid, rec.status, rec.concept_count)
db.close()
"
```
- [ ] 7.5 **Commit** the acceptance test file (skipped-by-default, so it is safe in the suite): `test(canon): rare-token Lacey Act acceptance for quote-based citations (operational, gated)`.

---

## Final whole-branch review

- [ ] Run the full backend suite: `cd apps/api && .venv/bin/python -m pytest -q`. Expected: green (the operational acceptance test is skipped without `RUN_CANON_ACCEPTANCE`).
- [ ] Confirm the invariants held: resolver calls no provider (`grep -n "get_provider\|httpx\|embed" app/canon/resolve.py` → nothing); `render.py`/`search.py` unmodified (`git diff --stat` shows no change to them); migration chained from the live head (not `c4d9e1f7a230`); `strip_printed_folio` + `test_canon_folio.py` unchanged.
- [ ] `superpowers:requesting-code-review` on the branch, then finish per `superpowers:finishing-a-development-branch`.
