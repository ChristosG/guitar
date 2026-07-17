"""HTTP shapes for the BROWSE side of the concept canon (`routers/canon.py`, C7).

Deliberately thin, and deliberately REUSES C8's `ConceptHitOut` for the concept
itself: a concept must look identical whether the tutor searched for it
(`POST /knowledge/concepts/search`) or scrolled to it here, so both surfaces carry
the same shape — one frontend component renders either. This module only adds the
list envelope (the counts a canon landing page shows before the concepts) and does
not redefine the concept, its positions, or its citations.
"""
from pydantic import BaseModel

from app.schemas.knowledge import ConceptHitOut


class CanonOverviewOut(BaseModel):
    """`GET /canon/concepts` — the whole browsable canon in one response.

    The counts are the honest header for a beginner opening the canon for the
    first time: how many of his books have been read INTO it, how many are still
    being read, how many concepts came out, and — the number that says why the
    canon exists at all — on how many of them his books DISAGREE.
    """

    concepts: list[ConceptHitOut]
    total_concepts: int
    # The count of concepts where the books hold more than one position — the
    # headline number. `divergence` per concept is single-sourced from
    # `render.py`'s exact-equality rule via `canon/search._positions`.
    divergence_count: int
    # Books with a `ready` ledger vs. books currently being compiled. Lets the
    # page say "3 of 5 books read · 2 still reading" instead of showing an
    # apparently-thin canon as if it were finished.
    books_compiled: int
    books_compiling: int
