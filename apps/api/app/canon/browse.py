"""C7 — the concept canon, BROWSABLE. The tutor can finally SEE it.

Chris, three times over: *"that would also be nice to see somewhere in the
library, i mean the canon generations"* / *"i dont see any canon component, or
text anywhere."* C8 made the canon SEARCHABLE (you have to know what to type);
this makes it BROWSABLE (you open it and read what your books say). Same data,
the other half of the door.

WHY A SECOND ENTRY POINT AND NOT JUST SEARCH. `canon/search.py` answers "what do
my books say about X?" — it needs an X. A tutor who has never seen the canon does
not yet have an X; he has a question shaped like "what did all this reading
actually produce?". So this lists EVERY compiled concept, most-divergent first,
each with the same cross-book positions a search hit carries. It is the index of
the book the compile wrote.

DIVERGENCE IS THE HEADLINE, AT THE LIST LEVEL TOO. `render.py` orders the prompt
block most-covered-first because coverage correlates with disagreement. Here the
reader is a human being looking for the payoff, so we are blunter about it: a
concept where the books DISAGREE sorts ahead of one where they merely agree. A
canon view that buried its divergences under fifty consensus rows would have
missed the entire point of owning ten books (see `render.py`'s docstring).

READS THE LEDGER, NEVER COMPILES. Like `build_canon_context` and `search`, this is
a render of what is already on disk. It never triggers a re-read of a book —
reading ten books cost real money on the tutor's own subscription, and a browse
that could re-spend it would be the top severity class in this plan.

REUSES C8's GROUPING, does not fork it. The consensus/divergence/only-in split is
`canon/search._positions` (which is itself `render.py`'s exact equality rule),
imported rather than re-derived — so a concept looks the same whether the tutor
searched for it or scrolled to it, and neither can drift from the block the
drafting model reads.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from app.canon.search import ConceptHit, _positions
from app.models.canon import Concept, ConceptClaim
from app.models.knowledge import KnowledgeSource


def list_concepts(db) -> list[ConceptHit]:
    """Every concept in the canon, most-divergent-then-most-covered first, each
    with its full cross-book positions and citations.

    A concept with no claims is a NAME with no book behind it (a reconciliation
    artefact, or a row written before a compile failed) — it is dropped here, the
    same way `render.py` only gives a ref to a book that actually contributed. The
    browse view is a render of what the books SAY, not of what got named.

    `score` is 0.0 on every hit: there is no query, so there is no relevance to
    rank by. Ordering is the canon's own content — divergence, then coverage — a
    pure function of the ledger, so two calls return the same order.
    """
    concepts = db.scalars(select(Concept)).all()
    claims = db.scalars(select(ConceptClaim)).all()

    by_concept: dict[UUID, list[ConceptClaim]] = {}
    for claim in claims:
        by_concept.setdefault(claim.concept_id, []).append(claim)

    source_ids = {c.source_id for c in claims}
    titles = {
        s.id: (s.title or "")
        for s in db.scalars(
            select(KnowledgeSource).where(KnowledgeSource.id.in_(source_ids))
        ).all()
    } if source_ids else {}

    hits: list[ConceptHit] = []
    for concept in concepts:
        concept_claims = by_concept.get(concept.id)
        if not concept_claims:
            continue  # a name with no book behind it — not part of the canon a reader sees
        positions, divergence = _positions(concept_claims, titles)
        hits.append(ConceptHit(
            concept_id=concept.id,
            key=concept.key,
            label_en=concept.label_en,
            label_el=concept.label_el,
            score=0.0,
            coverage=len({c.source_id for c in concept_claims}),
            divergence=divergence,
            positions=positions,
        ))

    # Divergence first (the payoff), then widest coverage, then most claims, then
    # key — deterministic, a pure function of the canon's content.
    hits.sort(key=lambda h: (not h.divergence, -h.coverage,
                             -sum(len(p.citations) for p in h.positions), h.key))
    return hits
