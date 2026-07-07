"""Ingestion pipeline: extract -> chunk -> embed -> store, with a status lifecycle.

Orchestrates the Brain pipeline for one already-created ``KnowledgeSource`` row:
``extract_text`` -> ``chunk_sections`` -> ``get_provider().embed(...)`` -> persist
``Chunk`` rows, using the Foundations embedding provider (never re-embedded or
re-normalized here — the provider already L2-normalizes, index-sorts, and
batches internally).

Status lifecycle (never leaves a source in "ingesting" on return):
    "ingesting" (committed immediately, before any extract/chunk/embed work)
    -> ... -> "ready" (success — including the legitimate empty-extraction
    case: 0 sections/chunks is NOT a failure, it's just an empty source) or
    "failed", with ``error=str(e)``, on any exception raised anywhere in the
    pipeline (extract/chunk/embed/persist).

Design choice — swallow, don't re-raise: like ``extract_text`` ("the one hard
guarantee is the contract used by ingest.py: never raise"), this function does
not propagate pipeline exceptions to its caller either. It catches them,
records "failed"/error on the ``KnowledgeSource`` row, logs the full traceback,
and returns normally. The row (status/error) is this function's entire
observable output — a caller (e.g. the sources router) creates the source,
calls this, and reads the row back; it never needs a try/except at the call
site to tell "ingestion failed" (an expected, fully-recorded outcome) apart
from an unrelated crash. The one exception to this is an unknown ``source_id``
(no row exists to record anything on) — that is a caller/programming error,
not an ingestion failure, and raises immediately.
"""
import logging
from dataclasses import dataclass

from app.brain.chunk import chunk_sections
from app.brain.extract import extract_text
from app.llm.factory import get_provider
from app.models.knowledge import Chunk, KnowledgeSource

log = logging.getLogger(__name__)


@dataclass
class IngestPayload:
    kind: str  # "pdf" | "url" | "text"
    text: str | None = None
    url: str | None = None
    data: bytes | None = None


def ingest_source(db, source_id, payload: IngestPayload) -> None:
    """Run extract -> chunk -> embed -> store for one KnowledgeSource, in place.

    `db` is a caller-owned SQLAlchemy Session; `source_id` must already refer
    to an existing KnowledgeSource row (the caller creates it first). See the
    module docstring for the full status lifecycle and the swallow-not-raise
    design choice for ordinary ingestion failures.
    """
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise ValueError(f"KnowledgeSource {source_id!r} not found")

    # Committed immediately (its own transaction) so the "ingesting" state is
    # durably visible to any other reader for the duration of the pipeline
    # below, independent of whether that pipeline ultimately succeeds or fails.
    source.status = "ingesting"
    source.error = None
    db.commit()

    try:
        sections = extract_text(payload.kind, data=payload.data, url=payload.url, text=payload.text)
        drafts = chunk_sections(sections)

        texts = [d.text for d in drafts]
        # Skip the call entirely for an empty source rather than asking the
        # provider to embed an empty batch.
        vectors = get_provider().embed(texts, is_query=False) if texts else []
        if len(vectors) != len(texts):
            raise RuntimeError(f"embed() returned {len(vectors)} vectors for {len(texts)} texts")

        char_count = 0
        for draft, vector in zip(drafts, vectors):
            db.add(
                Chunk(
                    source_id=source.id,
                    text=draft.text,
                    section_path=draft.section_path,
                    page=draft.page,
                    embedding=vector,
                )
            )
            char_count += len(draft.text)

        source.char_count = char_count
        source.status = "ready"
        db.commit()
    except Exception as e:
        log.exception("ingest_source failed for source_id=%s", source_id)
        db.rollback()  # discard any not-yet-committed Chunk adds from this attempt
        source = db.get(KnowledgeSource, source_id)
        source.status = "failed"
        source.error = str(e)
        db.commit()
