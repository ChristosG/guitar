"""The seam from the Library to lesson authoring (sub-project B).

DELIBERATELY A STUB. This captures a selection the tutor made while reading —
with the page provenance attached, so the lesson B eventually drafts can be
grounded in (and cite) the exact passage it came from. Drafting the lesson is
sub-project B's job and is explicitly NOT built here; this exists so the wire
is real and tested before B lands.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.knowledge import KnowledgeSource
from app.schemas.lessons import SelectionIn, SelectionOut

router = APIRouter(prefix="/lessons", tags=["lessons"])


@router.post("/from-selection", response_model=SelectionOut, status_code=201)
def from_selection(payload: SelectionIn, db: Session = Depends(get_db)) -> SelectionOut:
    source = db.get(KnowledgeSource, payload.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return SelectionOut(
        selection_id=uuid.uuid4(),
        source_id=source.id,
        source_title=source.title,
        page_no=payload.page_no,
        text=payload.text,
    )
