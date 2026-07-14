from app.models.knowledge import EMBED_DIM, Chunk, Collection, KnowledgeSource, Page


def test_page_belongs_to_source_and_carries_scan_and_text(db):
    src = KnowledgeSource(type="pdf", title="Book", status="ingesting")
    db.add(src); db.commit()

    page = Page(source_id=src.id, page_no=47, image_path="pages/x.jpg",
                text="The Tube Screamer is...", status="ready")
    db.add(page); db.commit()

    got = db.get(Page, page.id)
    assert got.page_no == 47
    assert got.image_path == "pages/x.jpg"
    assert got.status == "ready"
    assert got.ocr_error is None


def test_chunk_links_to_its_page(db):
    src = KnowledgeSource(type="pdf", title="Book", status="ingesting")
    db.add(src); db.commit()
    page = Page(source_id=src.id, page_no=1, text="hi", status="ready")
    db.add(page); db.commit()

    chunk = Chunk(source_id=src.id, page_id=page.id, text="hi",
                  embedding=[0.0] * EMBED_DIM)
    db.add(chunk); db.commit()
    assert db.get(Chunk, chunk.id).page_id == page.id


def test_source_lives_in_at_most_one_collection(db):
    col = Collection(name="Tone & Gear")
    db.add(col); db.commit()
    src = KnowledgeSource(type="pdf", title="Book", status="ingesting",
                          collection_id=col.id)
    db.add(src); db.commit()
    assert db.get(KnowledgeSource, src.id).collection_id == col.id


def test_source_with_no_collection_is_unfiled(db):
    src = KnowledgeSource(type="text", title="Loose note", status="ready")
    db.add(src); db.commit()
    assert db.get(KnowledgeSource, src.id).collection_id is None


def test_url_source_round_trips_its_url(db):
    # Controller decision (Task 1): KnowledgeSource.url persists the original
    # URL so a later "retry a failed ingest" task can re-fetch it — today's
    # row otherwise has no durable record of where a url-kind source came from.
    src = KnowledgeSource(type="url", title="Tone Tips Blog", status="ready",
                          url="https://example.com/tone-tips")
    db.add(src); db.commit()
    assert db.get(KnowledgeSource, src.id).url == "https://example.com/tone-tips"
