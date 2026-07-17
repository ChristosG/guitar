"""C8 — the concept canon, made searchable: from Library search AND from chat.

Chris: *"those concepts might be nice to be searchable bro, by the search and
from the chat screen too!"*

WHY THIS IS A SEARCH THE OTHER ONE CANNOT BE. `brain/retrieve.search` returns
chunks from ONE book and has no notion that two books disagree. The canon does:
every concept carries claims from EVERY book that treats it, and where two authors
take opposing positions that contradiction is the single thing a shelf of ten
books gives that no one book can (see `models/canon.py`, `canon/render.py`). So
searching the canon is not "search, but over concepts" — it is the only search
that can answer *"what do my books say about pickup height?"* with the CROSS-BOOK
synthesis and, when they disagree, BOTH positions with their own citations. That
divergence is the value; averaging it away would fail the request the same way a
consensus-only canon would.

BM25, NOT A NEW ENGINE, AND NOT DENSE. Per [[guitar-tutor-retrieval-is-the-weak-link]]
the dense arm is measured to miss on this corpus (a bare model name tops out at
0.787 cosine), so BM25 is LOAD-BEARING here, not optional. This reuses the EXACT
PyStemmer tokenizer and Okapi BM25 machinery the chunk arm uses
(`brain/lexical._build`/`LexicalIndex`), over a per-concept document built from
`label_en` + `label_el` + every claim's text and stance. One index, same math, no
second search engine to keep calibrated.

CROSS-LINGUAL THE SAME WAY THE CHUNK ARM IS. The tutor's default locale is Greek;
his books are English and so is every claim's prose. So a Greek query is normalised
to the corpus language first (`retrieve.normalize_query` — the same load-bearing,
circuit-broken, never-raising call the chunk search uses), which is why a Greek
question still lands on the English claim text. `label_el` is indexed too, so the
raw-Greek fallback (translation unavailable) still finds a concept the tutor named
in his own language.

CONSENSUS/DIVERGENCE IS render.py'S, SINGLE-SOURCED. The definition of "these two
books agree" is EXACT equality of the normalised position — never a fuzzy ratio,
because the one word that differs is usually the whole disagreement ("in the hands"
vs "in the wood"). That rule is load-bearing and lives in `render._norm`/
`_position`; this module IMPORTS them rather than re-deriving the grouping, so the
structured search result and the prompt-block the model also reads can never drift
into two different notions of agreement.

STALENESS, NOT INVALIDATION — the same design as `brain/lexical`. The index is a
plain in-process structure; a recompile REPLACES a book's claims wholesale. Rather
than wire an invalidate() into every compile/delete path (and forget one),
`get_concept_index` fingerprints the canon (concept + claim counts and newest
timestamps, ~1ms) and rebuilds when it moves.

READS THE LEDGER, NEVER RE-COMPILES. Like `build_canon_context`, this is a render
of what is already on disk — it never triggers a re-read of a book. Reading ten
books cost real money on the tutor's own subscription; a search that could re-spend
it would be the top severity class in this plan.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import func, select

from app.brain.lexical import LexicalIndex, _build
from app.brain.retrieve import normalize_query
from app.canon.render import _norm, _page_ranges, _position
from app.models.canon import Concept, ConceptClaim
from app.models.knowledge import KnowledgeSource

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The shape a hit takes — structured so divergence is EXPLICIT, not implied
# ---------------------------------------------------------------------------

@dataclass
class ConceptCitation:
    """One pointer into a real book. `source_id` + `pages` is exactly what the
    Reader deep-link needs (`GET /knowledge/sources/{source_id}/pages/{page_no}`).

    `grounding` carries the [FIGURE] contract through to the surface: an "author"
    citation may be quoted as the author's words; a "figure" one is OUR description
    of a picture — citable, never quotable.
    """

    source_id: UUID
    source_title: str
    pages: list[int]
    pages_label: str          # "p.57" / "pp.57-58" — via render._page_ranges
    grounding: str            # "author" | "figure"


@dataclass
class ConceptPosition:
    """One position on a concept: what it is, who holds it, and its citations.

    `kind` mirrors `canon/render.py`'s three labels exactly:
      - "consensus"  — two or more DIFFERENT books, the same position, verbatim.
      - "divergence" — a book departing from what the others said. This is the
                       product; it must never be averaged into the consensus.
      - "only_in"    — one book covers this concept. A unique take, nobody to
                       disagree with — honest under its own label, never dropped.
    """

    kind: str
    position: str
    books: list[str]
    citations: list[ConceptCitation]


@dataclass
class ConceptHit:
    """One concept the query matched, with its full cross-book picture."""

    concept_id: UUID
    key: str
    label_en: str
    label_el: str | None
    score: float                          # BM25, ordering only
    coverage: int                         # distinct books that treat this concept
    divergence: bool                      # do the books disagree?
    positions: list[ConceptPosition] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The BM25 index over concepts — reusing lexical.py's machinery, not a new one
# ---------------------------------------------------------------------------

_index: LexicalIndex | None = None
_lock = threading.Lock()


def _fingerprint(db) -> tuple:
    """Moves on any compile, recompile or delete. Two aggregates, ~1ms: concept
    count + newest concept, and claim count + newest claim — a recompile replaces
    a book's claims wholesale (new `created_at`), so the claim half catches churn
    the concept half cannot see. Deliberately NOT a hash of the text: that would
    cost a full scan on every search, which is the thing this exists to avoid."""
    c = db.execute(select(func.count(Concept.id), func.max(Concept.updated_at))).one()
    q = db.execute(select(func.count(ConceptClaim.id), func.max(ConceptClaim.created_at))).one()
    return (int(c[0] or 0), c[1], int(q[0] or 0), q[1])


def _document(concept: Concept, claims: list[ConceptClaim]) -> str:
    """The searchable text for one concept: both labels plus every claim's prose
    and stance. `label_el` is included so the raw-Greek fallback works; the claim
    text is included so this is a search of what the books SAY, not a title lookup.
    """
    parts = [concept.label_en or "", concept.label_el or ""]
    for claim in claims:
        if claim.text:
            parts.append(claim.text)
        if claim.stance:
            parts.append(claim.stance)
    return " ".join(p for p in parts if p)


def _build_index(db, fingerprint: tuple) -> LexicalIndex:
    concepts = db.scalars(select(Concept)).all()
    claims = db.scalars(select(ConceptClaim)).all()
    by_concept: dict[UUID, list[ConceptClaim]] = {}
    for claim in claims:
        by_concept.setdefault(claim.concept_id, []).append(claim)
    ids = [c.id for c in concepts]
    texts = [_document(c, by_concept.get(c.id, [])) for c in concepts]
    return _build(ids, texts, fingerprint)


def get_concept_index(db) -> LexicalIndex:
    """The process-wide concept BM25 index, rebuilt iff the canon moved. Its own
    global, separate from `brain/lexical`'s chunk index."""
    global _index
    fp = _fingerprint(db)
    if _index is not None and _index.fingerprint == fp:
        return _index
    with _lock:
        if _index is not None and _index.fingerprint == fp:
            return _index  # another thread won the race and built the same index
        _index = _build_index(db, fp)
        log.info("concept BM25 index built: %d concepts, %d terms",
                 _index.n_docs, len(_index.df))
        return _index


def reset_concept_index() -> None:
    """Test hook. Production never needs it — `get_concept_index` self-invalidates
    on the fingerprint, exactly like `brain/lexical.reset_index`."""
    global _index
    _index = None


# ---------------------------------------------------------------------------
# Grouping a concept's claims into positions — render.py's rules, structured
# ---------------------------------------------------------------------------

def _citation(claim: ConceptClaim, title: str) -> ConceptCitation:
    pages = sorted({p for p in (claim.pages or [])
                    if isinstance(p, int) and not isinstance(p, bool)})
    return ConceptCitation(
        source_id=claim.source_id,
        source_title=title,
        pages=pages,
        pages_label=_page_ranges(claim.pages or []),
        grounding=claim.grounding,
    )


def _positions(claims: list[ConceptClaim], titles: dict[UUID, str]) -> tuple[list[ConceptPosition], bool]:
    """Group a concept's claims into consensus/divergence/only-in positions.

    Equality of the NORMALISED position decides consensus — never similarity, for
    the reason `render.py` measures at length: the one word that differs is usually
    the whole disagreement. `_norm`/`_position` are imported from `render.py` so
    this and the prompt block share one definition of "agree".
    """
    by_position: dict[str, list[ConceptClaim]] = {}
    for claim in claims:
        by_position.setdefault(_norm(_position(claim)), []).append(claim)

    consensus_groups: list[list[ConceptClaim]] = []
    solo: list[ConceptClaim] = []
    for key in sorted(by_position):
        group = by_position[key]
        if len({c.source_id for c in group}) > 1:
            consensus_groups.append(group)      # two DIFFERENT books, same words
        else:
            solo.extend(group)

    # A concept diverges when its books hold more than one distinct position
    # between them. One book contradicting itself is not a divergence (you cannot
    # disagree with yourself across books), so it takes >1 distinct SOURCE too.
    divergence = len(by_position) > 1 and len({c.source_id for c in claims}) > 1

    positions: list[ConceptPosition] = []
    # Consensus first, widest agreement first, then position text — deterministic.
    for group in sorted(consensus_groups,
                        key=lambda g: (-len({c.source_id for c in g}), _norm(_position(g[0])))):
        positions.append(ConceptPosition(
            kind="consensus",
            position=_position(group[0]),
            books=sorted({titles.get(c.source_id, "") for c in group}),
            citations=[_citation(c, titles.get(c.source_id, ""))
                       for c in sorted(group, key=lambda c: (titles.get(c.source_id, ""),
                                                             min(c.pages or [0])))],
        ))
    solo_kind = "divergence" if divergence else "only_in"
    for claim in sorted(solo, key=lambda c: (titles.get(c.source_id, ""),
                                             min(c.pages or [0]), _norm(_position(c)))):
        positions.append(ConceptPosition(
            kind=solo_kind,
            position=_position(claim),
            books=[titles.get(claim.source_id, "")],
            citations=[_citation(claim, titles.get(claim.source_id, ""))],
        ))
    return positions, divergence


# ---------------------------------------------------------------------------
# The search
# ---------------------------------------------------------------------------

def search_concepts(db, query: str, *, k: int = 8) -> list[ConceptHit]:
    """Top-`k` concepts for `query`: normalise -> BM25 over the canon -> hydrate
    each hit's cross-book positions and citations.

    Returns `[]` when the canon is empty or nothing matched — the honest answer
    for a library with no compiled concepts, not an error.
    """
    normalized = normalize_query(db, query)
    ranked = get_concept_index(db).search(normalized, k=k)
    if not ranked:
        return []

    scores = dict(ranked)
    ordered_ids = [cid for cid, _ in ranked]

    concepts = {c.id: c for c in db.scalars(
        select(Concept).where(Concept.id.in_(ordered_ids))).all()}
    claims = db.scalars(
        select(ConceptClaim).where(ConceptClaim.concept_id.in_(ordered_ids))).all()
    by_concept: dict[UUID, list[ConceptClaim]] = {}
    for claim in claims:
        by_concept.setdefault(claim.concept_id, []).append(claim)

    source_ids = {c.source_id for c in claims}
    titles = {
        s.id: (s.title or "")
        for s in db.scalars(
            select(KnowledgeSource).where(KnowledgeSource.id.in_(source_ids))).all()
    } if source_ids else {}

    hits: list[ConceptHit] = []
    for cid in ordered_ids:               # preserve BM25 order
        concept = concepts.get(cid)
        if concept is None:
            continue                      # deleted between the index build and now
        concept_claims = by_concept.get(cid, [])
        positions, divergence = _positions(concept_claims, titles)
        hits.append(ConceptHit(
            concept_id=concept.id,
            key=concept.key,
            label_en=concept.label_en,
            label_el=concept.label_el,
            score=round(scores[cid], 3),
            coverage=len({c.source_id for c in concept_claims}),
            divergence=divergence,
            positions=positions,
        ))
    return hits
