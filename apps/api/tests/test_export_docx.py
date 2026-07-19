import io

from docx import Document
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models.block import Block

client = TestClient(app)


def _mk_tree(db):
    course = Block(kind="course", title="Ήχος και Ενισχυτές", is_template=True,
                   order=0, plane="content", target_profile={"level": "μεσαίο"})
    db.add(course); db.flush()
    m1 = Block(kind="module", title="Βασικές αρχές ήχου", parent_id=course.id, order=0, plane="content")
    db.add(m1); db.flush()
    l1 = Block(kind="lesson", title="Τι είναι το gain", parent_id=m1.id, order=0,
               plane="content", est_minutes=55,
               meta={"draft_status": "ready",
                     "citations": [{"source_id": "s1", "source_ref": "b1",
                                    "source_title": "Getting Great Guitar Sounds", "page": 19}]})
    db.add(l1); db.flush()
    db.add(Block(kind="segment", title="Θεωρία", parent_id=l1.id, order=0, plane="content",
                 body="Το gain καθορίζει την προενίσχυση του σήματος.",
                 meta={"section": "theory"}))
    db.add(Block(kind="segment", title="Ασκήσεις", parent_id=l1.id, order=1, plane="content",
                 body="Άσκηση 1: σύγκρινε clean και overdriven ήχο.",
                 meta={"section": "exercises"}))
    l2 = Block(kind="lesson", title="Ακόμα γράφεται", parent_id=m1.id, order=1,
               plane="content", meta={"draft_status": "queued"})
    db.add(l2); db.commit()
    return course


def test_export_docx_structure_and_greek_content():
    db = SessionLocal()
    try:
        course = _mk_tree(db)
        r = client.get(f"/curricula/{course.id}/export.docx")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert "attachment" in r.headers["content-disposition"]

        doc = Document(io.BytesIO(r.content))
        headings = [(p.style.name, p.text) for p in doc.paragraphs if p.style.name.startswith("Heading")]
        texts = [p.text for p in doc.paragraphs]

        assert ("Heading 1", "Βασικές αρχές ήχου") in headings
        assert any(s == "Heading 2" and "Τι είναι το gain" in t for s, t in headings)
        assert ("Heading 3", "Θεωρία") in headings
        assert ("Heading 3", "Ασκήσεις") in headings
        assert "Το gain καθορίζει την προενίσχυση του σήματος." in texts
        # heading ORDER: module before its lesson before its sections
        h_texts = [t for _, t in headings]
        assert h_texts.index("Βασικές αρχές ήχου") < h_texts.index(next(t for t in h_texts if "Τι είναι το gain" in t))

        # spec §4: NO sources anywhere in the document
        joined = "\n".join(texts)
        assert "Getting Great Guitar Sounds" not in joined
        assert "Πηγές" not in joined

        # undrafted lesson: present with a placeholder, not silently missing
        assert any("Ακόμα γράφεται" in t for t in h_texts)
    finally:
        db.close()


def test_export_docx_404_for_non_roots():
    db = SessionLocal()
    try:
        course = _mk_tree(db)
        module_id = next(b.id for b in course.children)
        assert client.get(f"/curricula/{module_id}/export.docx").status_code == 404
    finally:
        db.close()
