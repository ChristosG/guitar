import fitz
from app.brain.paginate import paginate_source
from app.models.knowledge import Chunk, KnowledgeSource, Page


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


def test_repaginating_a_pdf_source_replaces_pages_instead_of_duplicating(db, tmp_path, monkeypatch):
    """Task 6's retry-a-failed-ingest path re-runs paginate_source on an
    existing source. It must REPLACE the Page rows, not duplicate them —
    otherwise every retry doubles the Page count and corrupts page numbering."""
    monkeypatch.setattr("app.brain.paginate.settings.media_dir", str(tmp_path))
    src = KnowledgeSource(type="pdf", title="Book", status="ingesting")
    db.add(src); db.commit()

    first = paginate_source(db, src.id, kind="pdf", data=_two_page_scanned_pdf())
    second = paginate_source(db, src.id, kind="pdf", data=_two_page_scanned_pdf())

    assert [p.page_no for p in second] == [1, 2]
    rows = db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()
    assert [p.page_no for p in rows] == [1, 2]          # still N, no dupes
    assert len(rows) == 2


def test_repaginating_cascades_away_old_chunks(db, tmp_path, monkeypatch):
    """Chunk.page_id FKs to page.id ON DELETE CASCADE: re-paginating deletes
    the old Page rows, which must cascade-delete their Chunks too (the old
    chunks are stale — Task 4's OCR re-embeds against the fresh pages)."""
    monkeypatch.setattr("app.brain.paginate.settings.media_dir", str(tmp_path))
    src = KnowledgeSource(type="pdf", title="Book", status="ingesting")
    db.add(src); db.commit()

    first = paginate_source(db, src.id, kind="pdf", data=_two_page_scanned_pdf())
    chunk = Chunk(source_id=src.id, page_id=first[0].id, text="stale",
                  embedding=[0.0] * 2560)
    db.add(chunk); db.commit()
    chunk_id = chunk.id

    paginate_source(db, src.id, kind="pdf", data=_two_page_scanned_pdf())

    assert db.get(Chunk, chunk_id) is None


def test_repaginating_a_text_source_still_yields_exactly_one_page(db):
    src = KnowledgeSource(type="text", title="Note", status="ingesting")
    db.add(src); db.commit()

    paginate_source(db, src.id, kind="text", text="hello tone")
    paginate_source(db, src.id, kind="text", text="hello tone, again")

    rows = db.query(Page).filter_by(source_id=src.id).all()
    assert len(rows) == 1
    assert rows[0].page_no == 1
