"""Ingestion pipeline: paginate -> extract -> chunk -> embed -> store, with a
status lifecycle.

Orchestrates the Brain pipeline for one already-created ``KnowledgeSource`` row:
``paginate_source`` (creates/replaces this source's ``Page`` rows) ->
``extract_text`` -> ``chunk_sections`` -> ``get_provider().embed(...)`` ->
persist ``Chunk`` rows (each linked to its ``Page`` via ``page_id``), using
the Foundations embedding provider (never re-embedded or re-normalized here —
the provider already L2-normalizes, index-sorts, and batches internally).

Status lifecycle (never leaves a source in "ingesting" on return):
    "ingesting" (committed immediately, before any paginate/extract/chunk/
    embed work) -> ... -> "ready" (success, char_count > 0) or "empty"
    (success, but nothing was extracted — SPEC D6: this is NOT "ready".
    Three live sources once sat "ready" with 0 characters and rendered as a
    healthy green badge in the UI for two days, hiding a broken knowledge
    base — an empty source must be visibly distinguishable from a populated
    one) or "failed", with ``error=str(e)``, on any exception raised
    anywhere in the pipeline (paginate/extract/chunk/embed/persist).

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
from app.brain.extract import Section, extract_text
from app.brain.paginate import paginate_source
from app.llm.factory import get_provider
from app.models.knowledge import Chunk, KnowledgeSource

log = logging.getLogger(__name__)

# Resource-exhaustion cap (defense-in-depth, review pass 2): unlike kind="text"
# (capped at MAX_TEXT_CHARS in routers/knowledge.py) and kind="pdf" via upload
# (capped at MAX_UPLOAD_BYTES, same module), the kind="url" path has no
# upstream size limit at all — a caller can point it at an arbitrarily large
# response body and drive an unbounded fetch -> chunk -> embed -> persist.
# Enforced here, after extract_text and before chunk_sections, so one check
# closes the URL path AND covers kind="pdf" (a highly-compressible PDF can
# still decode to far more text than its 30 MiB upload-byte cap implies).
MAX_INGEST_CHARS = 2_000_000


def _cap_total_chars(sections: list[Section], source_id) -> list[Section]:
    """Truncate `sections` to a combined total of at most MAX_INGEST_CHARS.

    Keeps each Section whole while the running total still fits the budget,
    truncates the one Section that crosses it (mid-text, no boundary-seeking —
    this is a hard safety cap, not a chunk split), and drops every Section
    after that. Never raises; ingestion still completes as "ready" against
    the truncated text (truncate-and-ingest, not a failure). Logs a warning
    with the source id and the original vs. capped size when this actually
    truncates anything.
    """
    total = sum(len(s.text) for s in sections)
    if total <= MAX_INGEST_CHARS:
        return sections

    capped: list[Section] = []
    remaining = MAX_INGEST_CHARS
    for section in sections:
        if remaining <= 0:
            break
        if len(section.text) <= remaining:
            capped.append(section)
            remaining -= len(section.text)
        else:
            capped.append(
                Section(heading=section.heading, text=section.text[:remaining], page=section.page)
            )
            remaining = 0

    log.warning(
        "ingest_source: source_id=%s extracted text (%d chars) exceeded "
        "MAX_INGEST_CHARS=%d; truncated to %d chars",
        source_id,
        total,
        MAX_INGEST_CHARS,
        MAX_INGEST_CHARS,
    )
    return capped


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
        # Create/replace this source's Page rows first (Plan 9 Task 3's
        # paginate_source — idempotent, deletes-then-recreates). Every chunk
        # created below gets linked to one of these via page_id.
        pages = paginate_source(
            db, source_id, kind=payload.kind, data=payload.data, url=payload.url, text=payload.text,
        )
        page_by_no = {p.page_no: p for p in pages}

        # Ordering/double-fetch note: for kind="url", paginate_source's
        # single-Page path (app/brain/paginate.py's `_single_page`) already
        # called extract_text -> safe_fetch_html and fetched this exact URL
        # once, storing the result on the one Page it created. Calling
        # extract_text again here would fetch the SAME URL a second time over
        # the network for no reason — so for "url" we reuse that Page's text
        # instead of re-extracting. For "text"/"pdf" there is no network cost
        # to re-extracting (a string, or bytes already held in memory), so
        # those still go through extract_text directly, unchanged from
        # before this task — and for "pdf" that's not just cheaper-to-skip
        # duplication anyway: paginate_source's PDF path only renders page
        # images and opportunistically captures an existing text layer (or
        # leaves a page "pending" for the Task 4 OCR job) — it does not run
        # the full heading-aware section extraction the chunker needs.
        if payload.kind == "url":
            single_page = pages[0] if pages else None
            sections = (
                [Section(heading=None, text=single_page.text, page=single_page.page_no)]
                if single_page and single_page.text
                else []
            )
        else:
            sections = extract_text(payload.kind, data=payload.data, url=payload.url, text=payload.text)

        sections = _cap_total_chars(sections, source_id)
        drafts = chunk_sections(sections)

        texts = [d.text for d in drafts]
        # Skip the call entirely for an empty source rather than asking the
        # provider to embed an empty batch.
        vectors = get_provider().embed(texts, is_query=False) if texts else []
        if len(vectors) != len(texts):
            raise RuntimeError(f"embed() returned {len(vectors)} vectors for {len(texts)} texts")

        char_count = 0
        for draft, vector in zip(drafts, vectors):
            page = page_by_no.get(draft.page or 1) or (pages[0] if pages else None)
            db.add(
                Chunk(
                    source_id=source.id,
                    page_id=page.id if page else None,
                    text=draft.text,
                    section_path=draft.section_path,
                    embedding=vector,
                )
            )
            char_count += len(draft.text)

        source.char_count = char_count
        # SPEC D6 — a source with nothing in it is NOT ready. Three live rows
        # sat "ready" with 0 chars and rendered green; that lie hid a broken
        # knowledge base for two days and is why the agent had nothing to
        # ground against.
        source.status = "ready" if char_count > 0 else "empty"
        db.commit()
    except Exception as e:
        log.exception("ingest_source failed for source_id=%s", source_id)
        try:
            db.rollback()  # discard any not-yet-committed Chunk adds from this attempt
            source = db.get(KnowledgeSource, source_id)
            source.status = "failed"
            source.error = str(e)
            db.commit()
        except Exception:
            # Best-effort recovery (final review, Fix 3): this recovery block
            # itself talks to the DB (rollback/get/commit) — if THAT also
            # fails (e.g. the connection that just errored is now unusable),
            # it must not propagate either, or ingest_source would break its
            # one hard contract ("never raises") for what is ultimately still
            # an infra-level failure, not a caller/programming error. Swallow
            # it: the row may be left stranded at "ingesting" in this rare
            # double-failure case — worse than losing the human-readable
            # error message, but still strictly better than crashing the
            # caller (routers/knowledge.py's request handler).
            log.warning(
                "ingest_source: failed to record failure status for source_id=%s",
                source_id,
                exc_info=True,
            )
