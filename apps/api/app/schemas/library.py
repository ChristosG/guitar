"""Pydantic response/request models for `routers/library.py` (Plan 9 Task 6):
collections, the page-level reader, and the source-patch/retry endpoints.

Kept separate from the SQLAlchemy `app.models.knowledge` models per this
codebase's established split (mirrors `schemas/jobs.py` vs
`app.models.generation_job`) — this module is only the HTTP boundary's shape.
"""
import uuid

from pydantic import BaseModel, Field


class PageSummary(BaseModel):
    """One row of `GET /knowledge/sources/{id}/pages` — just enough for the
    reader's page-list/status strip, not the full text/scan (that's `PageOut`,
    fetched per-page on demand)."""

    page_no: int
    status: str


class PageOut(BaseModel):
    """`GET /knowledge/sources/{id}/pages/{n}` — the reader's single-page
    view: the transcribed text, a URL to the scan it came from (spec D1: a
    citation must be openable, not merely claimed), and `total_pages` so the
    reader can render "page N of M" without a second request."""

    id: uuid.UUID
    page_no: int
    status: str
    text: str | None
    image_url: str | None
    total_pages: int


class SourceProgress(BaseModel):
    """`GET /knowledge/sources/{id}/progress` — OCR progress that SURVIVES A
    RELOAD, because every field is derived from `Page.status` rows and a
    `GenerationJob`, not from the React state of the tab that pressed the button
    (which is where it used to live, and why hard-reloading during the tutor's
    9-minute OCR showed his book as unreadable while it was being read).

    `current_page` is `null` when no job is in flight — a finished book is not
    "on" any page. `empty` pages are reported separately from `failed` ones and
    are NOT a defect: a real scan has blank pages, and conflating the two is what
    would paint a healthy book amber."""

    source_id: uuid.UUID
    total: int
    ready: int
    failed: int
    empty: int
    pending: int
    current_page: int | None
    active: bool
    job_id: uuid.UUID | None


class CollectionCreate(BaseModel):
    """Body for both `POST /library/collections` (create) and `PATCH
    /library/collections/{id}` (rename) — both take exactly one field."""

    name: str = Field(min_length=1, max_length=120)


class CollectionOut(BaseModel):
    id: uuid.UUID
    name: str
    source_count: int


class SourcePatch(BaseModel):
    """Body for `PATCH /knowledge/sources/{id}`. Both fields optional and
    independently settable — `collection_id: None` explicitly means "move to
    Unfiled", so this uses `model_fields_set` (not `exclude_none`) in the
    router to tell "field omitted" apart from "field explicitly set to
    null"."""

    title: str | None = Field(default=None, min_length=1, max_length=400)
    collection_id: uuid.UUID | None = None
