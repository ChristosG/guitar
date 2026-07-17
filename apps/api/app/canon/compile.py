"""Pass 1 — read ONE book whole, write down what it says, concept by concept.

WHAT THIS IS FOR, IN THE OWNER'S WORDS:

    "10 books all of them talking for guitar TONE, with much information
     repeated, but also some unique perspectives from each writer. So whats the
     plan there to create the ultimate curriculum, combining the knowledge of the
     10 books all together?"

**THE DIVERGENCES ARE THE PRODUCT.** Ten tone books are ~80% the same content;
keyed by concept that redundancy collapses to nothing, and what survives is where
the authors DISAGREE — which is the one thing no single book gives him. So this
pass is not a summarizer. A summary of ten books averages Hunter and Gallagher
into consensus mush and deletes the reason the tenth book was bought. What it
extracts is each author's POSITION (`stance`) and where he takes it (`pages`), so
that C3 can put two of them side by side and find the disagreement. A compile that
recorded only what everyone agrees on would have failed the request while passing
every test in this file.

---------------------------------------------------------------------------
THIS IS NOT THE RAG THAT ALREADY FAILED, AND THE DISTINCTION IS THE DESIGN.

`curriculum/corpus.py` rejected retrieval on measured evidence: *"'Tube Screamer'
appears verbatim in 7 chunks of his book; the dense arm's top hit for that exact
query scores 0.844 and contains none of them."* A silent miss during an unattended
20-lesson run becomes a false "your library doesn't cover this" — the original bug.

This module does not undo that; it MOVES it. The searching happens ONCE, here,
offline, with the whole book in context and no lesson depending on the outcome,
and the result is written down as pointers — `source_id` + real page numbers.
Draft time then dereferences a pointer: no embedding, no similarity threshold, no
top-k, no silent miss. Which is why `concept_claim` stores CITATIONS and not
prose: a canon that stored summaries would be a lossy paraphrase of his library
and would have thrown away the only thing that makes it *his*.

---------------------------------------------------------------------------
NO PRE-FIXED VOCABULARY. The model names concepts in its OWN words.

Measured: the books' TOCs are too coarse to seed one (Gallagher's is "Chapter 2:
Wood"; none of the PDFs carry embedded bookmarks; Hunter's printed TOC extracts
out of order). Structural, and the real reason: **a fixed vocabulary is a FILTER,
and a filter's failure mode is dropping the unique take that justified buying the
tenth book.** Free-form naming means reconciliation (C3) can only ever MERGE
synonyms — it has no mechanism for silent discard. That asymmetry is the whole
argument, and it is why there is no `enum` on `name` below.

(`depth` and `grounding` ARE enums. They name no concept — they are measurements
of one, on axes with an obvious floor and ceiling. The prohibition is about
concept names, where a closed list destroys information; three books calling the
same treatment "primary" is the point.)

---------------------------------------------------------------------------
TWO THINGS ARE VALIDATED RATHER THAN TRUSTED, AND BOTH ARE FABRICATION GUARDS.

1. EVERY PAGE. `ctx.page_index` is the set of pages the model was ACTUALLY SHOWN
   — the same contract as `corpus.LibraryContext.page_index`, and deliberately not
   "the pages in the database": a page whose OCR produced 12 characters is a row
   and is not in the prompt, so the model cannot have read it, so a cite to it is a
   fabrication. A page outside the index is dropped and LOGGED, never stored. The
   tutor CLICKS these chips; a citation that opens the wrong page is worse than no
   citation, because it is one he will trust.

2. EVERY GROUNDING. See `_grounding` — the [FIGURE] contract, enforced
   structurally wherever the page itself can settle it, and believed only on a page
   that genuinely contains both.

---------------------------------------------------------------------------
NO PROMPT CACHE ON THIS CALL, DELIBERATELY.

`corpus.library_message` marks its block `cache: True` because twenty lesson
drafts read the same 90K prefix. Here there is exactly ONE call per book and
nothing ever reads the prefix again — and a cache WRITE costs 1.25x. Caching this
would raise the price of the compile by 25% to serve a read that never comes.
(A book above the model's practical ceiling would compile per-chapter against a
shared cached prefix, where the read does come. None of his books is close:
Gallagher, the largest, is ~271K tokens.)
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.brain.ocr import book_text, figure_text
from app.curriculum.corpus import MIN_PAGE_CHARS
from app.llm.factory import get_provider
from app.models.canon import (
    CLAIM_DEPTHS,
    CLAIM_GROUNDINGS,
    BookCompile,
    Concept,
    ConceptAlias,
    ConceptClaim,
)
from app.models.knowledge import KnowledgeSource, Page
from app.settings_store import resolve_llm_config

log = logging.getLogger(__name__)

# `concept_claim.stance` is varchar(40) and `llm/schema.py` STRIPS `maxLength`
# before the model ever sees the schema — structured outputs do not enforce string
# constraints. So nothing upstream can hold this line, and an over-long stance
# would arrive as a DataError that loses the whole book's compile at the INSERT,
# after the money is spent. Truncating a short label cannot invert its meaning;
# losing a 388-page read can.
_STANCE_MAX = 40


# ---------------------------------------------------------------------------
# The book, as the model sees it
# ---------------------------------------------------------------------------

@dataclass
class BookContext:
    """One book, page-annotated, plus the page sets a citation is checked against.

    THE THREE PAGE SETS ARE NOT DECORATION. `page_index` is what makes a citation
    checkable at all; `author_pages` and `figure_pages` are what make a GROUNDING
    checkable, which is the difference between citing a photo and quoting a
    sentence the author never wrote. Each holds exactly the pages whose
    corresponding half cleared `MIN_PAGE_CHARS` and therefore actually went into
    `text` — "what the model was shown", per half, never "what is in the DB".
    """

    text: str
    token_count: int
    title: str = ""
    page_index: set[int] = field(default_factory=set)
    author_pages: set[int] = field(default_factory=set)
    figure_pages: set[int] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        return not self.page_index


# The two markers the model is taught to read. `[p.N]` is `corpus.py`'s existing
# convention and is kept BYTE-IDENTICAL on purpose — it is the notation the whole
# codebase already validates citations against, and a second dialect for the same
# idea would be one more thing to get wrong.
#
# `[p.N FIGURE]` is its counterpart and is the only place our words appear in the
# prompt. It reads as a superset of `[p.N]` so a model that skims still gets the
# page number right; the label is what tells it whose sentence it is.
def _page_block(page_no: int, body: str, *, figure: bool) -> str:
    return f"[p.{page_no} FIGURE] {body}" if figure else f"[p.{page_no}] {body}"


def build_book_context(db, source_id: UUID) -> BookContext:
    """ONE book's complete text, with our figure descriptions labelled apart:

        <book title="Guitar Exercises Made Simple">
        [p.12] Alternate picking is the foundation of speed...
        [p.31 FIGURE] A tablature exercise in 4/4. Measure 1: under "Am"...
        </book>

    `Page.text` only — no chunk fallback, unlike `corpus._page_texts`. That
    fallback exists there for a source whose pages carry no text but whose chunks
    do; a book in that state has not been OCR'd, and compiling it would buy a call
    to read a book we do not have. Better to fail honestly (see `compile_book`).

    THE SPLIT IS `brain/ocr.py`'S, NOT OURS. `book_text` / `figure_text` ARE the
    contract, executable — they partition every character of the page, including
    the totality clause for Powers' 43 legacy unterminated regions. Re-deriving
    the rule here is how the two copies drift and one of them starts handing our
    photo captions to a prompt as the author's prose.
    """
    source = db.get(KnowledgeSource, source_id)
    title = (source.title or "") if source else ""

    pages = db.scalars(
        select(Page).where(Page.source_id == source_id).order_by(Page.page_no)
    ).all()

    parts: list[str] = []
    author_pages: set[int] = set()
    figure_pages: set[int] = set()

    for page in pages:
        raw = page.text or ""
        author = book_text(raw)
        figure = figure_text(raw)
        # The floor is applied per HALF, and that is what keeps the page sets
        # honest: a tab page whose only prose is a stray running head must not
        # count as "the author wrote something here", or `_grounding` would stop
        # being able to force the safe answer on it.
        if len(author) >= MIN_PAGE_CHARS:
            parts.append(_page_block(page.page_no, author, figure=False))
            author_pages.add(page.page_no)
        if len(figure) >= MIN_PAGE_CHARS:
            parts.append(_page_block(page.page_no, figure, figure=True))
            figure_pages.add(page.page_no)

    safe_title = title.replace('"', "'")   # keep the attribute well-formed
    text = (f'<book title="{safe_title}">\n' + "\n".join(parts) + "\n</book>"
            if parts else "")

    return BookContext(
        text=text,
        token_count=_count_tokens(text),
        title=title,
        page_index=author_pages | figure_pages,
        author_pages=author_pages,
        figure_pages=figure_pages,
    )


def _count_tokens(text: str) -> int:
    """Exact when the provider can tell us, estimated when it cannot. NEVER raises.

    Same contract and same reasoning as `corpus._count_tokens`, bound to THIS
    module's `get_provider` so the seam is monkeypatchable in one place per module.
    A compile must not die because `count_tokens` hit a rate limit before the real
    call was even made.
    """
    if not text:
        return 0
    try:
        return get_provider().count_tokens(text)
    except Exception:
        log.warning("count_tokens failed; falling back to the local estimate",
                    exc_info=True)
        return len(text) // 3 + 1


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

COMPILE_SYSTEM = (
    "You are reading ONE book from a working guitar teacher's library, in full, "
    "and writing down what THIS AUTHOR says — concept by concept, in your own "
    "words, with the page numbers where he says it.\n\n"
    "Output ONLY the JSON matching the schema you are given — no prose, no "
    "markdown, no commentary outside the JSON object."
)

# THE TASK, AFTER THE BOOK. Everything task-specific lives here rather than in the
# system string, for the reason `corpus.py` documents at length: with a whole book
# in the middle of the prompt, the model weighs the END hardest.
#
# Point 2 is the one that earns the money. The obvious compile writes neutral
# encyclopedia entries — and neutral entries from ten books are ten copies of the
# same paragraph, which is precisely the "consensus mush" this plan exists to
# avoid. What cannot be reconstructed later is what THIS author thinks, so that is
# what is asked for, in the author's own emphasis, including the parts other books
# would disagree with.
COMPILE_TASK = (
    "Above is the ENTIRE book. Go through it and write down every concept it "
    "teaches.\n"
    "\n"
    "1. NAME THEM IN YOUR OWN WORDS. There is no list to choose from and no "
    "vocabulary to match. Name each concept the way THIS book frames it, at "
    "whatever granularity the book actually teaches at. Do not force two "
    "different ideas together because they sound similar, and do not split one "
    "idea in two to be thorough. If this author has a concept no other guitar "
    "book would have, that one matters most — write it down as HIS.\n"
    "\n"
    "2. RECORD WHAT HE THINKS, NOT WHAT IS GENERALLY TRUE. This book will be "
    "compared against nine others on the same subject, and the POINT of the "
    "comparison is where they disagree. A neutral, encyclopedic claim is worth "
    "nothing here — every book produces the same one. So for each claim give "
    "`stance`: this author's actual position, in a few words, in his emphasis "
    "(\"prefers a lower treble-side height\", \"warns it kills sustain\", "
    "\"insists tone is in the hands\"). Where he contradicts common advice, or "
    "argues with another view, that IS the claim worth recording. Keep `stance` "
    "under 40 characters — it is a label, not a sentence.\n"
    "\n"
    "3. CITE ONLY [p.N] MARKERS YOU ACTUALLY READ ABOVE. Every claim needs the "
    "real page(s) it is made on. The teacher CLICKS these page numbers and lands "
    "on the page — a number you did not read above is worse than no citation at "
    "all, because it is one he will trust. If you are not certain of the page, "
    "leave the claim out.\n"
    "\n"
    "4. TWO KINDS OF TEXT ARE ABOVE, AND THEY ARE NOT THE SAME.\n"
    "   [p.N]        — the PAGE'S OWN WORDS. The author wrote these.\n"
    "   [p.N FIGURE] — OUR DESCRIPTION of a picture, diagram, photo or tab staff "
    "on that page. The author did NOT write these; we did, by looking at the "
    "image.\n"
    "   Both are real content and both are worth recording — much of this book "
    "may BE its diagrams and tab. But set `grounding` to say which one a claim "
    "came from: \"author\" if it comes from the page's own words, \"figure\" if it "
    "comes from our description of a picture. A figure-grounded claim is fine to "
    "record and cite; it just must never be repeated as the author's own "
    "sentence. If a claim draws on both, say \"figure\".\n"
    "\n"
    "5. `depth`: \"primary\" if this book is where you would send someone to learn "
    "this concept, \"secondary\" if it is covered properly but is not the focus, "
    "\"mention\" if it goes by in passing.\n"
    "\n"
    "6. `name_el`: the concept's name in GREEK. The teacher is Greek and reads "
    "the canon's index in Greek."
)

COMPILE_SYSTEM_SLICE_ID = "canon.system"
COMPILE_TASK_SLICE_ID = "canon.task"

# No `minItems`/`maxLength` anywhere: `llm/schema.py` strips them (structured
# outputs do not enforce string or array constraints) and would do so AFTER the
# tokens are paid for. The constraints they would have expressed live in
# `COMPILE_TASK` above and in `_validate_claim` below — stated to the model, and
# enforced in Python.
CONCEPT_SCHEMA = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "description": "Every concept this book teaches, named in your own words.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "This concept's name, in your own words, as THIS book "
                            "frames it. No fixed vocabulary."
                        ),
                    },
                    "name_el": {
                        "type": "string",
                        "description": "The same name in Greek.",
                    },
                    "claims": {
                        "type": "array",
                        "description": "What this author says about it, with real pages.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "description": (
                                        "The claim in your own words — this "
                                        "author's position, not a neutral summary."
                                    ),
                                },
                                "pages": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": (
                                        "The [p.N] page number(s) this is said on. "
                                        "Only numbers you actually read above."
                                    ),
                                },
                                "stance": {
                                    "type": "string",
                                    "description": (
                                        "This author's position, under 40 "
                                        "characters. His emphasis, not a neutral "
                                        "one."
                                    ),
                                },
                                "depth": {
                                    "type": "string",
                                    "enum": list(CLAIM_DEPTHS),
                                },
                                "grounding": {
                                    "type": "string",
                                    "enum": list(CLAIM_GROUNDINGS),
                                    "description": (
                                        "\"author\" if from the page's own words, "
                                        "\"figure\" if from our description of a "
                                        "picture."
                                    ),
                                },
                            },
                            "required": ["text", "pages", "stance", "depth", "grounding"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "name_el", "claims"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["concepts"],
    "additionalProperties": False,
}


def build_compile_messages(ctx: BookContext, source=None) -> list[dict]:
    """`[system, the whole book, the task]`. No `cache: True` — see the module
    docstring: one call per book, nothing reads the prefix again, and a cache
    write costs 1.25x."""
    from app.prompts.overrides import resolve

    return [
        {"role": "system", "content": resolve(source, COMPILE_SYSTEM_SLICE_ID,
                                              COMPILE_SYSTEM)},
        {"role": "user", "content": ctx.text},
        {"role": "user", "content": resolve(source, COMPILE_TASK_SLICE_ID,
                                            COMPILE_TASK)},
    ]


# ---------------------------------------------------------------------------
# Validation — the two things never taken on trust
# ---------------------------------------------------------------------------

def _valid_pages(raw_pages, ctx: BookContext, *, where: str) -> list[int]:
    """The cited pages that the model was ACTUALLY SHOWN, in order, deduplicated.

    Drop-and-log, never store. The log line is not politeness: a citation that
    vanishes silently is how a book compiles to half a ledger and nobody finds out
    for a month.
    """
    kept: list[int] = []
    dropped: list = []
    for page in raw_pages or []:
        # `isinstance(True, int)` is True in Python, and `True in {1}` is also
        # True — so a bool would sail through as a citation to page 1.
        if isinstance(page, bool) or not isinstance(page, int):
            dropped.append(page)
        elif page not in ctx.page_index:
            dropped.append(page)
        elif page not in kept:
            kept.append(page)
    if dropped:
        log.warning(
            "canon: dropping hallucinated citation(s) %s for %s — %r has pages %s",
            dropped, where, ctx.title,
            f"{min(ctx.page_index)}-{max(ctx.page_index)}" if ctx.page_index else "none",
        )
    return kept


def _grounding(declared, pages: list[int], ctx: BookContext) -> str:
    """MAY THIS TEXT BE PRESENTED AS THE AUTHOR'S OWN WORDS?

    Structural first, declaration last — the same discipline as the page check.
    Wherever the PAGE ITSELF settles the question, the model's answer is not
    consulted at all:

      * every cited page has no figure on it  -> "author". Safe by the contract's
        TOTALITY: no figure region means every character of that page is the
        page's own words. (Believing a wrong "figure" here would quietly demote a
        real quotation to unquotable — a smaller harm, but a real one.)
      * every cited page has no prose on it   -> "figure". This is the one that
        matters: a bare tab page has no sentence for the author to have written,
        so a claim citing it CANNOT be his, whatever the model declared.

    Only a page carrying BOTH leaves nothing to check against, and there the
    declaration is all there is. An unrecognised value fails to "figure" — the
    direction that cannot fabricate: mistaking the book's prose for our
    description costs a paragraph of attribution, mistaking our description for
    the book's prose is a quotation the author never wrote, with a real page
    number on it.
    """
    cited = set(pages)
    if not cited & ctx.figure_pages:
        return "author"
    if not cited & ctx.author_pages:
        return "figure"
    return declared if declared in CLAIM_GROUNDINGS else "figure"


def _validate_claim(raw: dict, ctx: BookContext, *, where: str) -> dict | None:
    """One claim, checked into shape — or None, which means it does not enter the
    canon at all."""
    if not isinstance(raw, dict):
        return None
    text = (raw.get("text") or "").strip()
    if not text:
        return None

    pages = _valid_pages(raw.get("pages"), ctx, where=where)
    if not pages:
        # An uncitable claim is not a claim. Storing it with `pages=[]` would put a
        # sentence in the canon that no page supports, which is the exact thing the
        # canon exists to make impossible.
        log.warning("canon: dropping claim with no citable page for %s: %.80r",
                    where, text)
        return None

    stance = (raw.get("stance") or "").strip() or None
    if stance and len(stance) > _STANCE_MAX:
        log.info("canon: truncating an over-long stance for %s: %.60r", where, stance)
        stance = stance[:_STANCE_MAX]

    depth = raw.get("depth")
    return {
        "text": text,
        "pages": pages,
        "stance": stance,
        "depth": depth if depth in CLAIM_DEPTHS else None,
        "grounding": _grounding(raw.get("grounding"), pages, ctx),
    }


# ---------------------------------------------------------------------------
# Concepts
# ---------------------------------------------------------------------------

# `\w` under re.UNICODE, so a Greek or accented name slugs to itself rather than
# to nothing. An ASCII fold here would silently collapse every non-Latin concept
# name to the empty string — i.e. to one shared key — which is the "silent
# discard" the free-form design exists to make impossible.
_SLUG = re.compile(r"[^\w]+", re.UNICODE)
_KEY_MAX = 120
_KEY_HASH = 7


def _concept_key(label: str) -> str:
    """A slug of the model's own words, stable and collision-free.

    TWO PROPERTIES, AND BOTH ARE LOAD-BEARING:

      * the SAME name always yields the same key — that is what makes the second
        book to say "alternate picking" reuse the first book's row, a free
        reconciliation before C3 runs at all;
      * DIFFERENT names never yield the same key. Hence the hash suffix past 120
        characters: a plain truncation would merge two genuinely different long
        concepts into one row and delete one of them — silently, which is the one
        failure mode the no-taxonomy decision exists to rule out.
    """
    slug = _SLUG.sub("-", (label or "").strip().lower()).strip("-")
    if not slug:
        # A name of pure punctuation. Vanishingly unlikely, and it must still not
        # collide with the next one.
        return "concept-" + _digest(label or "")[:8]
    if len(slug) <= _KEY_MAX:
        return slug
    head = slug[: _KEY_MAX - _KEY_HASH - 1].rstrip("-")
    return f"{head}-{_digest(slug)[:_KEY_HASH]}"


def _digest(text: str) -> str:
    """A disambiguator, not a signature — nothing here is authenticated and there
    is no adversary to be collision-resistant against. SHA-256 anyway: it costs
    nothing at this call rate and a reader should not have to work out that a
    weaker hash was fine."""
    return hashlib.sha256(text.encode()).hexdigest()


def _get_or_create_concept(db, name: str, name_el: str | None) -> Concept:
    """UPSERT, not "look then leap". Two books compiling concurrently would both
    see the empty row and both INSERT; `concept.key`'s UNIQUE index is what turns
    that race into an IntegrityError we can recover from instead of a duplicate."""
    key = _concept_key(name)
    concept = db.scalar(select(Concept).where(Concept.key == key))
    if concept is None:
        try:
            with db.begin_nested():
                concept = Concept(key=key, label_en=name, label_el=name_el or None)
                db.add(concept)
                db.flush()
        except IntegrityError:
            concept = db.scalar(select(Concept).where(Concept.key == key))
            if concept is None:                     # not the race, then — real
                raise
    if name_el and not concept.label_el:
        # A later book supplying the Greek name the first one skipped. Filling a
        # NULL is additive; overwriting would let the last book compiled rename
        # every concept in the canon.
        concept.label_el = name_el
    return concept


def _upsert_alias(db, concept_id: UUID, source_id: UUID, alias: str) -> None:
    existing = db.scalar(
        select(ConceptAlias).where(ConceptAlias.concept_id == concept_id,
                                   ConceptAlias.source_id == source_id)
    )
    if existing is None:
        db.add(ConceptAlias(concept_id=concept_id, source_id=source_id, alias=alias))
    else:
        existing.alias = alias


def _clear_source_ledger(db, source_id: UUID) -> None:
    """A RECOMPILE REPLACES, IT DOES NOT APPEND. Two readings of one book
    interleaved is a ledger nobody can reason about — every claim the model
    happened to phrase the same way twice would simply be there twice, and no
    query could tell the duplicate from a genuine repetition."""
    db.execute(delete(ConceptClaim).where(ConceptClaim.source_id == source_id))
    db.execute(delete(ConceptAlias).where(ConceptAlias.source_id == source_id))


def _prune_orphan_concepts(db) -> None:
    """A concept no book claims anything about is not a concept, it is a name.

    Canon-wide rather than per-source, and that is correct: a concept keeps its row
    for as long as ANY book still supports it, so recompiling Powers cannot prune
    something Hunter still claims. Aliases follow via ON DELETE CASCADE.
    """
    db.execute(
        delete(Concept).where(
            ~select(ConceptClaim.id)
            .where(ConceptClaim.concept_id == Concept.id)
            .exists()
        )
    )


# ---------------------------------------------------------------------------
# The compile
# ---------------------------------------------------------------------------

def _model_name() -> str:
    """Which model is about to read the book — stamped on `book_compile` so that
    "was this compiled by the good model?" is a question the DATA answers."""
    try:
        return resolve_llm_config().model
    except Exception:
        log.warning("canon: could not resolve the model name", exc_info=True)
        return "unknown"


def _measured_input_tokens(provider, fallback: int) -> int:
    """What the call ACTUALLY cost to send, from the provider's own usage report,
    falling back to our estimate when it does not offer one.

    Two shapes, because there are two providers: `ClaudeProvider.last_usage` is
    flat (`{input_tokens: N, ...}`), `ClaudeCLIProvider.last_usage` nests the
    CLI's own report under `usage` alongside `cost_usd`. Reading both here beats
    making the canon care which provider the tutor picked.
    """
    usage = getattr(provider, "last_usage", None) or {}
    inner = usage.get("usage") if isinstance(usage.get("usage"), dict) else usage
    measured = inner.get("input_tokens") if isinstance(inner, dict) else None
    return int(measured) if isinstance(measured, int) and measured > 0 else fallback


def _upsert_compile(db, source_id: UUID, **fields) -> BookCompile:
    record = db.get(BookCompile, source_id)
    if record is None:
        record = BookCompile(source_id=source_id)
        db.add(record)
    for key, value in fields.items():
        setattr(record, key, value)
    db.flush()
    return record


def compile_book(db, source_id: UUID, *, force: bool = False) -> BookCompile:
    """Read one book whole; write its concepts, claims and citations. THE C2 ENTRY
    POINT, and the shape C3-C6 consume.

        record = compile_book(db, source.id)          # never re-reads a "ready" book
        record = compile_book(db, source.id, force=True)   # re-reads, and REPLACES

    Returns the `BookCompile` row, committed. `status` is the whole answer:
    "ready" (with `concept_count`, `model`, `token_count`, `compiled_at`), or
    "failed" (with `error`). What it wrote is reachable from the row —
    `ConceptClaim.source_id == source_id` is that book's ledger.

    NEVER RE-READS A BOOK IT ALREADY READ, and that is the point rather than an
    optimisation. Chris: *"every generation, library, curriculum, etc.. every user
    data, has to be persisting! ... when i send him an update of the app, the data
    of the user must be the same!"* Ten books is ~$13 of real model time on his own
    subscription; an app update that silently re-spent it is the top severity class
    in this plan. So "ready" + no `force` returns the existing row having called
    nothing. Only "ready" guards — a "failed" or "running" book was never read to
    completion, so retrying it re-spends nothing and does not need `force`.

    RAISES: `LookupError` for an unknown source. The provider's `LLMError`
    propagates — but only AFTER `status="failed"` and the reason are committed to
    the row, because the caller is a background job and by then there is nobody
    left to tell.
    """
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise LookupError(f"no knowledge source {source_id}")

    record = db.get(BookCompile, source_id)
    if record is not None and record.status == "ready" and not force:
        log.info("canon: %r is already compiled (%s concepts, %s) — not re-reading it",
                 source.title, record.concept_count, record.model)
        return record

    ctx = build_book_context(db, source_id)
    if ctx.is_empty:
        # Gallagher is 388 pages of `text IS NULL` right now, mid-OCR. Compiling it
        # would buy a call that reads an empty book and returns a confident nothing.
        log.warning("canon: %r has no readable text — not compiling", source.title)
        record = _upsert_compile(
            db, source_id, status="failed", model=None, error=(
                "This book has no readable text yet — it has not finished being "
                "read (OCR). Nothing was compiled and nothing was spent."
            ),
        )
        db.commit()
        return record

    model = _model_name()
    record = _upsert_compile(db, source_id, status="running", model=model,
                             token_count=ctx.token_count, error=None)
    db.commit()

    provider = get_provider()
    try:
        data = provider.guided_json(build_compile_messages(ctx), CONCEPT_SCHEMA)
    except Exception as e:
        record.status = "failed"
        record.error = str(e)
        db.commit()
        log.warning("canon: compiling %r failed: %s", source.title, e)
        raise

    _clear_source_ledger(db, source_id)
    concept_count = _persist(db, source, ctx, data)

    record.status = "ready"
    record.concept_count = concept_count
    record.token_count = _measured_input_tokens(provider, ctx.token_count)
    record.compiled_at = datetime.now(timezone.utc)
    record.error = None
    db.commit()
    log.info("canon: compiled %r — %s concepts from %s pages, %s tokens (%s)",
             source.title, concept_count, len(ctx.page_index), record.token_count,
             model)
    return record


def _persist(db, source: KnowledgeSource, ctx: BookContext, data: dict) -> int:
    """The model's answer -> rows, with every citation checked on the way in.
    Returns how many concepts actually survived validation."""
    concepts = (data or {}).get("concepts") or []
    kept = 0
    for entry in concepts:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        where = f"{source.title!r}/{name!r}"

        claims = [c for c in (
            _validate_claim(raw, ctx, where=where) for raw in entry.get("claims") or []
        ) if c]
        if not claims:
            log.warning("canon: %s kept no citable claim — dropping the concept", where)
            continue

        concept = _get_or_create_concept(db, name, entry.get("name_el"))
        _upsert_alias(db, concept.id, source.id, name)
        for claim in claims:
            db.add(ConceptClaim(concept_id=concept.id, source_id=source.id, **claim))
        kept += 1

    db.flush()
    _prune_orphan_concepts(db)
    return kept
