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


def test_pdf_synthetic_multiline_heading_is_not_collapsed():
    """A heading that spans two consecutive lines of the same (large) font
    size must not collapse to just the last line — both lines belong to one
    heading, and the body text beneath it must still survive.

    Regression test for a silent-data-loss bug: the heading-detection loop
    used to flush-and-overwrite on *every* heading-classified line, so a
    same-run second heading line discarded the first line entirely (it was
    never appended to `heading` nor routed to `text`).
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "GETTING GREAT", fontsize=22)
    page.insert_text((72, 100), "GUITAR SOUNDS", fontsize=22)
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

    matches = [s for s in secs if s.heading and "GETTING GREAT" in s.heading]
    assert matches, f"expected a section with 'GETTING GREAT' in its heading, got: {secs}"
    assert "GUITAR SOUNDS" in matches[0].heading
    assert "60-cycle hum" in matches[0].text.lower()


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


def test_pdf_page_extraction_failure_is_isolated_to_that_page(monkeypatch):
    """One page raising during extraction (e.g. the unguarded plain-text
    fallback call for a page with no structured text spans) must not discard
    every other page's already-extracted sections — only that page is skipped.

    Regression test: previously the page loop had no per-page try/except, so
    an exception on any single page propagated to _extract_pdf's caller
    (extract_text's outer catch-all), which returned [] for the *whole*
    document, throwing away every other page's good extraction too.
    """
    import fitz

    doc = fitz.open()
    doc.new_page()  # page 1: blank -> hits the unguarded page.get_text() fallback
    doc.new_page().insert_text((72, 72), "Page two survives independently.")
    data = doc.tobytes()
    doc.close()

    real_get_text = fitz.Page.get_text

    def flaky_get_text(self, *args, **kwargs):
        if not args and not kwargs:  # the bare fallback call, only made for page 1 here
            raise RuntimeError("simulated failure in plain-text fallback")
        return real_get_text(self, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_text", flaky_get_text)

    secs = extract_text("pdf", data=data)
    joined = " ".join(s.text for s in secs).lower()
    assert "page two survives" in joined
    assert all(s.page != 1 for s in secs)  # page 1's failure was isolated, not fatal


def test_pdf_page_with_no_text_layer_yields_no_sections():
    """A raster-scanned page produces NOTHING at extract time — deliberately.

    The Tesseract-via-MuPDF fallback that used to fire here was
    host-dependent (nothing ships Tesseract) and hard-coded `language="eng"`,
    so a Greek scan on a machine that happened to have Tesseract came back as
    English-model garbage that no quality screen ever checked. The vision OCR
    job is the ONE reader of scanned pages; until the tutor presses "read",
    the page honestly has no text.
    """
    import fitz

    doc = fitz.open()
    doc.new_page()  # a page with no text layer at all (blank)
    data = doc.tobytes()
    doc.close()

    assert extract_text("pdf", data=data) == []


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
# page to a bitmap and recognizing it) — which is exactly the vision OCR
# job's whole job (`app.brain.ocr`), started by the tutor's "read" button,
# never by extraction. extract.py once had a Tesseract-via-MuPDF fallback
# here; it was deleted as host-dependent (`language="eng"`, screened by
# nothing) — see `_extract_pdf`'s comment.
#
# These tests verify what extract_text() can honestly promise for *this*
# file: it does not raise, does not hang, and degrades to `[]` per the
# "never raises on unextractable input" contract.


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
