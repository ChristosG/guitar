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

# Bulk URL ingestion (Plan 12 Task 1 — "paste many URLs" box in the Library).
# A resource-exhaustion bound on the request itself, same spirit as k/query
# above: each URL runs a full ingest (fetch -> extract -> chunk -> embed)
# synchronously in this one request, so an unbounded list is an easy way to
# make one HTTP call do an enormous amount of work.
_MAX_BULK_URLS = 20


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

    # Page-level truth, carried on the list row itself (Stage 7.2). NOT columns —
    # derived per request from `Page.status` (`app.brain.ocr.page_counts`, one
    # grouped query for the whole list). They exist so the Library can render
    # "71 of 77 pages read · 6 failed" and, crucially, so a FRESHLY LOADED tab
    # knows OCR is running: `ocr_active` is an in-flight `GenerationJob`, so a
    # hard reload mid-OCR shows "reading page 30 of 77" instead of the source's
    # at-rest status ("empty" — RED, with a Retry button that started a second
    # racing job). Zeroes for a source that has no pages, which is no source
    # ingested since Plan 9.
    pages_total: int = 0
    pages_ready: int = 0
    pages_failed: int = 0
    pages_pending: int = 0
    ocr_active: bool = False


class BulkSourceCreate(BaseModel):
    """`POST /knowledge/sources/bulk` — the Library's "paste many URLs" box.

    Deliberately URL-only (unlike `SourceCreate`, which also handles
    kind="text"): pasting a batch of URLs is the actual use case (the
    tutor's 8 real course links), and a bulk "text" ingest has no obvious
    per-item title/boundary to infer from a list of raw strings.
    """

    urls: list[str] = Field(min_length=1, max_length=_MAX_BULK_URLS)
    domain: str | None = None
    language: str | None = None


class BulkSourceResultOut(BaseModel):
    """One URL's honest outcome. `status` is always a real `KnowledgeSource`
    status (`ready`/`empty`/`failed`) once a row was created, or the
    request-level `"rejected"` when the URL never got that far (SSRF guard
    or an empty string) — never a green lie about a URL that yielded nothing
    or was never fetched at all (spec D6).
    """

    url: str
    status: str
    source_id: UUID | None = None
    title: str | None = None
    char_count: int | None = None
    error: str | None = None


class BulkSourceResponse(BaseModel):
    results: list[BulkSourceResultOut]


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
    # `domain` and `language` are GONE (Plan 13, Stage 4.4). Both were filters on
    # `KnowledgeSource` columns that 12 of the 16 real sources leave NULL, so both
    # could silently empty the corpus — and `language` was worse still, because the
    # CHAT MODEL chose its value: under the Greek default locale it passed
    # `language="el"` and filtered the tutor's English book to zero. See
    # `app.brain.retrieve.search`'s docstring. Both columns survive as Library
    # DISPLAY metadata; scoping a search is `source_ids`, which the tutor chooses.
    source_ids: list[UUID] | None = None


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
    # The RRF FUSION score (Plan 13, Stage 4.4) — an ordering key, not a
    # similarity. It tops out near 0.033 and means nothing on its own. Anything
    # that wants "how good is this match" wants `vector_score`, the cosine.
    score: float
    vector_score: float = 0.0
    lexical_score: float = 0.0


class SearchResponse(BaseModel):
    hits: list[HitOut]


class ConceptSearchRequest(BaseModel):
    """`POST /knowledge/concepts/search` — the concept-canon search (C8).

    Same query/k bounds as `SearchRequest`. No `source_ids`: a concept's whole
    POINT is that it spans books, so scoping the search to one book would throw
    away the cross-book synthesis and the divergences that are the reason this
    search exists.
    """

    query: str = Field(max_length=_MAX_QUERY_CHARS)
    k: int = Field(default=8, ge=_MIN_K, le=_MAX_K)


class ConceptCitationOut(BaseModel):
    """One citation on a concept position. `source_id` + a page number is exactly
    the Reader deep-link (`GET /knowledge/sources/{source_id}/pages/{page_no}`);
    `grounding` carries the [FIGURE] contract to the UI (a "figure" citation is
    OUR description of a picture — citable, never quotable)."""

    model_config = ConfigDict(from_attributes=True)

    source_id: UUID
    source_title: str
    pages: list[int]
    pages_label: str
    grounding: str


class ConceptPositionOut(BaseModel):
    """One position on a concept. `kind` is `consensus` | `divergence` | `only_in`,
    mirroring the canon block the drafting model reads."""

    model_config = ConfigDict(from_attributes=True)

    kind: str
    position: str
    books: list[str]
    citations: list[ConceptCitationOut]


class ConceptHitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    concept_id: UUID
    key: str
    label_en: str
    label_el: str | None
    score: float
    coverage: int
    divergence: bool
    positions: list[ConceptPositionOut]


class ConceptSearchResponse(BaseModel):
    hits: list[ConceptHitOut]


class AskRequest(BaseModel):
    query: str = Field(max_length=_MAX_QUERY_CHARS)
    locale: str
    k: int = Field(default=8, ge=_MIN_K, le=_MAX_K)


class AskResponse(BaseModel):
    text: str
    citations: list[HitOut]
