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

import re
from typing import Literal

# What OCR tools name the invisible text layer they lay over a scan. Substring,
# not equality: the exact name varies by tool and version ("GlyphLessFont" is
# Tesseract's; others differ in suffix).
GLYPHLESS_MARKER = "GlyphLess"

TextLayerKind = Literal["none", "ocr", "digital"]


def text_layer_kind(page) -> TextLayerKind:
    """`none` | `ocr` | `digital` — from the page's fonts.

    `digital` means every span on the page is a real embedded font: publisher
    text, trustworthy, free. `ocr` means AT LEAST ONE span is glyphless: an
    invisible layer over a scan, whose quality is whatever some upstream tool
    produced. We do not inherit it. `none` means no text at all — the
    pre-existing vision path.

    ANY, not ALL — deliberately. This used to be `all(...)`: a page counted
    as `ocr` only if every span was glyphless, so a single real-font span
    (a stamped page number, a running header, a watermark added after
    scanning) flipped the WHOLE page to `digital` and it was adopted as
    permanent truth, fraction-glyph loss and all. The costs of the two
    directions are not symmetric, so the polarity should not be either:
    wrongly sending a good page to vision costs one extra vision call;
    wrongly trusting a bad page corrupts the library permanently, because
    `ready` means nothing ever looks at that page again — and the corruption
    is the confident-but-wrong kind ("¼-inch" -> "4-inch") that reads as
    fine. A page is judged by its worst span, not its best one.
    """
    fonts = {
        (span.get("font") or "")
        for block in page.get_text("dict").get("blocks", ())
        for line in block.get("lines", ())
        for span in line.get("spans", ())
    }
    if not fonts or not (page.get_text() or "").strip():
        return "none"
    if any(GLYPHLESS_MARKER in font for font in fonts):
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
#
# BUT a length-only stub count is too blunt: string-name charts ("E A D G B E
# E A D G B E  1 3 5 b7") and fret/finger charts ("1 2 3 4  1 3 4 1  2 4 1 3
# T 1 2 3") are staple content in a guitar instructional book, and they are
# made almost entirely of tokens <= 2 characters. Counting those as stubs put
# real, correctly-transcribed pages of real content over the garbage line.
# `_MUSICAL_TOKEN` exempts tokens built only from note letters, digits, and
# tab/chord punctuation from the stub count, so short-but-musical text no
# longer counts against a page.
_MIN_LEN_TO_JUDGE = 40
_MIN_TOKENS_TO_JUDGE = 20
_STUB_TOKEN_RATIO = 0.55

# Note letters (A-G), flat/sharp/x-string markers (b, #, x, X), fret-hand
# fingering (t, T for thumb), digits, and the punctuation tab/chord charts are
# written in (/ for slash chords, | for bar lines, : for repeats, - for muted
# strings or ranges, . for dotted values). A token made ONLY of these is
# musical notation, not noise, no matter how short.
_MUSICAL_TOKEN = re.compile(r"^[A-Gb#xXtT0-9/|:\-\.]+$")


# A raster covering at least this much of the page is content, not decoration.
# Powers' tab pages measure 49-79%; a logo or a rule measures under 1%. The gap
# is wide, so the threshold is not delicate.
_CONTENT_IMAGE_AREA = 0.10


def has_content_images(page) -> bool:
    """True when a DIGITAL page carries raster art its text layer cannot describe.

    Only meaningful for `text_layer_kind(page) == "digital"`. On a scan every page
    has a full-page image under it, so this would answer True for all of them and
    mean nothing — which is exactly how the first, rejected heuristic ("raster
    coverage > 25% => needs vision") managed to flag 100% of Hunter and Gallagher.

    Deliberately NOT a function of text length. A page with one paragraph is a
    normal page, not a broken one; Powers p.11 has 502 chars against a 588-char
    median and is still 75% tab. What makes it need vision is the tab, not the
    brevity.
    """
    area = page.rect.width * page.rect.height
    if area <= 0:
        return False
    covered = sum(
        rect.width * rect.height
        for img in page.get_images(full=True)
        for rect in page.get_image_rects(img[0])
    )
    return (covered / area) >= _CONTENT_IMAGE_AREA


def looks_like_ocr_garbage(text: str) -> bool:
    """True when `text` is OCR noise rather than a transcription.

    NOT symmetric-cost the way text_layer_kind's page-selection is: this
    function screens the OUTPUT of vision transcription (Task 9 marks a page
    `failed` when this returns True), so a false positive here does not cost
    one extra vision call — it THROWS AWAY a correct transcription of real
    content and marks the page failed, which is the same class of permanent
    harm as trusting a bad page, not the cheap class. A false negative puts
    noise in the citation store permanently. Treat both directions as
    expensive; this is not "deliberately conservative" toward one side.
    """
    text = (text or "").strip()
    if len(text) < _MIN_LEN_TO_JUDGE:
        return False        # too short to judge — "Chapter 3" is not garbage
    tokens = text.split()
    if len(tokens) < _MIN_TOKENS_TO_JUDGE:
        return False
    stubs = sum(
        1 for token in tokens
        if len(token) <= 2 and not _MUSICAL_TOKEN.match(token)
    )
    return stubs / len(tokens) > _STUB_TOKEN_RATIO
