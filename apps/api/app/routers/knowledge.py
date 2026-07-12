"""`/knowledge` routes: sources CRUD (+ ingestion), search, and grounded ask.

Ingestion runs synchronously inline in the request (per the plan: "a source is
small" for this PoC) — `POST /sources`/`POST /sources/upload` create the
`KnowledgeSource` row, then call `ingest_source` in the same request/session
before responding, so the response already carries the final status
("ready"/"failed") rather than a client having to poll.

No-auth PoC posture (accepted, not a gap to fix here): this router has no
authentication/authorization — it deploys origin-locked behind Cloudflare for
a single user. Because anyone who can reach it can trigger ingestion, the
compensating controls actually enforced here are (a) `urlsafe.assert_public_
url` blocking SSRF on the URL-ingestion path and (b) the upload/text/`k`/query
bounds below guarding against resource exhaustion — not identity checks.
"""
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brain.ingest import IngestPayload, ingest_source
from app.brain.retrieve import answer as run_answer
from app.brain.retrieve import search as run_search
from app.brain.urlsafe import assert_public_url
from app.db import get_db
from app.models.knowledge import Chunk, KnowledgeSource
from app.schemas.knowledge import (
    AskRequest,
    AskResponse,
    ChunkPreviewOut,
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
MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 30 MiB cap for POST /sources/upload
MAX_TEXT_CHARS = 1_000_000  # cap for kind="text" ingestion via POST /sources


def _to_source_out(source: KnowledgeSource) -> SourceOut:
    return SourceOut.model_validate(source, from_attributes=True)


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
        db, source.id, IngestPayload(kind=payload.kind, text=payload.text, url=payload.url)
    )
    return _to_source_out(source)


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
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"upload exceeds the {MAX_UPLOAD_BYTES}-byte limit",
        )
    source = KnowledgeSource(type="pdf", title=title, domain=domain, language=language)
    db.add(source)
    db.commit()

    ingest_source(db, source.id, IngestPayload(kind="pdf", data=data))
    return _to_source_out(source)


@router.get("/sources", response_model=list[SourceOut])
def list_sources(db: Session = Depends(get_db)) -> list[SourceOut]:
    sources = db.scalars(
        select(KnowledgeSource).order_by(KnowledgeSource.created_at.desc())
    ).all()
    return [_to_source_out(s) for s in sources]


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
        **_to_source_out(source).model_dump(),
        chunks=[ChunkPreviewOut.model_validate(c, from_attributes=True) for c in chunks],
    )


@router.delete("/sources/{source_id}", status_code=204, response_model=None)
def delete_source(source_id: UUID, db: Session = Depends(get_db)) -> None:
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    db.delete(source)  # chunk.source_id has ON DELETE CASCADE at the DB level
    db.commit()


@router.post("/search", response_model=SearchResponse)
def search_endpoint(payload: SearchRequest, db: Session = Depends(get_db)) -> SearchResponse:
    hits = run_search(db, payload.query, k=payload.k, domain=payload.domain, language=payload.language)
    return SearchResponse(hits=[HitOut.model_validate(h, from_attributes=True) for h in hits])


@router.post("/ask", response_model=AskResponse)
def ask_endpoint(payload: AskRequest, db: Session = Depends(get_db)) -> AskResponse:
    result = run_answer(db, payload.query, locale=payload.locale, k=payload.k)
    return AskResponse(
        text=result.text,
        citations=[HitOut.model_validate(h, from_attributes=True) for h in result.citations],
    )
