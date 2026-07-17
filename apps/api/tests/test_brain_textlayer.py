"""The font IS the signal. Two heuristics were measured to FAIL before this one:

  1. raster coverage > 25% -> flags 100% of Hunter and Gallagher, because every
     page of a scanned book has a full-page image under it.
  2. chars < 0.5 * book median -> misses Powers p.11 (502 chars, 588 median),
     the exact page the rule was written for.

`GlyphLessFont` is what OCR tools name the invisible text layer they lay over a
scan. It is a fact read out of the PDF, not a guess about it.
"""
import fitz
import pytest

from app.brain.textlayer import looks_like_ocr_garbage, text_layer_kind


def _page(make) -> fitz.Page:
    doc = fitz.open()
    page = doc.new_page()
    make(page)
    return page


def test_no_text_layer_is_none():
    page = _page(lambda p: None)
    assert text_layer_kind(page) == "none"


def test_real_font_is_digital():
    page = _page(lambda p: p.insert_text((72, 72), "Many foot controllers have", fontname="helv"))
    assert text_layer_kind(page) == "digital"


def test_glyphless_font_is_ocr(monkeypatch):
    # Building a real GlyphLessFont PDF needs an OCR toolchain; the contract we
    # care about is the font-name rule, so drive it through get_text("dict").
    page = _page(lambda p: p.insert_text((72, 72), "x", fontname="helv"))
    monkeypatch.setattr(
        type(page), "get_text",
        lambda self, kind="text", **kw: {
            "blocks": [{"lines": [{"spans": [{"font": "GlyphLessFont"}]}]}]
        } if kind == "dict" else "some ocr text",
    )
    assert text_layer_kind(page) == "ocr"


@pytest.mark.parametrize("text", [
    # Kahn p.40, verbatim — what Tesseract produced for a page that renders
    # blank in BOTH MuPDF and poppler. 544 chars the pipeline currently ingests
    # as if it were the page's content.
    "7 ipgges x \na \nRar \nek \nen ee \n& \neile \na= \n@ \nFilip \n«@ \n' \n= \nae \n¢ \n® a \na ie \n_ \n- \n¥ \n= \n, \n» \nve \né \n—- \n= \na \nra \ni \n2“ \n-ohoutea-% \n*. \n® \n' \nPre el \nif \nrene \nGS \n210) ",
])
def test_ocr_garbage_is_detected(text):
    assert looks_like_ocr_garbage(text) is True


@pytest.mark.parametrize("text", [
    # Hunter p.57, verbatim — excellent Tesseract prose. Must NOT be flagged.
    "difficult to replicate with overdrive or distortion pedals. All of this might "
    "seem just a little too easy to be true, but it works for very scientific "
    "reasons that have to do with the electrical interaction between a guitar and "
    "a tube amplifier. Every note you pluck is transmitted to the amp in the form "
    "of an electrical current of a certain voltage.",
    "",                      # empty is not garbage; it is empty
    "Chapter 3",             # short and legitimate
])
def test_good_text_is_not_garbage(text):
    assert looks_like_ocr_garbage(text) is False
