"""The concept canon — what each book says, keyed by concept rather than by page.

WHY THIS TABLE SET EXISTS, IN CHRIS'S WORDS:

    "10 books all of them talking for guitar TONE, with much information
     repeated, but also some unique perspectives from each writer. So whats the
     plan there to create the ultimate curriculum, combining the knowledge of the
     10 books all together?"

Ten tone books are ~80% the same content. Keyed by concept, that redundancy
collapses to nothing and what SURVIVES is where the authors disagree — which is
the one thing no single book can give him, and the reason the tenth book was
worth buying. So the shape below is built to make divergence expressible and
findable:

    concept                     one idea, named in a model's own words
      +-- concept_claim   * N   what ONE book says about it, with real pages
      +-- concept_alias   * N   what each book CALLED it

Two claims on one concept from two sources with opposing `stance` IS a
divergence. Nothing else needs to model it. `SELECT ... GROUP BY concept_id
HAVING count(DISTINCT source_id) > 1` is the product.

NOT A SUMMARY, AND NOT AN INDEX. Every claim carries `source_id` + `pages`, so
the canon is a set of POINTERS INTO HIS ACTUAL BOOKS. `curriculum/corpus.py`
explains at length why curriculum authoring reads rather than searches; the canon
does not undo that. It moves the searching to compile time, where one model reads
one whole book with nobody's lesson depending on the outcome, and writes down
where things are. Draft time then dereferences a pointer — no embedding, no
top-k, no silent miss. A canon that stored prose instead of citations would be a
lossy summary of his library and would have thrown away the only thing that makes
it his.

THIS IS USER DATA. Chris: *"every generation, library, curriculum, etc.. every
user data, has to be persisting! even backup-able!"* — so it is Postgres, it is in
`pg_dump`, its migrations are additive, and it is NEVER auto-recompiled. Reading
ten books costs real money; an app update that silently re-spent it would be a bug
of the top severity class. `book_compile` is the row that makes "we already did
this" a fact the code can check.
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    ARRAY,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PkMixin, TimestampMixin

# `book_compile.status`. Deliberately smaller than `page`'s vocabulary: a compile
# is one call about one book, so there is no partial state to describe — either
# the book has a ledger or it does not.
#
#   "running" — a compile is in flight. Also the crash marker: a process that dies
#               mid-call leaves this behind, which is how a resumed run can tell
#               "never started" from "started and vanished".
#   "ready"   — there is a ledger, and `concept_count` says how big.
#   "failed"  — `error` says why. Retryable; nothing was written.
COMPILE_STATUSES = frozenset({"running", "ready", "failed"})

# `concept_claim.depth` — how deeply THIS book treats THIS concept, which is what
# lets Pass 3 say "S5 pp.110-118 goes deepest" and send the lesson to the book
# that actually teaches it rather than the one that name-checks it.
#
# A closed vocabulary here is NOT the pre-fixed taxonomy the spec forbids. That
# prohibition is about CONCEPT NAMES, where a fixed list acts as a filter and
# drops the unique take that justified the tenth book. `depth` names no concept —
# it is a measurement of one, on an axis with an obvious floor and ceiling, and
# three books calling the same treatment "primary" is precisely the point.
CLAIM_DEPTHS = ("primary", "secondary", "mention")

# `concept_claim.grounding` — THE [FIGURE] CONTRACT, PERSISTED.
#
# `brain/ocr.py` classifies every character of a page: text inside a
# [FIGURE]...[/FIGURE] region is OURS (a model's description of a picture),
# everything outside it is the PAGE'S OWN WORDS. That distinction is invisible
# once a claim is written down — both read like English prose about a guitar —
# and the cost of losing it is exact: quoting our description of a photo as the
# author's sentence is a fabricated citation WITH A REAL PAGE NUMBER ON IT, which
# is worse than having no description at all, because the tutor clicks it, lands
# on a real page, and finds a photo that does not say what we claimed.
#
# So the column answers exactly ONE question, and it is binary:
#
#     MAY THIS TEXT BE PRESENTED AS THE AUTHOR'S OWN WORDS?
#
#   "author" — yes. The claim is grounded in text outside every figure region.
#   "figure" — NO. It is grounded in our description of a picture. Still true,
#              still citable ("the diagram on p.31 shows..."), never quotable.
#
# Two values rather than three, deliberately: a claim drawing on both a
# photograph and the prose around it collapses to "figure", because the question
# above still answers NO. The fail-safe direction is the whole point — mistaking
# the book's prose for our description costs a paragraph of attribution;
# mistaking our description for the book's prose is the fabrication this codebase
# is architected against.
CLAIM_GROUNDINGS = ("author", "figure")


class Concept(Base, PkMixin, TimestampMixin):
    """One idea, named by a model in ITS OWN WORDS.

    NO PRE-FIXED VOCABULARY, and that is measured rather than tasteful. The books'
    TOCs are too coarse to seed one (Gallagher's is "Chapter 2: Wood"; none of the
    PDFs carry embedded bookmarks; Hunter's printed TOC extracts out of order) —
    but the real reason is structural: **a fixed vocabulary is a filter, and a
    filter's failure mode is dropping the unique take that justified buying the
    tenth book.** Free-form naming means reconciliation (Pass 2/C3) can only ever
    MERGE synonyms. It has no mechanism for silent discard, and that asymmetry is
    the design.

    `key` is a slug of `label_en`, and it is UNIQUE so that the second book to
    name a concept exactly what the first one did REUSES this row — a free
    reconciliation that costs nothing and happens before C3 ever runs. The
    constraint is also what makes `canon/compile._get_or_create` an upsert rather
    than a race between two books compiling at once.

    `label_el` because the tutor is Greek and `el` is the default locale — a canon
    he cannot read the index of is a canon he cannot steer. It is filled by the
    same compile call that names the concept (free — it is already writing) and is
    nullable for the row that predates a model bothering.
    """

    __tablename__ = "concept"

    key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    # Text, not String(N). These hold the MODEL'S OWN WORDS, and the one thing
    # this column must never do is truncate them into a different concept.
    label_en: Mapped[str] = mapped_column(Text)
    label_el: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConceptClaim(Base, PkMixin):
    """What ONE book says about ONE concept, and WHERE it says it.

    The unit of divergence. Two of these on the same `concept_id` from different
    `source_id`s with opposing `stance` is the thing the whole plan exists to
    surface, and it needs no further modelling than this table's grain.

    `created_at` only, no `updated_at` — deliberately not `TimestampMixin`. A claim
    is not edited; a recompile REPLACES a book's claims wholesale (see
    `canon/compile.compile_book`), because a half-updated ledger that mixes two
    readings of the same book is a canon nobody can reason about.
    """

    __tablename__ = "concept_claim"

    concept_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("concept.id", ondelete="CASCADE"), index=True)
    # ON DELETE CASCADE, and it is load-bearing rather than tidy: deleting a book
    # MUST take its claims with it, or the canon cites a book that is gone and the
    # tutor clicks a chip that opens nothing.
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    # A REAL int[], not JSON. Both round-trip `[47, 48]` through SQLAlchemy
    # identically and no Python assertion can tell them apart — and then
    # `WHERE 47 = ANY(pages)`, which is exactly how Pass 5 hydrates a lesson from
    # the pages a concept actually lives on, is a syntax error against JSON.
    # Every entry is validated against the source's real page set BEFORE it is
    # written (`canon/compile`); a page number in here is a promise.
    pages: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    # The author's POSITION, in the model's own words and free-form — no enum.
    # An enum here would flatten "prefers a lower treble-side height" and "warns
    # it kills sustain" into one bucket and delete the disagreement, which is the
    # product. Clamped to 40 chars by the schema the model is given, so it stays a
    # label and not an essay.
    stance: Mapped[str | None] = mapped_column(String(40), nullable=True)
    depth: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # See CLAIM_GROUNDINGS. Not nullable and no server default: a claim whose
    # provenance nobody decided is exactly the claim that gets quoted wrongly.
    grounding: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class ConceptAlias(Base, PkMixin, TimestampMixin):
    """What THIS book called this concept.

    EXISTS SO A BAD MERGE IS REVERSIBLE WITHOUT RECOMPILING — i.e. without
    re-spending. C3 will cluster ~1,000 free-form names down to a canonical
    vocabulary, and it will get some of them wrong. Without this row, undoing that
    means re-reading ten books to recover names the merge overwrote; with it, the
    original name is still on disk, per book, and the fix is a query.

    That is also why the alias carries `source_id`: "who called it this" is the
    whole question when deciding whether two books meant the same thing.
    """

    __tablename__ = "concept_alias"
    __table_args__ = (
        # One book calls one concept one thing. A second compile of the same book
        # must not stack duplicate aliases, and this is what makes the alias write
        # idempotent at the database rather than by hoping.
        UniqueConstraint("concept_id", "source_id", name="uq_concept_alias_concept_source"),
    )

    concept_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("concept.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(Text)


class BookCompile(Base):
    """One row per book: did we read it, with what, when, and what did it cost.

    KEYED ON `source_id` AS THE PRIMARY KEY — there is no separate `id`. One book
    has exactly one compile state; a table that could hold two rows for one book
    would immediately raise "which one is true?", and the answer would be decided
    by an ORDER BY somewhere.

    THIS ROW IS THE MONEY GUARD. Reading ten books costs ~$13 of real model time,
    and Chris's constraint is explicit: an app update must not silently re-spend it
    re-reading books it already read. `compile_book` checks for `status="ready"`
    here and returns without spending unless it is explicitly forced. That check is
    the feature; this table is just where it looks.

    `model` exists so that *"was this compiled by the good model?"* is a question
    the DATA can answer rather than a thing someone remembers. A canon half-built
    by a cheap model is worse than no canon — it looks identical, and the tutor
    would have no way to tell which of his books got the careless read.
    """

    __tablename__ = "book_compile"

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    model: Mapped[str | None] = mapped_column(String(40), nullable=True)
    compiled_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # What the BOOK cost to read, in input tokens — the number that makes the
    # spec's "~$1.26/book" checkable against reality instead of trusted.
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    concept_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
