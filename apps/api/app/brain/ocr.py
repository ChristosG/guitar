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
import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import or_

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

# Page-level pickup cap (Stage 7.4). Counts PICKUPS (`Page.ocr_attempts`), not
# vision() calls — each pickup already gets `_MAX_ATTEMPTS` tries of its own.
# Three exists to make re-picking-up `empty` pages affordable: an OCR'd-to-empty
# page used to be dead forever (the old pickup filter was pending/failed/
# ocr_running only), so a one-off "" from the model permanently lost a page of
# the book; retrying it on every run instead would re-bill the book's genuinely
# blank pages — a real 77-page scan has several — every single time the tutor
# pressed Retry. After three honest attempts a page is left alone.
MAX_PAGE_ATTEMPTS = 3

# Which page statuses `ocr_source` re-picks-up. "ocr_running" is here so a page
# orphaned by a hard process kill mid-vision() (which never got to write `failed`)
# resumes on the next run instead of being stuck forever; "empty" is here for the
# reason above. Safe to re-pick-up any of them: re-OCRing a page is idempotent
# (`_embed_page` deletes that page's existing chunks before re-adding them).
PICKUP_STATUSES = ("pending", "failed", "ocr_running", "empty")

# The quality gate (Stage 7.4). A vision model asked to transcribe a page it
# cannot read does not fail — it NARRATES. These are the shapes that answer,
# lifted from what actually turned up in the real library (retrieve.py's floor
# quotes the same 38-char "There is no visible text on this page." junk chunk,
# which was embedded, indexed, and cited to the tutor before anything screened
# for it). Matched only against a SHORT response — see `_looks_like_no_text`.
_NO_TEXT_PATTERNS = re.compile(
    r"(no (visible|readable|legible|discernible)?\s*text"
    r"|nothing (is )?(visible|readable|written)"
    r"|(this |the )?page (is|appears) (blank|empty)"
    r"|blank page"
    r"|δεν υπάρχει (ορατό )?κείμενο"
    r"|κενή σελίδα)",
    re.IGNORECASE,
)

# A page's response is screened for a "no visible text" sentinel only if it is at
# most this long. THE POINT OF THE BOUND: a genuinely short page with real content
# ("Chapter 3", a part title, a photo caption) must never be screened out — that
# would turn a healthy book amber, which is the exact failure mode Stage 7.4 was
# warned about. A model's refusal narration is one sentence; a real page that
# happens to also contain the phrase "no visible text" (a book about OCR, say)
# will be far longer than this.
_NO_TEXT_MAX_CHARS = 200

# Unicode-garbage screen. Mojibake and a mis-decoded scan produce long runs of
# symbol/private-use/replacement codepoints; real prose in any language this app
# serves (English, Greek) is overwhelmingly letters, digits, whitespace and
# punctuation. Only applied above a length where the ratio means anything at all:
# a 12-char page of pure musical symbols is not evidence of a broken read.
_GARBAGE_RATIO = 0.30
_GARBAGE_MIN_CHARS = 120


class _SuspectedTruncation(RuntimeError):
    """Raised when a vision() response is long enough to plausibly have hit
    the max_tokens ceiling. Routed through the same one-retry-then-`failed`
    path as any other vision() error (see module docstring): at
    temperature=0.0 a retry may reproduce the same output, but failing closed
    beats silently trusting text that might stop mid-page. A `failed` page is
    picked up again by a future `ocr_source()` run, same as any other
    failure — there is still no manual-edit path."""


class _GarbageTranscription(RuntimeError):
    """The response was long enough to judge and was mostly not language (see
    `_looks_like_garbage`). Routed through the same one-retry-then-`failed` path
    as any other vision() error: a mis-decoded scan is a FAILED page (amber, with
    a retry), not an `empty` one (which would silently roll up green)."""


def _looks_like_no_text(text: str) -> bool:
    """True for a model NARRATING that the page has nothing on it, rather than
    transcribing it. Such a page is `empty` — a true fact about the book, not a
    failure to read it — so it must not be counted against the source's health.

    The length bound is load-bearing; see `_NO_TEXT_MAX_CHARS`.
    """
    return len(text) <= _NO_TEXT_MAX_CHARS and bool(_NO_TEXT_PATTERNS.search(text))


def _looks_like_garbage(text: str) -> bool:
    """True when a long-enough response is mostly not letters/digits/whitespace/
    punctuation — the signature of mojibake or a mis-decoded image, which must
    never reach the index as if it were the tutor's book.

    Unicode CATEGORIES, not an ASCII allowlist: Greek is a first-class language
    here, and `str.isascii()`-style screens fail an entire Greek corpus.
    """
    if len(text) < _GARBAGE_MIN_CHARS:
        return False
    junk = sum(
        1 for ch in text
        if not (ch.isspace() or unicodedata.category(ch)[0] in ("L", "N", "P"))
    )
    return junk / len(text) > _GARBAGE_RATIO


@dataclass
class OcrResult:
    total: int
    ready: int
    failed: int


def ocr_source(db, source_id) -> OcrResult:
    # Pages this run will touch: a non-terminal/retryable status (PICKUP_STATUSES)
    # that has not already burned its attempt budget (MAX_PAGE_ATTEMPTS). The
    # attempt cap is what lets `empty` be retryable without re-billing the book's
    # blank pages on every run — see both constants' docstrings.
    #
    # `or_(is_(None), <)` because `ocr_attempts` is NULL on every Page row written
    # before the column existed (the server_default only applies to new INSERTs) —
    # and in SQL, `NULL < 3` is NULL, not TRUE. Without the explicit NULL branch,
    # this filter would silently exclude every page of the tutor's existing books
    # and OCR would appear to do nothing at all.
    pages = (
        db.query(Page)
        .filter(Page.source_id == source_id,
                Page.status.in_(PICKUP_STATUSES),
                or_(Page.ocr_attempts.is_(None),
                    Page.ocr_attempts < MAX_PAGE_ATTEMPTS))
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
        # Incremented BEFORE the call, and committed with the "ocr_running"
        # status, so it counts attempts that were MADE — a page whose vision()
        # call hard-kills the process still spent an attempt, and must not be
        # able to spend an unbounded number of them by never getting to record
        # any (`ocr_running` is itself a pickup status).
        page.ocr_attempts = (page.ocr_attempts or 0) + 1
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

        # The quality gate (Stage 7.4), in the one place that can still tell the
        # difference between "this page has nothing on it" and "we failed to read
        # this page". Getting that distinction wrong in either direction is a lie
        # the tutor pays for: an `empty` page rolls the source up GREEN, a
        # `failed` one turns it AMBER with a retry.
        if _looks_like_no_text(text):
            log.info("ocr: page %s narrated 'no visible text' — recording empty",
                     page.page_no)
            text = ""

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
            # Garbage is screened HERE, inside the retry loop, rather than at the
            # call site: mojibake is exactly the kind of transient decode failure
            # a second attempt can come back clean from, and if it doesn't, this
            # lands on the same `failed` path as any other unreadable page.
            if _looks_like_garbage(text):
                raise _GarbageTranscription(
                    f"vision() returned {len(text)} chars that are mostly not "
                    "language (>30% symbol/control codepoints) — the scan did "
                    "not decode"
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
