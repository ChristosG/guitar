"""What a PDF page's text layer IS — read out of the PDF, not guessed at.

MEASURED, 2026-07-17, on the tutor's four books:

    Powers      LiberationSerif  -> digital text (+ raster tab images)
    Hunter      GlyphLessFont    -> 360dpi SCAN + someone else's Tesseract
    Gallagher   GlyphLessFont    -> 360dpi SCAN + someone else's Tesseract
    Kahn        GlyphLessFont    -> 360dpi SCAN + someone else's Tesseract

`paginate.py` used to take ANY text layer for free and mark the page `ready`,
which means no model ever looks at it again. On a real-font PDF that is a gift.
On these three it silently adopts Tesseract's output as the permanent truth of
the library, and that output loses EVERY fraction glyph: zero of `¼½¾⅓⅔⅛`
survive in 2,012,859 characters. Kahn p.63 reads "¼-inch stereo cables"; the
inherited layer says "4-inch stereo cables" — a confident, plausible, wrong fact
in the exact vocabulary this library exists to teach, and one that cannot be
repaired by regex because some "4-inch" references are real (a 4-inch speaker
dust cap). The only thing that can tell them apart is looking at the page.

TWO HEURISTICS WERE TRIED FIRST AND MEASURED TO FAIL. Recorded so they are not
re-proposed:

  1. "raster coverage > 25% => needs vision" flags 100% of Hunter and Gallagher.
     Every page of a scanned book has a full-page image under it, so coverage
     carries no signal on exactly the books that need one.
  2. "chars < 0.5 * the book's median => needs vision" would not have caught
     Powers p.11 (502 chars against a 588-char median) — the page it was
     written for.

The font name is not a heuristic. It is a fact, and it is free.
"""
from __future__ import annotations

from typing import Literal

# What OCR tools name the invisible text layer they lay over a scan. Substring,
# not equality: the exact name varies by tool and version ("GlyphLessFont" is
# Tesseract's; others differ in suffix).
GLYPHLESS_MARKER = "GlyphLess"

TextLayerKind = Literal["none", "ocr", "digital"]


def text_layer_kind(page) -> TextLayerKind:
    """`none` | `ocr` | `digital` — from the page's fonts.

    `digital` means real embedded fonts: publisher text, trustworthy, free.
    `ocr` means every span is glyphless: an invisible layer over a scan, whose
    quality is whatever some upstream tool produced. We do not inherit it.
    `none` means no text at all — the pre-existing vision path.
    """
    fonts = {
        (span.get("font") or "")
        for block in page.get_text("dict").get("blocks", ())
        for line in block.get("lines", ())
        for span in line.get("spans", ())
    }
    if not fonts or not (page.get_text() or "").strip():
        return "none"
    if all(GLYPHLESS_MARKER in font or not font for font in fonts):
        return "ocr"
    return "digital"


# A page whose render is blank and whose text is noise still enters the library
# as content today. Kahn p.40 is the measured case: it renders blank in BOTH
# MuPDF and poppler (so its content is unrecoverable by any renderer — the page
# is damaged in the PDF itself), and Tesseract wrote 544 characters of
# "7 ipgges x a Rar ek en ee & eile a=" for it, which the pipeline chunks,
# embeds, and feeds to the curriculum as if it were the page.
#
# 9 of 617 pages (~1.5%) across the three scanned books are like this. The tell
# is unmistakable and needs no model: real prose is mostly multi-character words.
_MIN_LEN_TO_JUDGE = 40
_MIN_TOKENS_TO_JUDGE = 20
_STUB_TOKEN_RATIO = 0.55


def looks_like_ocr_garbage(text: str) -> bool:
    """True when `text` is OCR noise rather than a transcription.

    Deliberately conservative: a false positive costs one vision call, a false
    negative puts noise in the citation store permanently.
    """
    text = (text or "").strip()
    if len(text) < _MIN_LEN_TO_JUDGE:
        return False        # too short to judge — "Chapter 3" is not garbage
    tokens = text.split()
    if len(tokens) < _MIN_TOKENS_TO_JUDGE:
        return False
    stubs = sum(1 for token in tokens if len(token) <= 2)
    return stubs / len(tokens) > _STUB_TOKEN_RATIO
