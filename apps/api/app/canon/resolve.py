"""Quote-based citation resolution — deterministic, pure CPU, NO LLM, NO recompile.

WHY THIS EXISTS. The compile model, told to cite the injected [p.N] marker, cited
the FOLIO printed on the scan instead — the number the AUTHOR put on his own page,
which disagrees with the physical page by the book's front matter. `strip_printed_
folio` (see `compile.py`) fixed four of five books by deleting that number before
the model saw it; Gallagher (388pp/366K tok) reconstructs the numbering across the
book and stays offset ~+24. A numeric validator cannot catch it — `cited + 24` is
still a real `page_no` that passes the existence check. So the model stops
reporting a NUMBER: it copies a verbatim ANCHOR quote from the author's words, and
THIS module finds the page by locating that quote. A wrong number is a valid
number; a made-up quote matches nothing.

PURE CPU, PROVIDER-INDEPENDENT. Nothing here calls a model. The index is the page
text the compile already stored; the match is `rapidfuzz`, the same C-fast library
`reconcile.py` blocks names with. So this runs offline, over stored anchors, for
free, any number of times — which is what makes re-tuning the threshold and
rolling quote-based to another book cost zero subscription spend (`reresolve_*`).

AUTHOR TEXT ONLY — THE FABRICATION GUARD. The index is built from `book_text()`,
the executable `[FIGURE]...[/FIGURE]` contract from `brain/ocr.py`, which strips
our own picture descriptions. An anchor that exists ONLY inside a figure region
therefore matches nothing and resolves to no page — it can never cite our caption
as the author's sentence with a real page number on it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from rapidfuzz import fuzz
from sqlalchemy import select

from app.brain.ocr import book_text
from app.models.canon import ConceptClaim
from app.models.knowledge import Page

log = logging.getLogger(__name__)

# The `rapidfuzz.fuzz.partial_ratio` score (0-100) an anchor must clear to become a
# citation. `partial_ratio` — NOT `WRatio` — because the anchor is SHORT and a page
# is LONG: partial_ratio aligns the anchor against the best-matching SUBSTRING of
# the page, which is exactly "is this quote on this page?". (`reconcile.py` uses
# `WRatio` for the opposite shape — two short names of comparable length.)
#
# 88 is the calibrated floor: a normalized exact/OCR-noise anchor scores ~100; a
# genuinely unrelated quote scores <50 (dropped — invariant 3, which also kills
# hallucinated pages); a quote straddling a page boundary scores ~74-89 against
# EITHER single page but 100 against the adjacent-page concatenation, so 88 forces
# the span match to win and the single-page near-miss to lose. NAMED so Task 7
# re-tunes it on Gallagher for free via `reresolve_source`.
RESOLVE_THRESHOLD = 88.0

_WS = re.compile(r"\s+")
# `re.UNICODE` so Greek/accented author text folds to itself rather than to nothing
# — the same reasoning as `compile._SLUG`. (The anchors are English author prose,
# but the normalizer must not silently empty a non-Latin page.)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def _norm(text: str) -> str:
    """lowercase, strip punctuation, collapse whitespace. The anchor the model
    copied and the page text it copied from must be compared modulo typography and
    OCR spacing noise; nothing that changes MEANING is touched."""
    return _WS.sub(" ", _PUNCT.sub(" ", (text or "").lower())).strip()


@dataclass(frozen=True)
class _PageText:
    page_no: int
    text: str          # normalized AUTHOR text — figure regions already stripped


def build_page_index(db, source_id: UUID) -> list[_PageText]:
    """`[_PageText(page_no, normalized author text)]` for one source, in page
    order, FIGURE REGIONS STRIPPED (invariant 2). Ready pages only — a page that
    is not `ready` has no trustworthy text to match against.

    `book_text()` is the `[FIGURE]` contract, executable; call it, never re-derive
    it (its docstring: the compile is the caller it was written for). A page whose
    author half normalizes to empty (a bare tab page, all figure) contributes no
    entry — a real anchor cannot have come from it.
    """
    pages = db.scalars(
        select(Page)
        .where(Page.source_id == source_id, Page.status == "ready")
        .order_by(Page.page_no)
    ).all()
    out: list[_PageText] = []
    for page in pages:
        author = _norm(book_text(page.text or ""))
        if author:
            out.append(_PageText(page_no=page.page_no, text=author))
    return out


def resolve_anchor(anchor: str, index: list[_PageText]) -> int | None:
    """The physical `page_no` an anchor quote lives on, or None if nothing clears
    `RESOLVE_THRESHOLD`.

    Matches the normalized anchor against each page on its own AND across every
    physically-adjacent page boundary (a claim's quote can straddle two pages),
    attributing a cross-boundary match to the page the quote STARTS on via
    `partial_ratio_alignment.dest_start`. None (drop the citation) when the best
    score is below threshold — which is every hallucinated quote and every quote
    that lives only inside a figure region (invariants 2 & 3).
    """
    query = _norm(anchor)
    if not query or not index:
        return None

    best_score = 0.0
    best_page: int | None = None

    # 1) each page on its own.
    for pt in index:
        score = fuzz.partial_ratio(query, pt.text)
        if score > best_score:
            best_score, best_page = score, pt.page_no

    # 2) each PHYSICALLY-adjacent boundary — the quote may straddle two pages. We
    #    never bridge across a gap in the ready pages (`b == a + 1` only): a real
    #    quote does not span a page that was dropped, and bridging a hole would
    #    invent an adjacency the book does not have.
    for a, b in zip(index, index[1:]):
        if b.page_no != a.page_no + 1:
            continue
        bridge = a.text + " " + b.text
        al = fuzz.partial_ratio_alignment(query, bridge)
        if al is None or al.score <= best_score:
            continue
        best_score = al.score
        boundary = len(a.text) + 1        # index of `b`'s first char in the join
        best_page = a.page_no if al.dest_start < boundary else b.page_no

    return best_page if best_score >= RESOLVE_THRESHOLD else None


# ---------------------------------------------------------------------------
# Re-resolve over stored anchors — the whole reason the anchor is persisted
# ---------------------------------------------------------------------------

@dataclass
class ResolveSummary:
    source_id: UUID
    total: int = 0
    resolved: int = 0
    dropped: int = 0


def reresolve_source(db, source_id: UUID) -> ResolveSummary:
    """Re-run resolution over a source's STORED anchors — NO LLM, NO recompile.

    THE WHOLE REASON `concept_claim.anchor` IS PERSISTED. Re-tuning
    `RESOLVE_THRESHOLD`, fixing a resolver bug, or rolling quote-based to another
    book costs ZERO subscription spend: this reads the anchors already on disk and
    rewrites `pages`. A claim with no anchor (pre-Unit-B, or a figure claim) is
    LEFT ALONE — never blanked. A no-match empties `pages` and is counted
    (invariant 3). Commits, and returns the observable summary.
    """
    index = build_page_index(db, source_id)
    claims = db.scalars(
        select(ConceptClaim).where(ConceptClaim.source_id == source_id)).all()
    summary = ResolveSummary(source_id=source_id)
    for claim in claims:
        anchor = (claim.anchor or "").strip()
        if not anchor:
            continue                    # no anchor -> not a resolvable claim
        summary.total += 1
        page = resolve_anchor(anchor, index)
        if page is None:
            claim.pages = []
            summary.dropped += 1
            log.warning("canon.resolve: no page for stored anchor %.70r "
                        "(source=%s) — dropping citation", anchor, source_id)
        else:
            claim.pages = [page]
            summary.resolved += 1
    db.commit()
    log.info("canon.resolve: source=%s — %d anchors, %d resolved, %d dropped",
             source_id, summary.total, summary.resolved, summary.dropped)
    return summary


def reresolve_all(db) -> list[ResolveSummary]:
    """Re-resolve every source that has at least one stored anchor. The controller
    calls this after re-tuning `RESOLVE_THRESHOLD`; a source with no anchors (the
    four books compiled before Unit B) is a no-op."""
    source_ids = [
        r[0] for r in db.execute(
            select(ConceptClaim.source_id)
            .where(ConceptClaim.anchor.is_not(None))
            .distinct()
        ).all()
    ]
    return [reresolve_source(db, sid) for sid in source_ids]
