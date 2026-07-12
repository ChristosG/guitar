"""Pydantic request/response models for the Knowledge Brain API (`routers/knowledge.py`).

Kept separate from the SQLAlchemy models (`app.models.knowledge`) and the
`app.brain.retrieve` dataclasses (`Hit`/`Answer`) per the plan's file layout —
this module is only the HTTP boundary's shape; it holds no DB/retrieval logic.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Shared resource-exhaustion bounds for the search/ask request bodies (the
# `/sources` text-length cap and `/sources/upload` byte cap live in
# `routers/knowledge.py` instead, since they need a non-422 status code —
# see that module's docstring).
_MAX_QUERY_CHARS = 2000
_MIN_K = 1
_MAX_K = 50


class SourceCreate(BaseModel):
    kind: Literal["text", "url"]  # "pdf" goes through POST /sources/upload instead
    title: str
    domain: str | None = None
    language: str | None = None
    text: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _text_or_url_matches_kind(self) -> "SourceCreate":
        if self.kind == "text" and not (self.text and self.text.strip()):
            raise ValueError("kind='text' requires a non-empty 'text'")
        if self.kind == "url" and not (self.url and self.url.strip()):
            raise ValueError("kind='url' requires a non-empty 'url'")
        return self


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    type: str
    status: str
    domain: str | None
    language: str | None
    char_count: int | None
    error: str | None
    created_at: datetime
    # Review fix: `KnowledgeSource.collection_id` exists on the ORM model
    # (`app/models/knowledge.py`) but was never declared here, so
    # `model_validate(source, from_attributes=True)` silently dropped it —
    # `GET /knowledge/sources` never told the Library UI which `Collection` a
    # source was filed under (everything looked "Unfiled", and a PATCH that
    # filed a source appeared to revert on the next refresh). `SourceDetailOut`
    # inherits this field for free.
    collection_id: UUID | None = None


class ChunkPreviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    text: str
    section_path: str | None
    # Chunk.page (int) was replaced by Chunk.page_id (FK to Page) in Plan 9
    # Task 1; the ORM object no longer has a bare `.page` attribute at all.
    # Defaulting to None lets `from_attributes` validation fall back instead
    # of raising on the missing attribute — resolving a real page NUMBER via
    # page_id is later Plan 9 work.
    page: int | None = None


class SourceDetailOut(SourceOut):
    chunks: list[ChunkPreviewOut] = []


class SearchRequest(BaseModel):
    query: str = Field(max_length=_MAX_QUERY_CHARS)
    k: int = Field(default=8, ge=_MIN_K, le=_MAX_K)
    domain: str | None = None
    language: str | None = None


class HitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_id: UUID
    source_id: UUID
    source_title: str
    text: str
    section_path: str | None
    page: int | None
    # Page.id (FK) — Task 10 wired retrieve.search() to resolve this via
    # Chunk.page_id, so a citation's scan is fetchable at
    # GET /media/pages/{page_id}.jpg (see routers/library.py) instead of
    # merely claimed. None only for chunks from sources ingested before
    # Plan 9 Task 1, which predate Page rows entirely.
    page_id: UUID | None = None
    score: float


class SearchResponse(BaseModel):
    hits: list[HitOut]


class AskRequest(BaseModel):
    query: str = Field(max_length=_MAX_QUERY_CHARS)
    locale: str
    k: int = Field(default=8, ge=_MIN_K, le=_MAX_K)


class AskResponse(BaseModel):
    text: str
    citations: list[HitOut]
