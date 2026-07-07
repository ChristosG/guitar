import os

import pytest

from app.brain.extract import extract_text

BOOK = "/mnt/nvme2TB/guitar_tutor/Getting Great Guitar Sounds.pdf"


def test_text_passthrough():
    secs = extract_text("text", text="Hello tone")
    assert len(secs) == 1 and "Hello tone" in secs[0].text


def test_text_empty_and_none_return_empty_list():
    assert extract_text("text", text="") == []
    assert extract_text("text", text="   \n\t  ") == []
    assert extract_text("text") == []


def test_pdf_empty_and_garbage_bytes_return_empty_list():
    assert extract_text("pdf", data=b"") == []
    assert extract_text("pdf") == []
    assert extract_text("pdf", data=b"this is not a pdf at all") == []


def test_url_none_or_empty_returns_empty_list():
    # No network involved: url=None/"" short-circuits before any fetch is attempted.
    assert extract_text("url") == []
    assert extract_text("url", url="") == []


def test_unknown_kind_returns_empty_list():
    assert extract_text("carrier-pigeon") == []


def test_pdf_synthetic_single_page_roundtrips_inserted_text():
    import fitz  # pymupdf; build a throwaway one-page PDF entirely in memory

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "A humbucker pickup cancels 60-cycle hum.")
    data = doc.tobytes()
    doc.close()

    secs = extract_text("pdf", data=data)
    joined = " ".join(s.text for s in secs)
    assert "A humbucker pickup cancels 60-cycle hum." in joined
    assert secs and all(s.page == 1 for s in secs)


def test_pdf_synthetic_detects_heading_by_font_size():
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "GEAR BASICS", fontsize=22)
    page.insert_text(
        (72, 200),
        "A humbucker pickup cancels 60-cycle hum through opposed coil winding.",
        fontsize=10,
    )
    data = doc.tobytes()
    doc.close()

    secs = extract_text("pdf", data=data)
    joined = " ".join(s.text for s in secs).lower()
    assert "60-cycle hum" in joined
    # Heading detection is a best-effort heuristic (None is an acceptable outcome
    # in general) but on this deliberately-exaggerated font-size contrast it must fire.
    assert any(s.heading and "GEAR BASICS" in s.heading for s in secs)


def test_pdf_synthetic_multi_page_tracks_page_number():
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Page one is about tone.")
    doc.new_page().insert_text((72, 72), "Page two is about pickups.")
    data = doc.tobytes()
    doc.close()

    secs = extract_text("pdf", data=data)
    by_page = {p: " ".join(s.text for s in secs if s.page == p).lower() for p in (1, 2)}
    assert "tone" in by_page[1]
    assert "pickups" in by_page[2]


def test_pdf_ocr_fallback_invoked_when_a_page_has_no_text_layer(monkeypatch):
    """Deterministically exercise the OCR-fallback *wiring* (call the OCR
    helper when the normal text-layer path finds nothing) without depending
    on whether Tesseract is actually installed on the machine running the
    tests — that environment-dependent behavior is covered separately by
    `test_ocr_fallback_is_a_clean_noop_without_tesseract` and by the real-book
    tests below.
    """
    import fitz

    from app.brain import extract as extract_module

    doc = fitz.open()
    doc.new_page()  # a page with no text layer at all (blank)
    data = doc.tobytes()
    doc.close()

    def fake_ocr_page_section(page, page_number):
        return [extract_module.Section(heading=None, text="ocr'd content", page=page_number)]

    monkeypatch.setattr(extract_module, "_ocr_page_section", fake_ocr_page_section)

    secs = extract_text("pdf", data=data)
    assert secs == [extract_module.Section(heading=None, text="ocr'd content", page=1)]


def test_ocr_fallback_is_a_clean_noop_without_tesseract():
    """A blank page has nothing to recognize, so this is a true environment-
    independent assertion: it returns [] whether OCR fails outright (no
    Tesseract installed — confirmed the case in this sandbox) or runs and
    finds nothing (Tesseract installed elsewhere). Either way: never raises.
    """
    import fitz

    from app.brain.extract import _ocr_page_section

    doc = fitz.open()
    page = doc.new_page()
    result = _ocr_page_section(page, 1)
    doc.close()
    assert result == []


# --- The real book -----------------------------------------------------------
#
# DISCOVERY, verified independently with *two* separate PDF libraries before
# writing these assertions: "Getting Great Guitar Sounds.pdf" is a 77-page
# raster scan with NO text layer anywhere in it — every page is a single
# embedded image, not text-showing operators. Confirmed two ways:
#
#   1. PyMuPDF: `sum(len(doc[i].get_text()) for i in range(doc.page_count))`
#      is 0 across all 77 pages; every page's get_text("dict") block is
#      type=1 (image), never type=0 (text).
#   2. Poppler (an entirely independent codebase, via the `pdftotext`/
#      `pdffonts` CLIs): `pdftotext` on the whole file returns 77 bytes total
#      (one stray newline per page — no words at all); `pdffonts` shows
#      exactly one embedded TrueType font with `uni=no` (no ToUnicode map,
#      so even that font can't be mapped back to real characters).
#
# Getting "pickup" out of *this specific file* requires OCR (rendering each
# page to a bitmap and recognizing it), which extract.py now supports as a
# fallback (see `_ocr_page_section`, gated on the system having Tesseract
# installed) — but Tesseract is not installed in this environment (confirmed:
# `which tesseract` finds nothing, and installing it needs `apt-get install`,
# which needs root/a password this task does not have). It is also, by
# design, a no-op everywhere pymupdf's normal text-layer extraction already
# finds something, so it changes nothing for ordinary text-layer PDFs.
#
# These tests verify what extract_text() can honestly promise for *this*
# file today: it does not raise, does not hang, and degrades to `[]` per the
# "never raises on unextractable input" contract, rather than asserting book
# content that does not exist in the source file as currently provided. See
# the task report's Concerns section for the full writeup and next steps
# (installing Tesseract host- and image-side would make the OCR fallback
# above start actually returning prose here, with no further code changes).


@pytest.mark.skipif(not os.path.exists(BOOK), reason="book not present")
def test_pdf_book_does_not_raise_and_completes_promptly():
    import time

    with open(BOOK, "rb") as f:
        data = f.read()
    start = time.monotonic()
    secs = extract_text("pdf", data=data)
    elapsed = time.monotonic() - start
    assert isinstance(secs, list)
    assert elapsed < 60  # 77 pages, each attempting (fast-failing) OCR fallback


@pytest.mark.skipif(not os.path.exists(BOOK), reason="book not present")
def test_pdf_book_has_no_text_layer_in_this_environment():
    with open(BOOK, "rb") as f:
        secs = extract_text("pdf", data=f.read())
    # See the discovery note above. If this assertion ever starts failing,
    # that's *good news* (Tesseract got installed, or the file was replaced
    # with a text-layer version) — update/remove this test then.
    assert secs == []


def _online() -> bool:
    import httpx

    try:
        httpx.get("https://example.com", timeout=3)
        return True
    except Exception:
        return False


@pytest.mark.integration
def test_url_extraction_real_page():
    if not _online():
        pytest.skip("no network access in this environment")
    secs = extract_text("url", url="https://example.com/")
    joined = " ".join(s.text for s in secs)
    assert joined.strip()  # real text came back, via trafilatura or the httpx fallback
    assert all(s.page is None for s in secs)


@pytest.mark.integration
def test_url_extraction_bad_host_returns_empty_list():
    secs = extract_text("url", url="https://this-domain-should-not-exist.invalid/")
    assert secs == []
