"""Integration tests for the `/knowledge` HTTP routes: sources CRUD (incl. PDF
upload), search, and ask.

Every test here creates at least one KnowledgeSource through the live API,
which runs `ingest_source` synchronously against the real embed server — same
live DB+embed dependency as test_ingest.py/test_retrieve.py — so the whole
module is `@pytest.mark.integration`.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.knowledge import Chunk, KnowledgeSource

# Skip cleanly (not error) when no DB is reachable — mirrors test_ingest.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


pytestmark = pytest.mark.integration

client = TestClient(app)

_HUM_TEXT = (
    "A humbucker pickup cancels 60-cycle mains hum by combining two coils "
    "wound in opposite magnetic and electrical polarity, so hum picked up "
    "equally by both coils cancels while the string signal still adds "
    "constructively."
)


def _create_text_source(title: str = "Router Pickups") -> dict:
    r = client.post(
        "/knowledge/sources",
        json={"kind": "text", "title": title, "language": "en", "text": _HUM_TEXT},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_create_text_source_ingests_synchronously_and_returns_ready():
    body = _create_text_source()
    assert body["status"] == "ready"
    assert body["type"] == "text"
    assert body["char_count"] and body["char_count"] > 0
    assert body["error"] is None


def test_create_source_rejects_mismatched_kind_and_payload():
    # kind="text" with no `text` field must be rejected before ingestion runs.
    r = client.post("/knowledge/sources", json={"kind": "text", "title": "Bad"})
    assert r.status_code == 422


def _online() -> bool:
    import httpx

    try:
        httpx.get("https://example.com", timeout=3)
        return True
    except Exception:
        return False


def test_create_url_source_ingests_via_real_fetch():
    """The one `kind` POST /sources otherwise never exercises here — "text" and
    "pdf" (via upload) are covered by the other tests in this module. Mirrors
    test_extract.py's own real-URL test (same URL, same network-guard).
    """
    if not _online():
        pytest.skip("no network access in this environment")

    r = client.post(
        "/knowledge/sources",
        json={"kind": "url", "title": "Example Domain", "language": "en", "url": "https://example.com/"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "url"
    assert body["status"] == "ready"
    assert body["char_count"] and body["char_count"] > 0


def test_list_sources_includes_created_source():
    created = _create_text_source(title="Listed Source")
    r = client.get("/knowledge/sources")
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()]
    assert created["id"] in ids


def test_get_source_detail_returns_chunk_preview():
    created = _create_text_source(title="Detail Source")
    r = client.get(f"/knowledge/sources/{created['id']}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["id"] == created["id"]
    assert 1 <= len(detail["chunks"]) <= 5
    assert "humbucker" in detail["chunks"][0]["text"].lower()


def test_get_source_detail_404_for_unknown_id():
    r = client.get("/knowledge/sources/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_delete_source_cascades_chunks():
    created = _create_text_source(title="Delete Me")
    source_id = created["id"]

    r = client.delete(f"/knowledge/sources/{source_id}")
    assert r.status_code == 204

    assert client.get(f"/knowledge/sources/{source_id}").status_code == 404

    db = SessionLocal()
    try:
        remaining = db.scalars(select(Chunk).where(Chunk.source_id == source_id)).all()
        assert remaining == []
        assert db.get(KnowledgeSource, source_id) is None
    finally:
        db.close()


def test_delete_unknown_source_404s():
    r = client.delete("/knowledge/sources/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_search_endpoint_returns_ranked_hits():
    _create_text_source(title="Search Pickups")
    r = client.post("/knowledge/search", json={"query": "what removes hum?", "k": 3})
    assert r.status_code == 200
    hits = r.json()["hits"]
    assert hits
    assert "humbucker" in hits[0]["text"].lower()
    assert hits[0]["score"] >= hits[-1]["score"]


def test_ask_endpoint_returns_grounded_answer_with_citations():
    _create_text_source(title="Ask Pickups")
    r = client.post("/knowledge/ask", json={"query": "what is a humbucker?", "locale": "en"})
    assert r.status_code == 200
    body = r.json()
    assert body["text"].strip() != ""
    assert len(body["citations"]) >= 1


def test_upload_source_ingests_pdf():
    import fitz  # pymupdf; build a throwaway one-page PDF entirely in memory (mirrors test_extract.py)

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "A humbucker pickup cancels 60-cycle hum.")
    data = doc.tobytes()
    doc.close()

    r = client.post(
        "/knowledge/sources/upload",
        data={"title": "Uploaded PDF", "language": "en"},
        files={"file": ("pickups.pdf", data, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready"
    assert body["type"] == "pdf"
    assert body["char_count"] and body["char_count"] > 0
