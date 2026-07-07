"""Text extraction: turn a pdf/url/text source into ordered Sections.

Best-effort only — layout quality varies by source. The one hard guarantee is
the contract used by ingest.py: never raise, return [] when nothing usable was
found (empty input, a corrupt PDF, an unreachable URL, ...).
"""
import logging
from dataclasses import dataclass
from html.parser import HTMLParser

import fitz  # pymupdf
import trafilatura

from app.brain.urlsafe import safe_fetch_html

log = logging.getLogger(__name__)

# A line's font size must be at least this multiple of the page's typical body
# size (see _page_sections) to be treated as a heading rather than body text.
_HEADING_SIZE_RATIO = 1.15
# A "heading" longer than this is just a long paragraph in a big font, not a title.
_HEADING_MAX_CHARS = 120


@dataclass
class Section:
    heading: str | None      # nearest heading/section path, or None
    text: str
    page: int | None


def extract_text(
    kind: str, *, data: bytes | None = None, url: str | None = None, text: str | None = None
) -> list[Section]:
    """Extract ordered Sections. kind in {"pdf","url","text"}; never raises."""
    try:
        if kind == "text":
            return _extract_plain_text(text)
        if kind == "pdf":
            return _extract_pdf(data)
        if kind == "url":
            return _extract_url(url)
        log.warning("extract_text: unknown kind %r", kind)
        return []
    except Exception:
        log.warning("extract_text(kind=%r) failed", kind, exc_info=True)
        return []


def _extract_plain_text(text: str | None) -> list[Section]:
    if not text or not text.strip():
        return []
    return [Section(heading=None, text=text, page=None)]


# ---- PDF (pymupdf / fitz) ---------------------------------------------------


def _extract_pdf(data: bytes | None) -> list[Section]:
    if not data:
        return []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        log.warning("could not open PDF bytes", exc_info=True)
        return []
    try:
        sections: list[Section] = []
        for page_index in range(doc.page_count):
            page_number = page_index + 1
            try:
                page = doc[page_index]
                page_sections = _page_sections(page, page_number)
                if not page_sections:
                    # No text layer at all (e.g. a scanned page) — fall back to OCR.
                    page_sections = _ocr_page_section(page, page_number)
                sections.extend(page_sections)
            except Exception:
                # Isolate one bad page (e.g. the unguarded plain-text fallback
                # call below raising on unusual page content) so it degrades
                # to skipping just that page, instead of discarding every
                # other page's already-extracted sections too.
                log.warning("extraction failed for page %d; skipping page", page_number, exc_info=True)
                continue
        return sections
    finally:
        doc.close()


def _page_sections(page: "fitz.Page", page_number: int) -> list[Section]:
    """Split one page into Sections at font-size heading boundaries.

    Falls back to the whole page as a single Section (heading=None) when
    structured ("dict") extraction yields no usable spans.
    """
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        blocks = []

    lines: list[tuple[str, float]] = []  # (line text, max span font size)
    for block in blocks:
        if block.get("type") != 0:  # 0 = text block; skip images/drawings
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            line_text = "".join(s.get("text", "") for s in spans).strip()
            if not line_text:
                continue
            size = max((s.get("size", 0.0) for s in spans), default=0.0)
            lines.append((line_text, size))

    if not lines:
        plain = (page.get_text() or "").strip()
        return [Section(heading=None, text=plain, page=page_number)] if plain else []

    # "Typical body size" = the font size accounting for the most total
    # characters on the page, not the most *lines*. Headings are short relative
    # to the paragraphs under them, so a per-line median is skewed by short
    # samples (a 1-heading/1-line-body page would put the median AT the heading
    # size, since len()//2 picks the upper element of a 2-item list) — weighting
    # by character count keeps that from happening, both here and in general.
    char_totals: dict[float, int] = {}
    for line_text, size in lines:
        char_totals[size] = char_totals.get(size, 0) + len(line_text)
    body_size = max(char_totals, key=char_totals.get)
    heading_threshold = body_size * _HEADING_SIZE_RATIO

    sections: list[Section] = []
    heading: str | None = None
    body_lines: list[str] = []
    in_heading_run = False

    def flush() -> None:
        if body_lines:
            sections.append(Section(heading=heading, text="\n".join(body_lines), page=page_number))

    for line_text, size in lines:
        if size >= heading_threshold and len(line_text) <= _HEADING_MAX_CHARS:
            if in_heading_run:
                # Still inside the same heading run: a multi-line title (e.g. a
                # heading wrapped across two lines of equal font size) — append
                # rather than overwrite, or every line but the last is silently
                # discarded (never kept in `heading`, never routed to `text`).
                heading = f"{heading} {line_text}"
            else:
                # Transitioning INTO a new heading run: flush whatever body
                # accumulated under the previous heading, then start fresh.
                flush()
                heading = line_text
                body_lines = []
            in_heading_run = True
        else:
            body_lines.append(line_text)
            in_heading_run = False
    flush()

    return sections


def _ocr_page_section(page: "fitz.Page", page_number: int) -> list[Section]:
    """Best-effort OCR fallback for a page with no extractable text layer at
    all (e.g. a raster-scanned page with no embedded fonts/glyphs).

    Requires no new Python dependency: OCR is a MuPDF-native capability of
    `fitz` (already a hard dependency here), gated on the system having
    Tesseract + its `tessdata` language files installed. When that's absent —
    the common case unless someone has deliberately set it up — this raises
    a plain `RuntimeError` (confirmed empirically: no hang, no crash), which
    we treat exactly like "nothing found", per this module's contract.
    Heading detection is skipped for OCR'd text: full-page OCR text all comes
    back tagged with Tesseract's synthetic "GlyphLessFont" with no meaningful
    per-line size variation, so the font-size heuristic has nothing to key
    off; `heading=None` is the honest answer here (contract explicitly allows
    it — "None is fine if undetectable").
    """
    try:
        ocr_textpage = page.get_textpage_ocr(language="eng", dpi=150, full=True)
        plain = (page.get_text(textpage=ocr_textpage) or "").strip()
    except Exception:
        log.info("OCR fallback unavailable/failed for page %d", page_number, exc_info=True)
        return []
    return [Section(heading=None, text=plain, page=page_number)] if plain else []


# ---- URL (one redirect-safe fetch, then trafilatura with an html.parser
#      tag-stripping fallback on the SAME already-fetched HTML) -------------


def _extract_url(url: str | None) -> list[Section]:
    if not url:
        return []
    try:
        html = safe_fetch_html(url)
    except ValueError:
        # Disallowed host — at the original URL or at any redirect hop the
        # fetch followed (SSRF guard, see urlsafe.safe_fetch_html) — too many
        # redirects, a non-2xx response, or a transport error: all collapse
        # to "could not fetch", per this module's never-raises contract.
        log.warning("could not fetch url for extraction: %s", url, exc_info=True)
        return []

    # Both extraction attempts run on the SAME in-memory `html` string: only
    # one network fetch happens per URL (safe_fetch_html, above), never two.
    body = _trafilatura_extract(html, url) or _strip_html(html)
    if not body or not body.strip():
        return []
    return [Section(heading=None, text=body.strip(), page=None)]


def _trafilatura_extract(html: str, url: str) -> str | None:
    """Run trafilatura's content-extraction heuristics on already-fetched HTML.

    Takes the HTML `safe_fetch_html` already downloaded rather than fetching
    itself: `trafilatura.fetch_url` follows redirects on its own, bypassing
    the SSRF guard entirely, and calling it here would also fetch the same
    page a second time for no reason.
    """
    try:
        return trafilatura.extract(html, url=url, favor_recall=True)
    except Exception:
        log.warning("trafilatura extraction failed for %s", url, exc_info=True)
        return None


class _PlainTextHTMLParser(HTMLParser):
    """Minimal tag-stripper for the fallback path: body text, no script/style."""

    _SKIP_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        stripped = data.strip()
        if stripped:
            self.chunks.append(stripped)


def _strip_html(html: str) -> str | None:
    parser = _PlainTextHTMLParser()
    try:
        parser.feed(html)
    except Exception:
        log.warning("html.parser fallback failed", exc_info=True)
        return None
    return "\n".join(parser.chunks)
