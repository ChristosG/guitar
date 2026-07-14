"""Transcribe pending Page scans, then chunk + embed each page's text.

Per-page commit is the whole point (spec D5): a `vision()` timeout on page 60
must not cost us pages 1-59. Each page gets ONE automatic retry, then is
marked `failed` and the job moves on. The tutor is never asked to fix OCR by
hand — Chris was explicit that he should not have to care about this.

`chunk_sections` and `retrieve.py` are untouched: this bolts onto the tested
pipeline rather than replacing it. The one addition is `Chunk.page_id`, which
is what turns a retrieved chunk into a citation you can actually open.
"""
import logging
import os
from dataclasses import dataclass

from app.brain.chunk import chunk_sections
from app.brain.extract import Section
from app.config import settings
from app.llm.embed_factory import get_embedder
from app.llm.factory import get_provider
from app.models.knowledge import Chunk, KnowledgeSource, Page

log = logging.getLogger(__name__)

OCR_PROMPT = (
    "Transcribe ALL text on this page verbatim, preserving reading order. "
    "Include headings, captions and table text. Do not summarize, do not "
    "translate, and do not invent any text that is not visibly present. "
    "If the page has no readable text, reply with nothing at all."
)

_MAX_ATTEMPTS = 2       # initial + one retry

# vision()'s max_tokens=4000 (app/llm/qwen.py) bounds output length, but
# vision() does not surface finish_reason (see its docstring) — so a response
# cut off mid-generation is, at this seam, indistinguishable from a complete
# one that just happens to be long. Left unguarded, a truncated page would be
# committed as `ready` and silently corrupt the citation store (a citation
# that looks verbatim but stops mid-sentence is worse than one flagged
# `failed`, since nothing downstream has a way to know to distrust it).
#
# Heuristic: ~4 chars/token is a reasonable average for English prose, so a
# genuinely complete transcription of a single printed page essentially never
# reaches 4000 tokens' worth of characters (~1127 prompt tokens/page at
# 110dpi per qwen.py's own measurement, and output text is rendered from the
# same page, not generated de novo). 12,000 chars (~3000 tokens, 75% of the
# ceiling) is comfortably past any normal page while still catching a
# response that ran into the wall. False positives cost one extra retry;
# false negatives are the status quo this guard exists to reduce, not
# eliminate — it is cheap insurance, not a proof.
_SUSPECTED_TRUNCATION_CHARS = 12_000


class _SuspectedTruncation(RuntimeError):
    """Raised when a vision() response is long enough to plausibly have hit
    the max_tokens ceiling. Routed through the same one-retry-then-`failed`
    path as any other vision() error (see module docstring): at
    temperature=0.0 a retry may reproduce the same output, but failing closed
    beats silently trusting text that might stop mid-page. A `failed` page is
    picked up again by a future `ocr_source()` run, same as any other
    failure — there is still no manual-edit path."""


@dataclass
class OcrResult:
    total: int
    ready: int
    failed: int


def ocr_source(db, source_id) -> OcrResult:
    # "ocr_running" is included so a page orphaned by a hard process kill
    # mid-vision() (which never gets to write `failed`) is resumed on the
    # next run instead of being stuck forever. Safe to re-pick-up: re-OCRing
    # a page is idempotent (_embed_page deletes that page's existing chunks
    # before re-adding them).
    pages = (
        db.query(Page)
        .filter(Page.source_id == source_id,
                Page.status.in_(["pending", "failed", "ocr_running"]))
        .order_by(Page.page_no)
        .all()
    )
    # Two seams now, not one: the vision model that READS the page (Claude/Qwen,
    # remote) and the embedder that INDEXES it (local CPU). They were the same
    # object while one vLLM box served both; they are not any more.
    provider = get_provider()
    embedder = get_embedder()
    ready = failed = 0

    for page in pages:
        if page.image_path is None:
            # D2 degenerate page (url/text/note sources get exactly one Page
            # row with image_path=NULL): there is nothing to OCR here — any
            # text it has came from extraction, not vision(). Marking it
            # `failed` would be worse than doing nothing: `failed` pages are
            # re-picked-up by this same query, so it would fail forever with
            # a cryptic os.path.join(..., None) error on every run.
            continue

        page.status = "ocr_running"
        db.commit()
        try:
            text = _transcribe_with_retry(provider, page)
        except Exception as e:
            log.warning("ocr: page %s failed permanently", page.page_no, exc_info=True)
            db.rollback()
            page = db.get(Page, page.id)
            page.status = "failed"
            page.ocr_error = str(e)
            db.commit()
            failed += 1
            continue

        page.text = text or None
        page.ocr_error = None

        if not text:
            page.status = "empty"
            db.commit()
            continue

        # Embed BEFORE committing `ready` — and commit the page's status
        # together with its chunks in one transaction. This is what makes
        # "status==ready implies chunks exist" hold: a page can never be
        # durably ready with zero chunks, because the only commit that sets
        # status="ready" is the same commit that persists those chunks.
        #
        # An embed() failure gets the SAME per-page handling as a transcribe
        # failure: mark this page `failed`, record why, and continue the
        # batch — an embedding-service hiccup on page 60 must not cost pages
        # 1-59, and must not silently leave page 60 "ready" with nothing to
        # cite.
        try:
            n_chunks = _embed_page(db, embedder, page)
        except Exception as e:
            log.warning("ocr: page %s embed failed", page.page_no, exc_info=True)
            db.rollback()
            page = db.get(Page, page.id)
            page.status = "failed"
            page.ocr_error = f"embedding failed: {e}"
            db.commit()
            failed += 1
            continue

        if n_chunks == 0:
            # Non-empty transcription that chunk_sections still couldn't
            # turn into anything chunkable (e.g. whitespace-only after
            # normalization). There's nothing to cite, so this can't be
            # `ready` either — that would repeat the same "ready with zero
            # chunks" bug this fix exists to close.
            page.status = "empty"
            db.commit()
            continue

        page.status = "ready"
        db.commit()                       # page + its chunks, one transaction
        ready += 1

    _rollup_source_status(db, source_id)

    return OcrResult(total=len(pages), ready=ready, failed=failed)


def _rollup_source_status(db, source_id) -> None:
    """Re-derive the parent `KnowledgeSource`'s `status`/`char_count` from
    its pages, after the per-page loop above has finished.

    Review fix: without this, a PDF whose pages ALL reached `ready` with real
    transcribed text still left the source row at status="empty",
    char_count=0 forever — nothing had ever re-derived it. The Library UI
    renders `source.status`, so a perfectly-OCR'd, fully-searchable book kept
    showing RED/broken with a "Retry" button.

    Mirrors the D6 rule `ingest_source` already applies (app/brain/ingest.py:
    ready iff char_count > 0) rather than inventing a new rule here — only
    READY pages' text counts, so a partially-failed batch still rolls up to
    `ready` with a char_count that reflects just the citable pages.

    Guarded the same swallow-and-log way as every other commit in this
    module (module docstring, D5): a rollup failure is logged and rolled
    back, but must not raise back into the caller and must not undo the
    per-page work already durably committed above.
    """
    try:
        source = db.get(KnowledgeSource, source_id)
        if source is None:
            return
        source_pages = db.query(Page).filter(Page.source_id == source_id).all()
        char_count = sum(
            len(p.text) for p in source_pages if p.status == "ready" and p.text
        )
        source.char_count = char_count
        source.status = "ready" if char_count > 0 else "empty"
        db.commit()
    except Exception:
        log.warning("ocr: source status rollup failed for source_id=%s", source_id, exc_info=True)
        db.rollback()


def _transcribe_with_retry(provider, page: Page) -> str:
    path = os.path.join(settings.media_dir, page.image_path)
    try:
        with open(path, "rb") as fh:
            image_bytes = fh.read()
    except FileNotFoundError:
        # A clear, human-meaningful ocr_error, not a raw errno string — this
        # is what a tutor (or a future debugging session) actually needs to
        # know: the scan is missing from disk, not a transcription failure.
        raise RuntimeError(f"OCR image file not found on disk: {path}") from None
    except OSError as e:
        raise RuntimeError(f"OCR image file could not be read ({path}): {e}") from None

    last: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            text = (provider.vision(image_bytes, OCR_PROMPT) or "").strip()
            if len(text) >= _SUSPECTED_TRUNCATION_CHARS:
                raise _SuspectedTruncation(
                    f"vision() returned {len(text)} chars (>= "
                    f"{_SUSPECTED_TRUNCATION_CHARS}) — suspected truncation "
                    "at the max_tokens ceiling; finish_reason is not "
                    "available to confirm either way"
                )
            return text
        except Exception as e:                       # noqa: BLE001 — retry boundary
            last = e
            log.info("ocr: page %s attempt %d failed", page.page_no, attempt)
    raise last                                       # type: ignore[misc]


def _embed_page(db, embedder, page: Page) -> int:
    """Chunk + embed ONE page, deleting any prior chunks for it first so a
    re-run (retry) replaces rather than duplicates. Returns the number of
    chunks created.

    Deliberately does NOT commit: the caller (`ocr_source`) commits this
    page's `status="ready"` in the SAME transaction as these chunks, so a
    page can never be durably `ready` with zero chunks — if the embed()
    call raises, everything added here (and the delete above) rolls back
    together with it."""
    db.query(Chunk).filter(Chunk.page_id == page.id).delete()
    drafts = chunk_sections([Section(heading=None, text=page.text, page=page.page_no)])
    if not drafts:
        return 0
    vectors = embedder.embed([d.text for d in drafts], is_query=False)
    for draft, vector in zip(drafts, vectors):
        db.add(Chunk(
            source_id=page.source_id, page_id=page.id, text=draft.text,
            section_path=draft.section_path, embedding=vector,
        ))
    return len(drafts)
