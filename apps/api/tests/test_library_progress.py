"""Stage 7.2 — the OCR progress the tutor sees must be a fact about the SERVER,
not about the tab that happened to press the button.

The bug these pin: progress lived only in `library/page.tsx`'s `ocrProgress`
React state. Hard-reload during the tutor's 9-minute, 77-page OCR and the row
fell back to the source's at-rest status — `empty` — so his book rendered RED,
"nothing was read from this source", WITH A RETRY BUTTON, while it was being
read. Pressing that button enqueued a SECOND job racing the first, both doing
delete-then-insert of the same page's chunks.
"""
import uuid

from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource, Page


def _book(db, *, pages=77, ready=0, failed=0, empty=0, status="ingesting"):
    src = KnowledgeSource(type="pdf", title="Getting Great Guitar Sounds", status=status)
    db.add(src)
    db.commit()
    for i in range(1, pages + 1):
        if i <= ready:
            st, text = "ready", "x" * 100
        elif i <= ready + failed:
            st, text = "failed", None
        elif i <= ready + failed + empty:
            st, text = "empty", None
        else:
            st, text = "pending", None
        db.add(Page(source_id=src.id, page_no=i, image_path=f"{src.id}/{i:04d}.jpg",
                    status=st, text=text))
    db.commit()
    return src


def _running_job(db, source_id) -> GenerationJob:
    job = GenerationJob(kind="ocr", status="running", params={"source_id": str(source_id)})
    db.add(job)
    db.commit()
    return job


def test_progress_survives_a_reload_because_it_is_computed_from_page_rows(db, client):
    """THE F5 TEST. A brand-new client with no React state at all asks the server
    where the OCR is, and gets "page 30 of 77" — not "empty, nothing was read"."""
    src = _book(db, pages=77, ready=29)
    _running_job(db, src.id)

    r = client.get(f"/knowledge/sources/{src.id}/progress")
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is True
    assert body["total"] == 77
    assert body["ready"] == 29
    assert body["current_page"] == 30       # resolved + 1 — the page it's on now


def test_progress_is_inactive_with_no_current_page_when_nothing_is_running(db, client):
    src = _book(db, pages=3, ready=3, status="ready")
    body = client.get(f"/knowledge/sources/{src.id}/progress").json()
    assert body["active"] is False
    assert body["job_id"] is None
    assert body["current_page"] is None     # a finished book is not "on" a page


def test_current_page_never_exceeds_the_total(db, client):
    """The last page's own completion must not read "page 78 of 77"."""
    src = _book(db, pages=5, ready=5)
    _running_job(db, src.id)
    assert client.get(f"/knowledge/sources/{src.id}/progress").json()["current_page"] == 5


def test_progress_404s_for_an_unknown_source(client):
    assert client.get(f"/knowledge/sources/{uuid.uuid4()}/progress").status_code == 404


def test_the_source_list_carries_page_counts_and_a_live_ocr_flag(db, client):
    """A freshly loaded Library tab must be able to tell, from the LIST alone,
    that a book is mid-OCR — that is what makes the reload honest before any
    per-source poll has even fired."""
    src = _book(db, pages=77, ready=71, failed=6, status="partial")
    _running_job(db, src.id)

    row = next(s for s in client.get("/knowledge/sources").json() if s["id"] == str(src.id))
    assert row["pages_total"] == 77
    assert row["pages_ready"] == 71
    assert row["pages_failed"] == 6
    assert row["ocr_active"] is True


def test_a_source_with_no_in_flight_job_is_not_flagged_active(db, client):
    src = _book(db, pages=2, ready=2, status="ready")
    # A SUCCEEDED job is not in flight — only pending/running are.
    db.add(GenerationJob(kind="ocr", status="succeeded",
                         params={"source_id": str(src.id)}))
    db.commit()
    row = next(s for s in client.get("/knowledge/sources").json() if s["id"] == str(src.id))
    assert row["ocr_active"] is False


# --- the in-flight guard ---------------------------------------------------

def test_starting_ocr_twice_does_not_start_two_jobs(db, client, monkeypatch):
    """Click Retry twice, fast -> exactly ONE running job.

    Before the guard, `POST /ocr` enqueued unconditionally: two jobs, both
    running `ocr_source` over the same book, each deleting and re-inserting the
    same page's chunks. The second call now returns the FIRST job's id.
    """
    # BackgroundTasks would otherwise actually run the OCR (against a real
    # provider) inside TestClient's response cycle.
    ran = []
    monkeypatch.setattr("app.routers.library.run_ocr_job", lambda job_id: ran.append(job_id))

    src = _book(db, pages=3)

    first = client.post(f"/knowledge/sources/{src.id}/ocr").json()
    second = client.post(f"/knowledge/sources/{src.id}/ocr").json()

    assert first["already_running"] is False
    assert second["already_running"] is True
    assert second["job_id"] == first["job_id"]
    assert db.query(GenerationJob).filter_by(kind="ocr").count() == 1


def test_retry_on_a_pdf_mid_ocr_joins_the_running_job_instead_of_racing_it(db, client, monkeypatch):
    """The precise sequence the tutor hit: OCR is running, the (stale) row offers
    a Retry, he presses it. That must not start a second reader of the same book."""
    monkeypatch.setattr("app.routers.library.run_ocr_job", lambda job_id: None)

    src = _book(db, pages=77, ready=30)
    running = _running_job(db, src.id)

    body = client.post(f"/knowledge/sources/{src.id}/retry").json()
    assert body["job_id"] == str(running.id)
    assert db.query(GenerationJob).filter_by(kind="ocr").count() == 1
