from app.models.knowledge import KnowledgeSource, Page


def test_selection_is_captured_with_its_page_provenance(client, db):
    src = KnowledgeSource(type="pdf", title="Getting Great Guitar Sounds", status="ready")
    db.add(src); db.commit()
    db.add(Page(source_id=src.id, page_no=47, status="ready", text="The Tube Screamer..."))
    db.commit()

    r = client.post("/lessons/from-selection", json={
        "source_id": str(src.id), "page_no": 47,
        "text": "The Tube Screamer is not really a distortion box",
    })

    assert r.status_code == 201
    body = r.json()
    # provenance travels WITH the selection — sub-project B grounds the lesson
    # it drafts in exactly this passage, and can cite the page it came from
    assert body["source_title"] == "Getting Great Guitar Sounds"
    assert body["page_no"] == 47
    assert "Tube Screamer" in body["text"]


def test_selection_from_an_unknown_source_is_404(client):
    import uuid
    r = client.post("/lessons/from-selection", json={
        "source_id": str(uuid.uuid4()), "page_no": 1, "text": "x"})
    assert r.status_code == 404


def test_empty_selection_is_rejected(client, db):
    src = KnowledgeSource(type="pdf", title="Book", status="ready")
    db.add(src); db.commit()
    r = client.post("/lessons/from-selection", json={
        "source_id": str(src.id), "page_no": 1, "text": "   "})
    assert r.status_code == 422
