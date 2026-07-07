"""Pydantic request/response models for the Knowledge Brain API (`routers/knowledge.py`).

Kept separate from the SQLAlchemy models (`app.models.knowledge`) and the
`app.brain.retrieve` dataclasses (`Hit`/`Answer`) per the plan's file layout —
this module is only the HTTP boundary's shape; it holds no DB/retrieval logic.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


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


class ChunkPreviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    text: str
    section_path: str | None
    page: int | None


class SourceDetailOut(SourceOut):
    chunks: list[ChunkPreviewOut] = []


class SearchRequest(BaseModel):
    query: str
    k: int = 8
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
    score: float


class SearchResponse(BaseModel):
    hits: list[HitOut]


class AskRequest(BaseModel):
    query: str
    locale: str
    k: int = 8


class AskResponse(BaseModel):
    text: str
    citations: list[HitOut]
