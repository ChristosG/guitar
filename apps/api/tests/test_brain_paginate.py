"""paginate must not inherit someone else's OCR.

Before: `layer = page.get_text(); status = "ready" if layer else "pending"`.
Any text at all meant no model would ever see the page again — so Hunter,
Gallagher and Kahn entered the library as Tesseract output, fraction glyphs
and all.
"""
from unittest.mock import patch

from app.brain.paginate import _paginate_pdf


def _fake_doc(kinds: list[str]):
    """A doc whose pages report the given text_layer_kind."""
    class FakePage:
        def __init__(self, kind):
            self.kind = kind
        def get_pixmap(self, dpi=110):
            class Pix:
                def tobytes(self, fmt): return b"\xff\xd8jpeg"
            return Pix()
        def get_text(self, kind="text", **kw):
            return "" if self.kind == "none" else "some text on the page"
    class FakeDoc:
        page_count = len(kinds)
        def __init__(self): self._pages = [FakePage(k) for k in kinds]
        def __getitem__(self, i): return self._pages[i]
        def close(self): pass
    return FakeDoc()


def test_digital_text_is_taken_for_free(db, tmp_media):
    doc = _fake_doc(["digital"])
    with patch("app.brain.paginate.fitz.open", return_value=doc), \
         patch("app.brain.paginate.text_layer_kind", return_value="digital"):
        pages = _paginate_pdf(db, tmp_media.source_id, b"%PDF")
    assert pages[0].status == "ready"
    assert pages[0].text_source == "text_layer"
    assert pages[0].ocr_reason is None


def test_inherited_ocr_is_NOT_taken_for_free(db, tmp_media):
    """The Hunter/Gallagher/Kahn case. A GlyphLessFont layer is someone else's
    Tesseract; we re-OCR rather than adopt its 100% fraction loss."""
    doc = _fake_doc(["ocr"])
    with patch("app.brain.paginate.fitz.open", return_value=doc), \
         patch("app.brain.paginate.text_layer_kind", return_value="ocr"):
        pages = _paginate_pdf(db, tmp_media.source_id, b"%PDF")
    assert pages[0].status == "pending"
    assert pages[0].ocr_reason == "inherited_ocr"
    assert pages[0].text is None, "the inherited layer must not be kept as truth"


def test_no_text_layer_still_goes_to_vision(db, tmp_media):
    doc = _fake_doc(["none"])
    with patch("app.brain.paginate.fitz.open", return_value=doc), \
         patch("app.brain.paginate.text_layer_kind", return_value="none"):
        pages = _paginate_pdf(db, tmp_media.source_id, b"%PDF")
    assert pages[0].status == "pending"
    assert pages[0].ocr_reason == "no_text_layer"
