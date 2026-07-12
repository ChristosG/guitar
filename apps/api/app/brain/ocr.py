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
from app.llm.factory import get_provider
from app.models.knowledge import Chunk, Page

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
    pages = (
        db.query(Page)
        .filter(Page.source_id == source_id, Page.status.in_(["pending", "failed"]))
        .order_by(Page.page_no)
        .all()
    )
    provider = get_provider()
    ready = failed = 0

    for page in pages:
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
        page.status = "ready" if text else "empty"
        page.ocr_error = None
        db.commit()                       # <-- this page is now safe, whatever happens next

        if text:
            _embed_page(db, provider, page)
            ready += 1

    return OcrResult(total=len(pages), ready=ready, failed=failed)


def _transcribe_with_retry(provider, page: Page) -> str:
    path = os.path.join(settings.media_dir, page.image_path)
    with open(path, "rb") as fh:
        image_bytes = fh.read()

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


def _embed_page(db, provider, page: Page) -> None:
    """Chunk + embed ONE page, deleting any prior chunks for it first so a
    re-run (retry) replaces rather than duplicates."""
    db.query(Chunk).filter(Chunk.page_id == page.id).delete()
    drafts = chunk_sections([Section(heading=None, text=page.text, page=page.page_no)])
    if not drafts:
        return
    vectors = provider.embed([d.text for d in drafts], is_query=False)
    for draft, vector in zip(drafts, vectors):
        db.add(Chunk(
            source_id=page.source_id, page_id=page.id, text=draft.text,
            section_path=draft.section_path, embedding=vector,
        ))
    db.commit()
