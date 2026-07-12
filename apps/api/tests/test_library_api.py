"""Integration tests for `routers/library.py` (Plan 9 Task 6): the OCR
enqueue endpoint, the page-level reader (`GET .../pages`, `GET .../pages/{n}`,
`GET /media/pages/{id}.jpg`), collections CRUD (+ D7's "delete unfiles,
never deletes"), `PATCH /knowledge/sources/{id}`, and the three-way retry
controller decision (pdf -> OCR job, url -> reingest job off the stored URL,
text -> 409).

Only hits the DB — no live LLM/embed/vision call anywhere in this module.
Mirrors `test_curriculum_generate_enqueue.py`'s skip-guard + `setup_module` +
module-level `TestClient` pattern (NOT a `client` fixture — this codebase has
no such fixture in `conftest.py`; every async-job test module builds its own
`TestClient(app)` at module scope).

KEY TEST GOTCHA (established, Plan 8): Starlette's `TestClient` runs
`BackgroundTasks` AFTER the response, in-process — every enqueue test below
monkeypatches `app.routers.library.run_ocr_job` / `run_reingest_job` to a
no-op, or a real multi-minute OCR/ingest run fires during the test run.
Patches the NAME AS BOUND INTO THE ROUTER MODULE (its own `from ... import`),
not the origin module in `app.jobs.runner` — patching the origin would not
affect the router's already-bound reference (same reasoning
`test_curriculum_generate_enqueue.py` documents for `run_curriculum_job`).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.generation_job import GenerationJob
from app.models.knowledge import Collection, KnowledgeSource, Page

# Skip cleanly (not error) when no DB is reachable — mirrors test_knowledge_router.py.
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


client = TestClient(app)


# --- OCR enqueue -----------------------------------------------------------

def test_ocr_enqueue_returns_202_and_a_job_id(db, monkeypatch):
    monkeypatch.setattr("app.routers.library.run_ocr_job", lambda job_id: None)
    src = KnowledgeSource(type="pdf", title="Book", status="ingesting")
    db.add(src); db.commit()

    r = client.post(f"/knowledge/sources/{src.id}/ocr")

    assert r.status_code == 202
    assert uuid.UUID(r.json()["job_id"])
    job = db.get(GenerationJob, uuid.UUID(r.json()["job_id"]))
    assert job.kind == "ocr"
    assert job.params["source_id"] == str(src.id)


def test_ocr_enqueue_404s_for_unknown_source(db, monkeypatch):
    monkeypatch.setattr("app.routers.library.run_ocr_job", lambda job_id: None)
    assert client.post(f"/knowledge/sources/{uuid.uuid4()}/ocr").status_code == 404


# --- Reader: page list + single page ---------------------------------------

def test_pages_endpoint_lists_page_status_for_the_reader(db):
    src = KnowledgeSource(type="pdf", title="Book", status="ingesting")
    db.add(src); db.commit()
    db.add(Page(source_id=src.id, page_no=1, status="ready", text="hi",
                image_path=f"{src.id}/0001.jpg"))
    db.add(Page(source_id=src.id, page_no=2, status="failed"))
    db.commit()

    r = client.get(f"/knowledge/sources/{src.id}/pages")

    assert r.status_code == 200
    assert r.json() == [{"page_no": 1, "status": "ready"},
                        {"page_no": 2, "status": "failed"}]


def test_single_page_returns_text_and_image_url(db):
    src = KnowledgeSource(type="pdf", title="Book", status="ready")
    db.add(src); db.commit()
    page = Page(source_id=src.id, page_no=47, status="ready",
                text="The Tube Screamer...", image_path=f"{src.id}/0047.jpg")
    db.add(page); db.commit()

    r = client.get(f"/knowledge/sources/{src.id}/pages/47")

    body = r.json()
    assert body["text"] == "The Tube Screamer..."
    assert body["image_url"] == f"/media/pages/{page.id}.jpg"
    assert body["total_pages"] == 1


def test_missing_page_is_404(db):
    src = KnowledgeSource(type="pdf", title="Book", status="ready")
    db.add(src); db.commit()
    assert client.get(f"/knowledge/sources/{src.id}/pages/99").status_code == 404


def test_pages_endpoints_404_for_unknown_source(db):
    unknown = uuid.uuid4()
    assert client.get(f"/knowledge/sources/{unknown}/pages").status_code == 404
    assert client.get(f"/knowledge/sources/{unknown}/pages/1").status_code == 404


# --- Media -------------------------------------------------------------

def test_media_endpoint_serves_the_scan_bytes(db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.library.settings.media_dir", str(tmp_path))
    src = KnowledgeSource(type="pdf", title="Book", status="ready")
    db.add(src); db.commit()
    page_dir = tmp_path / str(src.id)
    page_dir.mkdir()
    (page_dir / "0001.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    page = Page(source_id=src.id, page_no=1, status="ready",
                image_path=f"{src.id}/0001.jpg")
    db.add(page); db.commit()

    r = client.get(f"/media/pages/{page.id}.jpg")

    assert r.status_code == 200
    assert r.content == b"\xff\xd8\xff\xe0fakejpeg"
    assert r.headers["content-type"] == "image/jpeg"


def test_media_endpoint_404s_when_file_missing_on_disk(db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.library.settings.media_dir", str(tmp_path))
    src = KnowledgeSource(type="pdf", title="Book", status="ready")
    db.add(src); db.commit()
    page = Page(source_id=src.id, page_no=1, status="failed",
                image_path=f"{src.id}/0001.jpg")  # no file actually written
    db.add(page); db.commit()

    assert client.get(f"/media/pages/{page.id}.jpg").status_code == 404


def test_media_endpoint_404s_when_page_has_no_image_path(db):
    src = KnowledgeSource(type="url", title="A page", status="ready")
    db.add(src); db.commit()
    page = Page(source_id=src.id, page_no=1, status="ready", image_path=None)
    db.add(page); db.commit()

    assert client.get(f"/media/pages/{page.id}.jpg").status_code == 404


def test_media_endpoint_404s_for_unknown_page(db):
    assert client.get(f"/media/pages/{uuid.uuid4()}.jpg").status_code == 404


# --- Collections CRUD + D7 (delete unfiles, never deletes) -----------------

def test_collections_crud_and_moving_a_source(db):
    r = client.post("/library/collections", json={"name": "Tone & Gear"})
    assert r.status_code == 201
    col_id = r.json()["id"]

    src = KnowledgeSource(type="pdf", title="Book", status="ready")
    db.add(src); db.commit()

    r = client.patch(f"/knowledge/sources/{src.id}", json={"collection_id": col_id})
    assert r.status_code == 200
    db.expire_all()  # the API call committed on its OWN session (Depends(get_db))
    assert str(db.get(KnowledgeSource, src.id).collection_id) == col_id

    listing = client.get("/library/collections").json()
    assert listing[0]["name"] == "Tone & Gear"
    assert listing[0]["source_count"] == 1


def test_rename_collection(db):
    col = Collection(name="Old Name"); db.add(col); db.commit()

    r = client.patch(f"/library/collections/{col.id}", json={"name": "New Name"})

    assert r.status_code == 200
    assert r.json()["name"] == "New Name"
    db.expire_all()  # the API call committed on its OWN session (Depends(get_db))
    assert db.get(Collection, col.id).name == "New Name"


def test_patch_source_can_move_a_source_back_to_unfiled(db):
    col = Collection(name="Temp Folder"); db.add(col); db.commit()
    src = KnowledgeSource(type="pdf", title="Book", status="ready", collection_id=col.id)
    db.add(src); db.commit()

    r = client.patch(f"/knowledge/sources/{src.id}", json={"collection_id": None})

    assert r.status_code == 200
    db.expire_all()
    assert db.get(KnowledgeSource, src.id).collection_id is None


# --- Review fix (Finding 2): an unknown collection_id used to reach
# `source.collection_id = payload.collection_id` -> `db.commit()` with no
# existence check, and blow up as a raw 500 IntegrityError (FK violation) —
# e.g. the collection was deleted in one tab while moved-to in another.
# Mirrors the existence-check-before-assignment precedent already established
# by `students.py::upsert_student_progress` and `curriculum.py::update_block`.
# `collection_id: None` stays valid (it means "Unfiled") — only an unknown,
# non-null id should 404. ---------------------------------------------------

def test_patch_source_unknown_collection_id_is_404_not_500(db):
    src = KnowledgeSource(type="pdf", title="Book", status="ready")
    db.add(src); db.commit()

    r = client.patch(
        f"/knowledge/sources/{src.id}", json={"collection_id": str(uuid.uuid4())}
    )

    assert r.status_code == 404
    db.expire_all()
    assert db.get(KnowledgeSource, src.id).collection_id is None  # untouched


def test_deleting_a_collection_unfiles_its_sources_rather_than_deleting_them(db):
    """A folder is a label, not a container — deleting it must never destroy
    the tutor's material."""
    col = Collection(name="Temp"); db.add(col); db.commit()
    src = KnowledgeSource(type="pdf", title="Book", status="ready", collection_id=col.id)
    db.add(src); db.commit()

    assert client.delete(f"/library/collections/{col.id}").status_code == 204

    db.expire_all()
    kept = db.get(KnowledgeSource, src.id)
    assert kept is not None            # the source SURVIVES
    assert kept.collection_id is None  # it is merely Unfiled now


def test_deleting_an_unknown_collection_is_404(db):
    assert client.delete(f"/library/collections/{uuid.uuid4()}").status_code == 404


# --- Retry: the three-way controller decision -------------------------------

def test_retry_on_a_pdf_source_enqueues_an_ocr_job(db, monkeypatch):
    monkeypatch.setattr("app.routers.library.run_ocr_job", lambda job_id: None)
    src = KnowledgeSource(type="pdf", title="Book", status="failed")
    db.add(src); db.commit()

    r = client.post(f"/knowledge/sources/{src.id}/retry")

    assert r.status_code == 202
    job = db.get(GenerationJob, uuid.UUID(r.json()["job_id"]))
    assert job.kind == "ocr"
    assert job.params["source_id"] == str(src.id)


def test_retry_on_a_url_source_reingests_the_stored_url(db, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "app.routers.library.run_reingest_job",
        lambda job_id: captured.setdefault("job_id", job_id),
    )
    src = KnowledgeSource(type="url", title="Tone Tips", status="failed",
                          url="https://example.com/tone-tips")
    db.add(src); db.commit()

    r = client.post(f"/knowledge/sources/{src.id}/retry")

    assert r.status_code == 202
    job = db.get(GenerationJob, uuid.UUID(r.json()["job_id"]))
    assert job.kind == "reingest"
    assert job.params["source_id"] == str(src.id)


def test_retry_on_a_text_source_is_409_not_a_silent_noop(db):
    src = KnowledgeSource(type="text", title="Pasted notes", status="ready")
    db.add(src); db.commit()

    r = client.post(f"/knowledge/sources/{src.id}/retry")

    assert r.status_code == 409
    assert "no original" in r.json()["detail"].lower() or "nothing to retry" in r.json()["detail"].lower()


def test_retry_on_unknown_source_is_404(db):
    assert client.post(f"/knowledge/sources/{uuid.uuid4()}/retry").status_code == 404


# --- Reader / retry runner ---------------------------------------------

def test_run_reingest_job_refetches_the_stored_url(db, monkeypatch):
    """Unit-level check of the runner itself (not just that the router
    schedules it): `run_reingest_job` must call `ingest_source` with the
    URL persisted on the row, not anything supplied at retry time — the
    whole point of storing `KnowledgeSource.url` (Task 1) is that retry needs
    no fresh input from the caller."""
    from app.jobs.runner import run_reingest_job

    src = KnowledgeSource(type="url", title="Tone Tips", status="failed",
                          url="https://example.com/tone-tips")
    db.add(src); db.commit()
    job = GenerationJob(kind="reingest", status="pending", params={"source_id": str(src.id)})
    db.add(job); db.commit()

    captured = {}

    def _fake_ingest_source(db_, source_id, payload):
        captured["source_id"] = source_id
        captured["url"] = payload.url
        captured["kind"] = payload.kind
        source = db_.get(KnowledgeSource, source_id)
        source.status = "ready"
        db_.commit()

    monkeypatch.setattr("app.jobs.runner.ingest_source", _fake_ingest_source)

    run_reingest_job(job.id)

    assert captured["url"] == "https://example.com/tone-tips"
    assert captured["kind"] == "url"
    db.expire_all()  # run_reingest_job commits on its OWN SessionLocal()
    reloaded_job = db.get(GenerationJob, job.id)
    assert reloaded_job.status == "succeeded"
