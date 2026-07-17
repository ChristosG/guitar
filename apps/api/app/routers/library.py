"""Library: collections, the page-level reader, media, OCR + retry (Plan 9
Task 6).

The reader endpoints are why `Page` exists (spec D1): a citation is only
trustworthy if the tutor can OPEN the page it came from and see the scan.
Ingestion (`POST /sources`, etc.) stays in `routers/knowledge.py`; this
module owns everything downstream of a `KnowledgeSource` already existing —
filing it into a `Collection`, reading its pages, retrying a failed one.

Auth: every route here sits behind the `gt_session` password gate
(`app/auth/middleware.py`), which is a whole-API ASGI middleware rather than a
per-router dependency — so there is nothing to declare in this file. One tutor,
one password; there is still no authorization model, because there is nobody to
authorize against anybody else.
"""
import logging
import os
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.brain.ocr import active_ocr_job, page_counts
from app.brain.repair import repair_pageless_source
from app.config import settings
from app.db import get_db
from app.jobs.canon_compile import enqueue_canon_compile, run_canon_compile_job
from app.jobs.runner import run_ocr_job, run_reingest_job
from app.llm.factory import require_llm_configured, require_ocr_configured
from app.models.generation_job import GenerationJob
from app.models.knowledge import (
    SOURCE_USABLE_STATUSES,
    Collection,
    KnowledgeSource,
    Page,
)
from app.schemas.library import (
    CollectionCreate,
    CollectionOut,
    PageOut,
    PageSummary,
    SourcePatch,
    SourceProgress,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["library"])


def _source_or_404(db: Session, source_id: uuid.UUID) -> KnowledgeSource:
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


# --- OCR ---------------------------------------------------------------

def _enqueue_ocr(
    db: Session,
    background: BackgroundTasks,
    source_id: uuid.UUID,
    *,
    refund_attempts_for: tuple[str, ...] = (),
) -> dict:
    """Enqueue a `GenerationJob(kind="ocr")` + schedule `run_ocr_job` — UNLESS
    one is already in flight for this source, in which case the caller gets that
    job's id back and nothing new is started. THE IN-FLIGHT GUARD (Stage 7.2).

    `refund_attempts_for` is the "I INSIST" reset — the page statuses whose
    `ocr_attempts` this press puts back to 0 (see `reocr_source` and
    `retry_source`, which insist about different sets of pages). IT LIVES INSIDE
    THE GUARD ON PURPOSE. Both callers used to reset AND COMMIT before calling
    here, i.e. before the `SELECT ... FOR UPDATE` — so a press that hit the guard
    and correctly returned `already_running: true` still refunded a RUNNING job's
    page budget on its way to doing nothing, silently weakening the
    `MAX_PAGE_ATTEMPTS` re-billing cap that `reocr_source`'s own docstring cites.
    That is not a rare race: re-read is an 8-hour button, and pressing it again
    mid-run is exactly what the tutor does. A refund is part of STARTING a job, so
    it belongs where the decision to start one is made, in that decision's
    transaction and under that decision's lock.

    Both producers (`POST .../ocr` and the pdf branch of `POST .../retry`) go
    through here. Before this, each of them enqueued unconditionally: two clicks
    = two jobs = two `ocr_source` runs on the same book, each doing a
    delete-then-insert of the same page's chunks, racing. And the tutor DID click
    twice, because a reload during the 9-minute OCR used to paint his book red
    with a Retry button (the progress lived in tab-local React state).

    `SELECT ... FOR UPDATE` on the source row, not a bare read: FastAPI runs these
    sync handlers in a threadpool, so two clicks 30ms apart are genuinely
    concurrent and a check-then-insert without a lock is a check-then-insert with
    a race. Locking the SOURCE (which every OCR enqueue for it must also lock)
    serializes them into "first one wins, second one observes the first" — an
    advisory lock on a row nobody else contends for, held for microseconds.

    The job row is committed BEFORE `background.add_task` — NOT left to the
    background task, which Starlette runs AFTER the response is sent — so the
    runner (which opens its own session) is guaranteed to find the row, an
    immediate `GET /jobs/{job_id}` poll sees it, and the guard above can see it.
    """
    db.query(KnowledgeSource).filter(KnowledgeSource.id == source_id).with_for_update().one()

    existing = active_ocr_job(db, source_id)
    if existing is not None:
        db.commit()                                   # release the row lock
        log.info("ocr: job %s already in flight for source=%s — not enqueuing a second",
                 existing.id, source_id)
        return {"job_id": str(existing.id), "already_running": True}

    if refund_attempts_for:
        db.query(Page).filter(
            Page.source_id == source_id,
            Page.status.in_(refund_attempts_for),
        ).update({Page.ocr_attempts: 0}, synchronize_session=False)

    job = GenerationJob(kind="ocr", status="pending", params={"source_id": str(source_id)})
    db.add(job)
    db.commit()                       # the refund and the job row it is for, together
    background.add_task(run_ocr_job, job.id)
    return {"job_id": str(job.id), "already_running": False}


@router.post(
    "/knowledge/sources/{source_id}/ocr",
    status_code=202,
    # OCR is 77 vision calls. Without a key that is 77 "failed" pages and a book
    # the tutor is told is unreadable — see `require_ocr_configured`, which
    # resolves the provider that will do the READING, not chat's.
    dependencies=[Depends(require_ocr_configured)],
)
def start_ocr(
    source_id: uuid.UUID, background: BackgroundTasks, db: Session = Depends(get_db)
) -> dict:
    """Start (or re-run) OCR on a source's unread pages. Poll `GET
    /knowledge/sources/{id}/progress` — or `GET /jobs/{job_id}` — for the outcome.

    Idempotent under a double-click (`_enqueue_ocr`), and safe to call on a
    HEALTHY source: `ocr_source` only picks up pages that are not already `ready`
    (and have attempts left), so this re-reads the pages that failed and leaves
    the 71 good ones alone. That is what makes "Re-run OCR" an affordance the
    Library can offer on any PDF rather than only on a broken one.
    """
    _source_or_404(db, source_id)
    return _enqueue_ocr(db, background, source_id)


@router.post(
    "/knowledge/sources/{source_id}/reocr",
    status_code=202,
    dependencies=[Depends(require_ocr_configured)],
)
def reocr_source(
    source_id: uuid.UUID, background: BackgroundTasks, db: Session = Depends(get_db)
) -> dict:
    """Re-read this book with the vision model. THE BUTTON, and it resumes.

    WHY THIS IS NOT AUTOMATIC ON UPLOAD, and must never become so. At ~40s per
    page through `claude -p`, the tutor's 888 pages is 8-12 hours of model time
    and a repeated slice of a 5-hour subscription cap — a cap shared with every
    other thing this app does for him, including the curriculum draft that is the
    actual product. Wasted or duplicated LLM spend is this plan's top severity
    class. So the cost is spent when he asks for it, and only then.

    AND IT RESUMES, which is what makes an 8-hour run survivable across the
    several sittings it will actually take. `ocr_source` picks up only pages that
    are not `ready` (`PICKUP_STATUSES`), so every press after the first costs
    exactly the pages still unread — press it, lose the connection, hit the cap,
    come back tomorrow, press it again. The 429 rule inside `ocr_source` parks
    the run and refunds the attempt rather than burning 800 pages' budgets
    against a rate-limit window.

    Behind the same `SELECT ... FOR UPDATE` in-flight guard as `POST .../ocr`
    (`_enqueue_ocr`) — not optional here of all places: this is the button the
    tutor presses again mid-run precisely BECAUSE it is a long run, and two jobs
    reading the same book race each other through the same page's
    delete-then-insert of chunks.

    409 for a non-PDF: url/text/note sources have no scan to re-read, and a
    silent no-op that returned a job id would look like something happened.
    """
    source = _source_or_404(db, source_id)
    if source.type != "pdf":
        raise HTTPException(
            status_code=409,
            detail="nothing to re-read: only a PDF has page scans a model can read",
        )

    # AN EXPLICIT PRESS MEANS "I INSIST" — the same refund `retry_source` asks
    # for, widened to every page still waiting to be read rather than only the
    # ones that already failed. Without it, a book whose pages burned
    # MAX_PAGE_ATTEMPTS inside one rate-limit window is permanently un-re-readable
    # with this very button silently reading zero pages.
    #
    # Handed to `_enqueue_ocr` rather than done here, because a press that starts
    # no job must refund nothing — see that function's docstring.
    #
    # `empty` IS DELIBERATELY NOT IN THE LIST. A real 77-page scan has genuinely
    # blank pages, and the quality gate records a model's "no visible text"
    # narration as `empty` on purpose — it is a true fact about the book, not a
    # failure to read it. Resetting those on every press would re-bill the book's
    # blank pages to a paid model on every press, forever, which is the exact
    # re-billing `MAX_PAGE_ATTEMPTS` exists to prevent. A `ready` page is not here
    # either, for a stronger reason: it is already read, and putting its attempts
    # back would only ever cost money to re-derive text we have.
    return _enqueue_ocr(
        db, background, source_id,
        refund_attempts_for=("pending", "failed", "ocr_running"),
    )


@router.get("/knowledge/sources/{source_id}/progress", response_model=SourceProgress)
def source_progress(source_id: uuid.UUID, db: Session = Depends(get_db)) -> SourceProgress:
    """SERVER-COMPUTED OCR progress — the whole point of Stage 7.2.

    Progress used to live ONLY in the React state of the tab that started the job
    (`library/page.tsx`'s `ocrProgress`/`watchOcr`). So a reload at page 30 of 77
    — during a NINE MINUTE OCR — lost it, and the row fell back to the source's
    at-rest status, which is still `empty`: the tutor's book showed RED, "nothing
    was read", with a Retry button, WHILE IT WAS BEING READ. Pressing it started a
    second job racing the first.

    Everything here is derived from durable rows (`Page.status` + the
    `GenerationJob`), so it reads the same in a fresh tab, after a reload, on his
    phone, and tomorrow.
    """
    _source_or_404(db, source_id)
    counts = page_counts(db, [source_id])[source_id]
    job = active_ocr_job(db, source_id)
    return SourceProgress(
        source_id=source_id,
        total=counts.total,
        ready=counts.ready,
        failed=counts.failed,
        empty=counts.empty,
        pending=counts.pending,
        # `resolved + 1` is the page the job is on RIGHT NOW; clamped so the last
        # page's own completion doesn't read "page 78 of 77".
        current_page=min(counts.resolved + 1, counts.total) if job else None,
        active=job is not None,
        job_id=job.id if job else None,
    )


# --- Canon compile (Part B, Task C6) ----------------------------------------

@router.post(
    "/knowledge/sources/{source_id}/compile",
    status_code=202,
    # Compiling reads the whole book through the CHAT provider (not OCR's) — so it
    # gates on that key. Without it the tutor would get a `canon_compile` job that
    # fails "auth" a second later; a clean 4xx here is honest instead.
    dependencies=[Depends(require_llm_configured)],
)
def compile_source(
    source_id: uuid.UUID, background: BackgroundTasks, db: Session = Depends(get_db)
) -> dict:
    """Read this book into the concept canon. Poll `GET /jobs/{job_id}` for the
    outcome, exactly like OCR.

    THE MONEY GUARD, twice. Pressing this on an already-compiled book starts NO
    job and returns `already_compiled: true` — an app update must not re-spend the
    tutor's subscription re-reading a book it already read. Pressing it twice on an
    un-compiled book returns the SAME job the second time (`already_running: true`,
    the `SELECT ... FOR UPDATE` guard in `enqueue_canon_compile`) rather than a
    second reading racing the first through the ledger.

    Not automatic here: OCR completion auto-compiles (`runner.run_ocr_job`), so
    this endpoint is the explicit "compile it now / retry a failed compile" button.
    """
    _source_or_404(db, source_id)
    job_id, status = enqueue_canon_compile(db, source_id)
    if status == "already_compiled":
        return {"already_compiled": True}
    if status == "enqueued":
        background.add_task(run_canon_compile_job, job_id)
    return {"job_id": str(job_id), "already_running": status == "already_running"}


# --- Reader --------------------------------------------------------------

def _ensure_pages_exist(db: Session, source: KnowledgeSource) -> None:
    """Self-heal a `status="ready"` source that has zero `Page` rows —
    the exact live bug on "Guitar Tone & Gear — Course Spine" (ingested in
    Plan 7, before the Page model existed, so it never got one). Per spec
    D6, "ready" means there IS content; a ready source the Reader can't
    open is the same class of lie D6 already exists to kill. See
    `app/brain/repair.py`'s module docstring for the full story and why
    this can no longer happen to anything ingested since Plan 9 Task 5.

    Deliberately scoped tight — only fires for exactly this shape (a USABLE
    status AND zero Pages) — so it never touches a source that is legitimately
    still ingesting/failed/empty, or a merely out-of-range page number on an
    otherwise-healthy source. "Usable" is `ready` OR `partial` (Stage 7.2): a
    book with 6 unreadable pages is still a book, and the Reader must open it —
    gating this on the string "ready" alone would have made `partial` mean
    "unopenable", which is not what it means.

    Runs inline on the read path (a GET), not queued as a background job:
    the repair is a bounded, idempotent, one-time cost (paginate_source
    always replaces rather than duplicates — Plan 9 Task 3), and the whole
    point is that the tutor's very first click must not dead-end. Any
    failure (e.g. the embed server is briefly down) is caught and logged,
    never raised — the caller re-checks afterward and still gets a clean,
    honest 404 rather than a 500 if the heal didn't take.
    """
    if source.status not in SOURCE_USABLE_STATUSES:
        return
    if db.query(func.count(Page.id)).filter_by(source_id=source.id).scalar() > 0:
        return
    try:
        repair_pageless_source(db, source)
    except Exception:
        log.exception("pages self-heal failed for source_id=%s", source.id)


@router.get("/knowledge/sources/{source_id}/pages", response_model=list[PageSummary])
def list_pages(source_id: uuid.UUID, db: Session = Depends(get_db)) -> list[PageSummary]:
    source = _source_or_404(db, source_id)
    _ensure_pages_exist(db, source)
    pages = (
        db.query(Page).filter_by(source_id=source_id).order_by(Page.page_no).all()
    )
    return [PageSummary(page_no=p.page_no, status=p.status) for p in pages]


@router.get("/knowledge/sources/{source_id}/pages/{page_no}", response_model=PageOut)
def get_page(source_id: uuid.UUID, page_no: int, db: Session = Depends(get_db)) -> PageOut:
    source = _source_or_404(db, source_id)
    page = db.query(Page).filter_by(source_id=source_id, page_no=page_no).one_or_none()
    if page is None:
        _ensure_pages_exist(db, source)
        page = db.query(Page).filter_by(source_id=source_id, page_no=page_no).one_or_none()
    if page is None:
        # Same status code either way (existing behavior pinned by
        # test_missing_page_is_404 — a plain out-of-range page number stays
        # a plain 404), but an honest, more actionable detail for the one
        # case self-heal above could not fix: a source the Library says is
        # READY that still, somehow, has no pages at all.
        detail = (
            "This source is marked ready but has no pages — try re-indexing it."
            if source.status in SOURCE_USABLE_STATUSES
            else "Page not found"
        )
        raise HTTPException(status_code=404, detail=detail)
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
    the caller, but both must fail closed rather than crash.

    `Cache-Control: private, no-store` IS THE LINE THAT MAKES THE PASSWORD GATE
    REAL (Plan 13 Task 3.3). This app is served through Cloudflare, and
    Cloudflare edge-caches `.jpg` BY EXTENSION, by default, with no regard for
    the cookie the request carried. Without this header, the first authenticated
    fetch of a page scan populates the edge, and every subsequent request for
    that URL — from anyone, with no cookie at all — is served the tutor's
    scanned book straight off the CDN, never reaching this handler and never
    reaching the auth middleware. The gate would be theatre. One line, and it
    lives here because `FileResponse` sets no cache headers of its own."""
    page = db.get(Page, page_id)
    if page is None or not page.image_path:
        raise HTTPException(status_code=404, detail="No scan for this page")
    path = os.path.join(settings.media_dir, page.image_path)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Scan missing on disk")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, no-store"},
    )


# --- Retry (controller decision, Task 6) ------------------------------------

@router.post(
    "/knowledge/sources/{source_id}/retry",
    status_code=202,
    dependencies=[Depends(require_ocr_configured)],
)
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
        # AN EXPLICIT RETRY MEANS "I INSIST". Pages that exhausted their
        # per-run attempt budget (MAX_PAGE_ATTEMPTS) are excluded from every
        # automatic pickup — without this refund, a book whose pages burned
        # their attempts during a rate-limit window was permanently
        # un-OCR-able, with this very button silently processing 0 pages.
        # `_enqueue_ocr` applies it only if this press actually starts a job.
        return _enqueue_ocr(db, background, source_id, refund_attempts_for=("failed",))

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
    and mean different things for a nullable FK.

    Review fix: an unknown (or stale — collection deleted in one tab, moved-
    to in another) `collection_id` used to be assigned straight onto
    `source.collection_id` with no existence check, so `db.commit()` blew up
    as a raw FK IntegrityError -> 500. Look the collection up and 404
    cleanly BEFORE assigning, same existence-check-before-assignment
    precedent as `students.py::upsert_student_progress` and
    `curriculum.py::update_block`. `collection_id: None` stays valid (it
    means "Unfiled") — only a non-null, unknown id 404s.
    """
    source = _source_or_404(db, source_id)
    if payload.title is not None:
        source.title = payload.title
    if "collection_id" in payload.model_fields_set:
        if payload.collection_id is not None and db.get(Collection, payload.collection_id) is None:
            raise HTTPException(status_code=404, detail="Collection not found")
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
