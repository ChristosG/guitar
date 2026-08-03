"""`/knowledge` routes: sources CRUD (+ ingestion), search, and grounded ask.

Ingestion runs synchronously inline in the request (per the plan: "a source is
small" for this PoC) — `POST /sources`/`POST /sources/upload` create the
`KnowledgeSource` row, then call `ingest_source` in the same request/session
before responding, so the response already carries the final status
("ready"/"failed") rather than a client having to poll.

Auth: every route here sits behind the `gt_session` password gate
(`app/auth/middleware.py`) — a whole-API ASGI middleware, not a per-router
dependency, so there is nothing to declare in this file. One tutor, one
password; there is still no authorization model, because there is nobody to
authorize against anybody else.
"""
import hashlib
import logging
import re
from typing import Annotated
from urllib.parse import unquote, urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brain.ingest import IngestPayload, ingest_source
from app.brain.media import purge_source_media
from app.brain.ocr import PageCounts, active_ocr_jobs, latest_failed_ocr_jobs, page_counts
from app.brain.reembed import reembed_all
from app.brain.retrieve import answer as run_answer
from app.brain.retrieve import search as run_search
from app.brain.urlsafe import assert_public_url
from app.canon.search import search_concepts as run_concept_search
from app.db import get_db
from app.jobs.canon_compile import active_compile_jobs
from app.models.canon import BookCompile
from app.models.knowledge import Chunk, KnowledgeSource
from app.schemas.knowledge import (
    AskRequest,
    AskResponse,
    BulkSourceCreate,
    BulkSourceResponse,
    BulkSourceResultOut,
    ChunkPreviewOut,
    CompileStatusOut,
    ConceptHitOut,
    ConceptSearchRequest,
    ConceptSearchResponse,
    HitOut,
    SearchRequest,
    SearchResponse,
    SourceCreate,
    SourceDetailOut,
    SourceOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

_CHUNK_PREVIEW_LIMIT = 5

# Resource-exhaustion caps (Fix 2). `k`/query-length bounds live on the
# request schemas instead (`schemas/knowledge.py`) since Pydantic gives those
# a 422; these two need a distinct 413 ("payload too large"), so they're
# plain in-router checks instead of Field constraints.
# 60 MiB. The old 30 MiB cap left ~14% headroom over the tutor's LARGEST real
# book (Gallagher, 26 MB) — the very next scanned book he buys would have
# bounced. Scans of long books routinely run 40-50 MB; ingest cost scales with
# page count, not file size, so the cap only needs to keep out the absurd.
MAX_UPLOAD_BYTES = 60 * 1024 * 1024
MAX_TEXT_CHARS = 1_000_000  # cap for kind="text" ingestion via POST /sources


def _to_source_out(
    source: KnowledgeSource,
    counts: PageCounts | None = None,
    ocr_active: bool = False,
    compile: BookCompile | None = None,
    compile_active: bool = False,
    last_failed_ocr=None,
) -> SourceOut:
    """`counts`/`ocr_active`/`compile` are passed in, never queried here:
    `list_sources` resolves them for the WHOLE list in a fixed number of queries
    (see `_decorate`), and doing it per row would turn one list request into
    O(N) round trips."""
    out = SourceOut.model_validate(source, from_attributes=True)
    c = counts or PageCounts()
    return out.model_copy(update={
        "pages_total": c.total,
        "pages_ready": c.ready,
        "pages_failed": c.failed,
        "pages_pending": c.pending,
        "ocr_active": ocr_active,
        # `None` stays `None` (never compiled); a real row becomes the canon
        # compile state the Library row shows beside OCR status (Part B, C7).
        "compile": CompileStatusOut.model_validate(compile, from_attributes=True)
        if compile is not None else None,
        # `ocr_active`'s analogue for compiles: the in-flight JOB, because
        # `book_compile.status` flips to "running" seconds late (see
        # `jobs/canon_compile.active_compile_jobs`).
        "compile_active": compile_active,
        # The LATEST ocr job's failure, when there is one — what lets the row
        # say "your key was rejected" instead of a healthy sky-blue "Continue
        # reading" after an auth park (`latest_failed_ocr_jobs`' docstring).
        "last_ocr_error_kind": last_failed_ocr.error_kind if last_failed_ocr else None,
        "last_ocr_error": last_failed_ocr.error if last_failed_ocr else None,
    })


def _compiles(db: Session, ids: list[UUID]) -> dict[UUID, BookCompile]:
    """`book_compile` rows for a list of sources, keyed by source_id, in one query
    — the canon-compile analogue of `page_counts`/`active_ocr_jobs`. Only sources
    that have ever been compiled appear; the rest map to nothing (= never
    compiled)."""
    if not ids:
        return {}
    rows = db.scalars(
        select(BookCompile).where(BookCompile.source_id.in_(ids))
    ).all()
    return {r.source_id: r for r in rows}


def _decorate(db: Session, sources: list[KnowledgeSource]) -> list[SourceOut]:
    """Attach page counts, in-flight-OCR AND canon-compile state to a list of
    sources — the facts that let the Library render an honest row (Stage 7.2), a
    LIVE one after a reload mid-OCR, and (Part B, C7) whether each book has been
    read into the concept canon. A fixed number of queries total, regardless of
    list length."""
    ids = [s.id for s in sources]
    counts = page_counts(db, ids)
    active = active_ocr_jobs(db, ids)
    compiles = _compiles(db, ids)
    compiling = active_compile_jobs(db, ids)
    failed_ocr = latest_failed_ocr_jobs(db, ids)
    return [
        _to_source_out(
            s, counts.get(s.id), str(s.id) in active, compiles.get(s.id),
            str(s.id) in compiling, failed_ocr.get(str(s.id)),
        )
        for s in sources
    ]


@router.post("/sources", response_model=SourceOut)
def create_source(payload: SourceCreate, db: Session = Depends(get_db)) -> SourceOut:
    # Both guards run BEFORE the source row is created/ingested — no DB
    # write and no fetch happens for a rejected payload.
    if payload.kind == "url":
        try:
            assert_public_url(payload.url)
        except ValueError as e:
            # Log the detailed reason (which host, which resolved IP) server-side
            # only — returning it verbatim to the caller would be a mild
            # internal-recon oracle (review pass 2). The client gets a generic,
            # non-revealing detail instead.
            log.warning("rejected kind='url' source (SSRF guard): %s", e)
            raise HTTPException(status_code=400, detail="URL not allowed") from e
    elif payload.kind == "text" and len(payload.text) > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"text exceeds the {MAX_TEXT_CHARS}-character limit",
        )

    source = KnowledgeSource(
        type=payload.kind,
        title=payload.title,
        domain=payload.domain,
        language=payload.language,
        url=payload.url,
    )
    db.add(source)
    db.commit()  # assigns source.id; durable row before ingest_source's own commits

    ingest_source(
        db, source.id,
        IngestPayload(
            kind=payload.kind, text=payload.text, url=payload.url,
            crawl_pages=payload.crawl_pages,
        ),
    )
    return _decorate(db, [source])[0]


# `KnowledgeSource.title` is `String(400)` at the DB level — a derived title
# (below) must respect that cap same as a hand-typed one would.
_TITLE_MAX_CHARS = 400


def _title_for_url(url: str) -> str:
    """A reasonable default title for a bulk-added URL: no per-item title
    field exists in a pasted list of raw URLs (unlike `POST /sources`, which
    always gets one from the caller), so derive a human-legible one instead
    of just repeating the raw URL back as its own title.

    Plan 12 follow-up: this used to be the raw `netloc + path`
    ("en.wikipedia.org/wiki/Humbucker") — indistinguishable from a URL or a
    server-log line, and confusing next to the 3 dead pre-Plan-9 Wikipedia
    rows already in the sources list. Instead, derive a title from the
    URL's last path segment: decode percent-encoding, turn `-`/`_` into
    spaces, and title-case it ONLY if the raw slug was all-lowercase (a
    slug already carrying meaningful casing — e.g. a wiki article's own
    proper-noun title — is left alone rather than mangled), then append the
    domain for context/provenance. Falls back to the bare domain (or the
    raw URL, if even that's empty) when the URL has no path segments at
    all, e.g. `https://example.com/`.
    """
    parts = urlsplit(url)
    domain = parts.netloc
    segments = [seg for seg in parts.path.split("/") if seg]
    if not segments:
        label = domain or url
    else:
        slug = unquote(segments[-1])
        slug = re.sub(r"[-_]+", " ", slug).strip()
        if slug and slug == slug.lower():
            slug = slug.title()
        label = f"{slug} — {domain}" if slug and domain else (slug or domain or url)
    return label[:_TITLE_MAX_CHARS]


@router.post("/sources/bulk", response_model=BulkSourceResponse)
def bulk_create_sources(payload: BulkSourceCreate, db: Session = Depends(get_db)) -> BulkSourceResponse:
    """Create + ingest one `KnowledgeSource` per URL, honestly.

    Per URL: the SSRF guard runs first, exactly like `POST /sources` — a
    disallowed host is `"rejected"` and never gets a DB row or a fetch. A URL
    that passes the guard always gets a row and an ingest attempt; its
    reported `status` is whatever `ingest_source` actually recorded
    (`"ready"`/`"empty"`/`"failed"`) — never coerced to a green status just
    because the request as a whole "succeeded" (spec D6: an ingest that
    pulled 0 characters is `"empty"`, not `"ready"`, and this endpoint must
    not paper over that at the batch level either).

    One URL's failure does not abort the batch — each is independent, and
    the response is always 200 with a per-URL breakdown; there is no
    aggregate failure status for the request itself.
    """
    results: list[BulkSourceResultOut] = []
    for raw_url in payload.urls:
        url = raw_url.strip()
        if not url:
            results.append(BulkSourceResultOut(url=raw_url, status="rejected", error="empty URL"))
            continue

        try:
            assert_public_url(url)
        except ValueError as e:
            # Same posture as create_source: log the specific reason
            # server-side only, never echo it back (mild internal-recon
            # oracle otherwise).
            log.warning("bulk: rejected kind='url' source (SSRF guard): %s", e)
            results.append(BulkSourceResultOut(url=url, status="rejected", error="URL not allowed"))
            continue

        source = KnowledgeSource(
            type="url",
            title=_title_for_url(url),
            domain=payload.domain,
            language=payload.language,
            url=url,
        )
        db.add(source)
        db.commit()  # assigns source.id; durable row before ingest_source's own commits

        ingest_source(db, source.id, IngestPayload(kind="url", url=url))

        results.append(
            BulkSourceResultOut(
                url=url,
                status=source.status,
                source_id=source.id,
                title=source.title,
                char_count=source.char_count,
                error=source.error,
            )
        )
    return BulkSourceResponse(results=results)


@router.post("/sources/upload", response_model=SourceOut)
def upload_source(
    title: Annotated[str, Form()],
    domain: Annotated[str | None, Form()] = None,
    language: Annotated[str | None, Form()] = None,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> SourceOut:
    # Bounded read: stop at one byte past the cap rather than reading an
    # arbitrarily large upload fully into memory before checking its size.
    # The `{"code", "message"}` detail shape below is the auth/settings
    # routers' convention — `parseError` (api.ts) lifts `code` out, and the
    # add-source dialog renders its own LOCALIZED sentence for a code it
    # knows instead of parroting English at a Greek tutor.
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "upload_too_large",
                "message": f"the file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
            },
        )

    # Magic bytes, not the picker's word for it. The route hard-codes
    # `type="pdf"`, so a JPEG chosen through "All Files" used to sail in,
    # explode inside `fitz.open`, and leave a red row whose error was MuPDF's
    # own English — with a Retry button that ran an OCR job over zero pages.
    # (The spec allows junk before the header; 1024 bytes is the same window
    # real readers scan.)
    if b"%PDF-" not in data[:1024]:
        raise HTTPException(
            status_code=415,
            detail={"code": "upload_not_pdf", "message": "this file is not a PDF"},
        )

    # THE DUPLICATE GUARD. Two ids for the same bytes become two compiles, and
    # canon consensus/divergence — keyed on distinct source ids — then shows
    # the same book "disagreeing with itself" as a headline DIVERGENCE, with
    # `coverage: 2/2 books` a lie. Re-uploading after a bad read is a real
    # workflow, so the message names the existing row: delete or displace it
    # first, and the upload goes through.
    digest = hashlib.sha256(data).hexdigest()
    existing = db.scalars(
        select(KnowledgeSource).where(KnowledgeSource.content_sha256 == digest)
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "upload_duplicate",
                "message": f"this exact file is already in the library as “{existing.title}”",
                "existing_title": existing.title,
            },
        )

    source = KnowledgeSource(
        type="pdf", title=title, domain=domain, language=language, content_sha256=digest,
    )
    db.add(source)
    db.commit()

    ingest_source(db, source.id, IngestPayload(kind="pdf", data=data))
    return _decorate(db, [source])[0]


@router.get("/sources", response_model=list[SourceOut])
def list_sources(db: Session = Depends(get_db)) -> list[SourceOut]:
    sources = list(db.scalars(
        select(KnowledgeSource).order_by(KnowledgeSource.created_at.desc())
    ).all())
    return _decorate(db, sources)


@router.get("/sources/{source_id}", response_model=SourceDetailOut)
def get_source(source_id: UUID, db: Session = Depends(get_db)) -> SourceDetailOut:
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")

    chunks = db.scalars(
        select(Chunk)
        .where(Chunk.source_id == source_id)
        .order_by(Chunk.created_at)
        .limit(_CHUNK_PREVIEW_LIMIT)
    ).all()
    return SourceDetailOut(
        **_decorate(db, [source])[0].model_dump(),
        chunks=[ChunkPreviewOut.model_validate(c, from_attributes=True) for c in chunks],
    )


@router.delete("/sources/{source_id}", status_code=204, response_model=None)
def delete_source(source_id: UUID, db: Session = Depends(get_db)) -> None:
    """Delete the row AND the scans it rendered (Stage 7.3).

    The scans were leaked, permanently, by every DELETE this app has ever
    served: `Page`/`Chunk` cascade at the DB level, and nothing anywhere called
    `os.remove` — so the tutor's 77-page book left ~25MB of JPEGs behind under a
    directory named after a source id that no longer existed, and re-uploading it
    (which is exactly what you do after a bad OCR run) leaked another 25MB.

    Ordering is deliberate: COMMIT FIRST, delete files after. The database is the
    truth about what the tutor has; a file we failed to unlink is a wasted
    megabyte, but a failed unlink raised BEFORE the commit would abort a deletion
    the tutor asked for and make the row look undeletable. `purge_source_media`
    is best-effort and never raises (see `app/brain/media.py`), and the boot-time
    orphan sweep in `app.main`'s lifespan is the backstop for whatever it missed.
    """
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    db.delete(source)  # chunk.source_id has ON DELETE CASCADE at the DB level
    db.commit()
    purge_source_media(source_id)


@router.post("/reindex")
def reindex_endpoint(db: Session = Depends(get_db)) -> dict:
    """Re-embed every chunk in place, from `chunk.text` (Plan 13, Stage 4.2).

    The same work `scripts/reembed.py` does, reachable without a shell — because
    the end state of this app is a local bundle on the tutor's iMac, where "run
    this script inside the container" is not an instruction anybody is going to
    follow. The next embedding-model change is then a button, not a support call.

    Synchronous, like ingestion (see the module docstring): the whole 408-chunk
    corpus re-embeds on the local CPU in well under a minute. It becomes a
    `GenerationJob` the day the library is big enough for that to be false.
    """
    return reembed_all(db)


@router.post("/search", response_model=SearchResponse)
def search_endpoint(payload: SearchRequest, db: Session = Depends(get_db)) -> SearchResponse:
    hits = run_search(db, payload.query, k=payload.k, source_ids=payload.source_ids)
    return SearchResponse(hits=[HitOut.model_validate(h, from_attributes=True) for h in hits])


@router.post("/concepts/search", response_model=ConceptSearchResponse)
def search_concepts_endpoint(
    payload: ConceptSearchRequest, db: Session = Depends(get_db),
) -> ConceptSearchResponse:
    """Search the concept canon — a searchable KIND alongside pages/chunks (C8).

    Distinct from `POST /knowledge/search` (which returns chunks from individual
    books): a concept hit is the CROSS-BOOK picture of one idea — where the books
    agree, and where they DISAGREE, each position carrying its own citations. Its
    citations deep-link into the Reader via `source_id` + a page number, the same
    `GET /knowledge/sources/{source_id}/pages/{page_no}` a chunk citation uses.
    """
    hits = run_concept_search(db, payload.query, k=payload.k)
    return ConceptSearchResponse(
        hits=[ConceptHitOut.model_validate(h, from_attributes=True) for h in hits]
    )


@router.post("/ask", response_model=AskResponse)
def ask_endpoint(payload: AskRequest, db: Session = Depends(get_db)) -> AskResponse:
    result = run_answer(db, payload.query, locale=payload.locale, k=payload.k)
    return AskResponse(
        text=result.text,
        citations=[HitOut.model_validate(h, from_attributes=True) for h in result.citations],
    )
