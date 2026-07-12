import fitz
from app.brain.paginate import paginate_source
from app.models.knowledge import KnowledgeSource, Page


def _two_page_scanned_pdf() -> bytes:
    """A PDF with NO text layer — each page is a raster image, like the book."""
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page(width=612, height=792)
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 200))
        pix.clear_with(255)
        page.insert_image(fitz.Rect(0, 0, 612, 792), pixmap=pix)
    return doc.tobytes()


def test_pdf_yields_one_pending_page_per_physical_page_with_an_image(db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.brain.paginate.settings.media_dir", str(tmp_path))
    src = KnowledgeSource(type="pdf", title="Book", status="ingesting")
    db.add(src); db.commit()

    pages = paginate_source(db, src.id, kind="pdf", data=_two_page_scanned_pdf())

    assert [p.page_no for p in pages] == [1, 2]
    assert all(p.status == "pending" for p in pages)
    assert all(p.image_path for p in pages)
    for p in pages:
        assert (tmp_path / p.image_path).exists()          # the scan is really on disk
    assert db.query(Page).filter_by(source_id=src.id).count() == 2


def test_text_source_gets_exactly_one_ready_page_with_no_image(db):
    """Spec D2: non-paginated sources still get a Page, so nothing downstream
    has to branch on source.type."""
    src = KnowledgeSource(type="text", title="Note", status="ingesting")
    db.add(src); db.commit()

    pages = paginate_source(db, src.id, kind="text", text="hello tone")

    assert len(pages) == 1
    assert pages[0].page_no == 1
    assert pages[0].image_path is None
    assert pages[0].status == "ready"          # nothing to OCR
    assert "hello tone" in pages[0].text
