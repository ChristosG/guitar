"""Pins the invariant this whole fix depends on: **a `status="ready"`
`KnowledgeSource` always has >= 1 `Page`.**

"Guitar Tone & Gear — Course Spine" broke this — it sat "ready" with 0 Pages
because it was ingested in Plan 7, before the Page model existed
(app/brain/repair.py heals that specific legacy shape). This module proves
it can't happen again for anything ingested through `ingest_source`, across
all three source kinds: `paginate_source` (which creates the Page row(s))
always runs BEFORE `ingest_source` can set `status="ready"` — see that
function's ordering in app/brain/ingest.py. `test_ingest_status.py::
test_real_content_still_becomes_ready_with_a_page` already covers kind="text";
this file adds kind="url" and kind="pdf" (with a text layer, so it reaches
"ready" synchronously without needing the async OCR job) to close the loop
across every kind ingest_source accepts.

Only hits the DB + a monkeypatched embed provider — no live LLM/embed
server, so this runs under `-m "not integration"`.
"""
import fitz

from app.brain import extract as extract_mod
from app.brain.ingest import IngestPayload, ingest_source
from app.config import settings
from app.models.knowledge import KnowledgeSource, Page


class _Provider:
    def embed(self, texts, *, is_query=False):
        return [[0.1] * settings.embed_dim for _ in texts]


def test_ready_text_source_always_has_at_least_one_page(db, monkeypatch):
    monkeypatch.setattr("app.brain.ingest.get_provider", lambda: _Provider())
    src = KnowledgeSource(type="text", title="Notes", status="ingesting")
    db.add(src); db.commit()

    ingest_source(db, src.id, IngestPayload(kind="text", text="Humbuckers cancel hum."))

    got = db.get(KnowledgeSource, src.id)
    assert got.status == "ready"
    pages = db.query(Page).filter_by(source_id=src.id).all()
    assert len(pages) >= 1


def test_ready_url_source_always_has_at_least_one_page(db, monkeypatch):
    monkeypatch.setattr("app.brain.ingest.get_provider", lambda: _Provider())
    fake_sections = [extract_mod.Section(heading=None, text="Real fetched tone content.", page=1)]
    # paginate_source's single-Page path fetches (its own bound `extract_text`,
    # app.brain.paginate module) — same double-patch precedent as
    # test_ingest.py::test_ingest_caps_total_extracted_text_at_max_ingest_chars.
    monkeypatch.setattr("app.brain.paginate.extract_text", lambda *a, **k: fake_sections)

    src = KnowledgeSource(type="url", title="Tone Tips", status="ingesting",
                          url="https://example.com/tone-tips")
    db.add(src); db.commit()

    ingest_source(db, src.id, IngestPayload(kind="url", url="https://example.com/tone-tips"))

    got = db.get(KnowledgeSource, src.id)
    assert got.status == "ready"
    pages = db.query(Page).filter_by(source_id=src.id).all()
    assert len(pages) >= 1


def test_ready_pdf_source_with_a_text_layer_always_has_at_least_one_page(db, tmp_path, monkeypatch):
    """A PDF page with an existing text layer is taken for free (no OCR job
    needed — see app/brain/paginate.py's `_paginate_pdf`), so this reaches
    "ready" synchronously inside ingest_source itself, same as text/url."""
    monkeypatch.setattr("app.brain.ingest.get_provider", lambda: _Provider())
    monkeypatch.setattr("app.brain.paginate.settings.media_dir", str(tmp_path))

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Tube Screamers clip asymmetrically.")
    pdf_bytes = doc.tobytes()
    doc.close()

    src = KnowledgeSource(type="pdf", title="Book", status="ingesting")
    db.add(src); db.commit()

    ingest_source(db, src.id, IngestPayload(kind="pdf", data=pdf_bytes))

    got = db.get(KnowledgeSource, src.id)
    assert got.status == "ready"
    pages = db.query(Page).filter_by(source_id=src.id).all()
    assert len(pages) >= 1
