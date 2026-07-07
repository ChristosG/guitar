"""`/knowledge` routes: sources CRUD (+ ingestion), search, and grounded ask.

Ingestion runs synchronously inline in the request (per the plan: "a source is
small" for this PoC) — `POST /sources`/`POST /sources/upload` create the
`KnowledgeSource` row, then call `ingest_source` in the same request/session
before responding, so the response already carries the final status
("ready"/"failed") rather than a client having to poll.
"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brain.ingest import IngestPayload, ingest_source
from app.brain.retrieve import answer as run_answer
from app.brain.retrieve import search as run_search
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

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

_CHUNK_PREVIEW_LIMIT = 5


def _to_source_out(source: KnowledgeSource) -> SourceOut:
    return SourceOut.model_validate(source, from_attributes=True)


@router.post("/sources", response_model=SourceOut)
def create_source(payload: SourceCreate, db: Session = Depends(get_db)) -> SourceOut:
    source = KnowledgeSource(
        type=payload.kind,
        title=payload.title,
        domain=payload.domain,
        language=payload.language,
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
    data = file.file.read()
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
