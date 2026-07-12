"""Library: collections, the page-level reader, media, OCR + retry (Plan 9
Task 6).

The reader endpoints are why `Page` exists (spec D1): a citation is only
trustworthy if the tutor can OPEN the page it came from and see the scan.
Ingestion (`POST /sources`, etc.) stays in `routers/knowledge.py`; this
module owns everything downstream of a `KnowledgeSource` already existing —
filing it into a `Collection`, reading its pages, retrying a failed one.

No-auth PoC posture, same as `routers/knowledge.py` — no
authentication/authorization here either; this deploys origin-locked behind
Cloudflare for a single user.
"""
import os
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.jobs.runner import run_ocr_job, run_reingest_job
from app.models.generation_job import GenerationJob
from app.models.knowledge import Collection, KnowledgeSource, Page
from app.schemas.library import (
    CollectionCreate,
    CollectionOut,
    PageOut,
    PageSummary,
    SourcePatch,
)

router = APIRouter(tags=["library"])


def _source_or_404(db: Session, source_id: uuid.UUID) -> KnowledgeSource:
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


# --- OCR ---------------------------------------------------------------

@router.post("/knowledge/sources/{source_id}/ocr", status_code=202)
def start_ocr(
    source_id: uuid.UUID, background: BackgroundTasks, db: Session = Depends(get_db)
) -> dict:
    """Enqueue a `GenerationJob(kind="ocr")` and schedule `run_ocr_job` via
    `BackgroundTasks` — mirrors `routers/curriculum.py`'s
    `generate_curriculum_endpoint` async pattern exactly (Task 6 brief: reuse
    it, no new infra). Poll `GET /jobs/{job_id}` for the outcome.

    The job row is committed BEFORE `background.add_task` is called — NOT
    left for the background task, which Starlette/FastAPI run AFTER the
    response is sent — so the runner (which opens its OWN session) is
    guaranteed to find the row, and an immediate `GET /jobs/{job_id}` poll
    sees it too.
    """
    _source_or_404(db, source_id)
    job = GenerationJob(kind="ocr", status="pending", params={"source_id": str(source_id)})
    db.add(job)
    db.commit()
    background.add_task(run_ocr_job, job.id)
    return {"job_id": str(job.id)}


# --- Reader --------------------------------------------------------------

@router.get("/knowledge/sources/{source_id}/pages", response_model=list[PageSummary])
def list_pages(source_id: uuid.UUID, db: Session = Depends(get_db)) -> list[PageSummary]:
    _source_or_404(db, source_id)
    pages = (
        db.query(Page).filter_by(source_id=source_id).order_by(Page.page_no).all()
    )
    return [PageSummary(page_no=p.page_no, status=p.status) for p in pages]


@router.get("/knowledge/sources/{source_id}/pages/{page_no}", response_model=PageOut)
def get_page(source_id: uuid.UUID, page_no: int, db: Session = Depends(get_db)) -> PageOut:
    _source_or_404(db, source_id)
    page = db.query(Page).filter_by(source_id=source_id, page_no=page_no).one_or_none()
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    total = db.query(func.count(Page.id)).filter_by(source_id=source_id).scalar()
    return PageOut(
        id=page.id,
        page_no=page.page_no,
        status=page.status,
        text=page.text,
        image_url=f"/media/pages/{page.id}.jpg" if page.image_path else None,
        total_pages=total,
    )


# --- Media -----------------------------------------------------------------

@router.get("/media/pages/{page_id}.jpg")
def get_page_image(page_id: uuid.UUID, db: Session = Depends(get_db)) -> FileResponse:
    """Serve the scan a `Page` was OCR'd from off `settings.media_dir`
    (`Page.image_path` is stored relative to it). 404, not a raw 500, for
    both a `Page` with no scan at all (`image_path is None` — the D2
    degenerate url/text/note page) and one whose file has gone missing on
    disk (volume issue) — neither should look like an unknown page id to
    the caller, but both must fail closed rather than crash."""
    page = db.get(Page, page_id)
    if page is None or not page.image_path:
        raise HTTPException(status_code=404, detail="No scan for this page")
    path = os.path.join(settings.media_dir, page.image_path)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Scan missing on disk")
    return FileResponse(path, media_type="image/jpeg")


# --- Retry (controller decision, Task 6) ------------------------------------

@router.post("/knowledge/sources/{source_id}/retry", status_code=202)
def retry_source(
    source_id: uuid.UUID, background: BackgroundTasks, db: Session = Depends(get_db)
) -> dict:
    """Retry a failed/empty ingest. Semantics depend on `source.type`
    (Task 6's controller decision — the plan's own draft router sketch had a
    broken `_reingest` call for the non-pdf branch; this replaces it):

    - "pdf"  -> enqueue a `GenerationJob(kind="ocr")`, same as `POST .../ocr`
                (`ocr_source` re-picks-up pages that are pending/failed/
                ocr_running, so this naturally only re-does the pages that
                are not yet ready).
    - "url"  -> enqueue a `GenerationJob(kind="reingest")` that re-runs the
                full ingest off the row's OWN stored `KnowledgeSource.url` —
                that column exists precisely for this (Plan 9 Task 1).
    - anything else ("text", "note", "image") -> 409. A pasted-text (or
      otherwise caller-supplied, non-fetchable) source has no original to
      re-fetch; silently no-op'ing here would look like a retry happened
      when nothing did.
    """
    source = _source_or_404(db, source_id)

    if source.type == "pdf":
        job = GenerationJob(kind="ocr", status="pending", params={"source_id": str(source_id)})
        db.add(job)
        db.commit()
        background.add_task(run_ocr_job, job.id)
        return {"job_id": str(job.id)}

    if source.type == "url":
        job = GenerationJob(
            kind="reingest", status="pending", params={"source_id": str(source_id)}
        )
        db.add(job)
        db.commit()
        background.add_task(run_reingest_job, job.id)
        return {"job_id": str(job.id)}

    raise HTTPException(
        status_code=409,
        detail="nothing to retry: a pasted-text source has no original to re-fetch",
    )


# --- Source patch (title / move between collections) -----------------------

@router.patch("/knowledge/sources/{source_id}")
def patch_source(
    source_id: uuid.UUID, payload: SourcePatch, db: Session = Depends(get_db)
) -> dict:
    """`model_fields_set` (not `exclude_none`/`exclude_unset` on a dict) so
    an explicit `{"collection_id": null}` (move to Unfiled) is distinguished
    from the field being omitted entirely (leave untouched) — both are legal
    and mean different things for a nullable FK."""
    source = _source_or_404(db, source_id)
    if payload.title is not None:
        source.title = payload.title
    if "collection_id" in payload.model_fields_set:
        source.collection_id = payload.collection_id
    db.commit()
    return {
        "id": str(source.id),
        "title": source.title,
        "collection_id": str(source.collection_id) if source.collection_id else None,
    }


# --- Collections CRUD --------------------------------------------------

@router.get("/library/collections", response_model=list[CollectionOut])
def list_collections(db: Session = Depends(get_db)) -> list[CollectionOut]:
    rows = (
        db.query(Collection, func.count(KnowledgeSource.id))
        .outerjoin(KnowledgeSource, KnowledgeSource.collection_id == Collection.id)
        .group_by(Collection.id)
        .order_by(Collection.name)
        .all()
    )
    return [CollectionOut(id=c.id, name=c.name, source_count=n) for c, n in rows]


@router.post("/library/collections", response_model=CollectionOut, status_code=201)
def create_collection(payload: CollectionCreate, db: Session = Depends(get_db)) -> CollectionOut:
    col = Collection(name=payload.name)
    db.add(col)
    db.commit()
    return CollectionOut(id=col.id, name=col.name, source_count=0)


@router.patch("/library/collections/{collection_id}", response_model=CollectionOut)
def rename_collection(
    collection_id: uuid.UUID, payload: CollectionCreate, db: Session = Depends(get_db)
) -> CollectionOut:
    col = db.get(Collection, collection_id)
    if col is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    col.name = payload.name
    db.commit()
    n = db.query(func.count(KnowledgeSource.id)).filter_by(collection_id=col.id).scalar()
    return CollectionOut(id=col.id, name=col.name, source_count=n)


@router.delete("/library/collections/{collection_id}", status_code=204, response_model=None)
def delete_collection(collection_id: uuid.UUID, db: Session = Depends(get_db)) -> Response:
    """Deleting a folder must NEVER delete the tutor's material (spec D7) —
    `KnowledgeSource.collection_id` has `ondelete="SET NULL"` at the DB
    level, so its sources simply become Unfiled; this just deletes the
    `Collection` row itself and lets that FK behavior do the rest."""
    col = db.get(Collection, collection_id)
    if col is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    db.delete(col)
    db.commit()
    return Response(status_code=204)
