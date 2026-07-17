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


def test_a_single_real_font_span_does_not_launder_an_ocr_page(monkeypatch):
    # A scanned page carrying a Tesseract layer PLUS one real-font element (a
    # stamped page number, a running header, a watermark added after scanning)
    # must NOT be adopted as "digital". Mixed fonts mean the text is at least
    # partly machine-transcribed, so the whole page stays untrusted.
    page = _page(lambda p: p.insert_text((72, 72), "x", fontname="helv"))
    monkeypatch.setattr(
        type(page), "get_text",
        lambda self, kind="text", **kw: {
            "blocks": [{"lines": [{"spans": [
                {"font": "Helvetica"},
                {"font": "GlyphLessFont"},
            ]}]}]
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


def test_a_string_name_chart_is_not_garbage():
    # E-A-D-G-B-E tuning names plus interval numbers ("1 3 5 b7"), repeated —
    # staple content in a guitar instructional book, and realistic length for
    # a transcribed page (the single-line form from the review is too short
    # to even reach _MIN_LEN_TO_JUDGE/_MIN_TOKENS_TO_JUDGE, so it can't
    # exercise the bug). Every token is <= 2 chars, so a naive length-only
    # stub count flags this as garbage and a real transcription of real
    # content gets marked `failed` and thrown away.
    text = "E A D G B E   E A D G B E   E A D G B E   1 3 5 b7 1 3 5 b7 1 3 5 b7"
    assert looks_like_ocr_garbage(text) is False


def test_a_fret_finger_chart_is_not_garbage():
    # A fret/finger diagram: single digits and a lone "T" for thumb, all
    # short tokens, all musical, repeated to a realistic transcribed-page
    # length. Must not be screened out as noise.
    text = "1 2 3 4  1 3 4 1  2 4 1 3  T 1 2 3  1 2 3 4  1 3 4 1  2 4 1 3  T 1 2 3"
    assert looks_like_ocr_garbage(text) is False
