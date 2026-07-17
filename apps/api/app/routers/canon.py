"""`/canon` — the READ-ONLY browse surface for the concept canon (Part B, C7).

The tutor asked three times to SEE the canon. C8 gave it a search
(`POST /knowledge/concepts/search`, in `routers/knowledge.py`); this router gives
it a front page — `GET /canon/concepts` lists everything the compile produced so
he can open the canon and read it without first knowing what to type.

STRICTLY READ-ONLY, AND THAT IS LOAD-BEARING. Nothing here compiles, recompiles,
or writes a row. Reading ten books cost real money on the tutor's own
subscription; the one place a browse could do harm is by triggering a re-read, so
it does not — it renders the ledger, exactly like `build_canon_context` and
`canon/search`. The one WRITE the tutor can start (compile a not-yet-read book)
already lives at `POST /knowledge/sources/{id}/compile` in `routers/library.py`
and stays there — this router does not duplicate it.

Auth: every route sits behind the whole-API `gt_session` password gate
(`app/auth/middleware.py`), same as every other router — nothing to declare here.
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.canon.browse import list_concepts
from app.db import get_db
from app.models.canon import BookCompile
from app.schemas.canon import CanonOverviewOut
from app.schemas.knowledge import ConceptHitOut

log = logging.getLogger(__name__)

router = APIRouter(prefix="/canon", tags=["canon"])


@router.get("/concepts", response_model=CanonOverviewOut)
def browse_concepts(db: Session = Depends(get_db)) -> CanonOverviewOut:
    """The whole browsable canon: every compiled concept (most-divergent first),
    plus the counts a beginner needs to read the page honestly.

    Empty canon → `concepts: []` with zeroed counts, 200 (not an error): a library
    whose books have not been compiled yet has an empty canon, which is a true and
    renderable state, not a failure.
    """
    hits = list_concepts(db)
    books_compiled = db.scalar(
        select(func.count()).select_from(BookCompile).where(BookCompile.status == "ready")
    ) or 0
    books_compiling = db.scalar(
        select(func.count()).select_from(BookCompile).where(BookCompile.status == "running")
    ) or 0
    return CanonOverviewOut(
        concepts=[ConceptHitOut.model_validate(h, from_attributes=True) for h in hits],
        total_concepts=len(hits),
        divergence_count=sum(1 for h in hits if h.divergence),
        books_compiled=int(books_compiled),
        books_compiling=int(books_compiling),
    )
