"""C6 — compile at ingest, resumable and explicit.

The compile is a background job, sibling to OCR, behind the SAME
`SELECT ... FOR UPDATE` in-flight guard `_enqueue_ocr` uses: pressing compile
twice must yield ONE job, not two readings of the same book racing. It NEVER
auto-recompiles an already-compiled book — reading ten books is ~$13 of real
model time on the tutor's own subscription, and an app update that silently
re-spent it is the top severity class in this plan. The money guard lives in
`compile_book` (status="ready" returns without spending); this suite proves the
job and the enqueue honour it.

It also runs when a book's OCR completes — but only once the book is FULLY read
(no page still pending), so a run parked on a rate limit does not compile a half
book and then poison the money guard against the complete one.
"""
import uuid

import pytest

import app.canon.compile as compile_mod
import app.jobs.canon_compile as job_mod
from app.jobs.canon_compile import (
    active_compile_job,
    autocompile_after_ocr,
    enqueue_canon_compile,
    run_canon_compile_job,
)
from app.models.canon import BookCompile, Concept, ConceptClaim
from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource, Page


class _FakeProvider:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"concepts": []}
        self.calls = 0
        self.last_usage = {"usage": {"input_tokens": 23_000}}

    def guided_json(self, messages, schema, **kw):
        self.calls += 1
        return self.payload

    def count_tokens(self, text: str) -> int:
        return len(text) // 3 + 1


_PROSE = ("Alternate picking is the foundation of speed. Down, up, down, up — "
          "the pick never rests, and the wrist does the work, not the arm.")


def _concept(name="alternate picking"):
    return {"name": name, "name_el": "εναλλασσόμενη πενιά", "claims": [
        {"text": "The wrist does the work.", "pages": [12], "stance": "wrist not arm",
         "depth": "primary", "grounding": "author"}]}


def _book(db, *, pages=None, status="ready", title="Guitar Exercises"):
    source = KnowledgeSource(type="pdf", title=title, status=status)
    db.add(source)
    db.flush()
    for page_no, (text, pstatus) in (pages or {12: (_PROSE, "ready")}).items():
        db.add(Page(source_id=source.id, page_no=page_no, text=text, status=pstatus))
    db.commit()
    return source


def _use(monkeypatch, provider):
    monkeypatch.setattr(compile_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(compile_mod, "_model_name", lambda: "claude-sonnet-5")
    return provider


def _spy_reconcile(monkeypatch):
    calls = []
    monkeypatch.setattr(job_mod, "reconcile", lambda db: calls.append(True) or [])
    return calls


# ---------------------------------------------------------------------------
# The in-flight guard — one job, never two readings racing
# ---------------------------------------------------------------------------

def test_enqueue_twice_yields_ONE_job(db):
    source = _book(db)

    first_id, first_status = enqueue_canon_compile(db, source.id)
    second_id, second_status = enqueue_canon_compile(db, source.id)

    assert first_status == "enqueued"
    assert second_status == "already_running", "a second in-flight compile must be refused"
    assert second_id == first_id
    assert db.query(GenerationJob).filter_by(kind="canon_compile").count() == 1


def test_active_compile_job_finds_the_in_flight_one(db):
    source = _book(db)
    job = GenerationJob(kind="canon_compile", status="pending",
                        params={"source_id": str(source.id)})
    db.add(job)
    db.commit()

    assert active_compile_job(db, source.id).id == job.id


# ---------------------------------------------------------------------------
# The money guard — an already-compiled book is never re-billed
# ---------------------------------------------------------------------------

def test_an_already_compiled_book_is_not_enqueued(db):
    source = _book(db)
    db.add(BookCompile(source_id=source.id, status="ready", model="claude-sonnet-5",
                       concept_count=1))
    db.commit()

    job_id, status = enqueue_canon_compile(db, source.id)

    assert status == "already_compiled"
    assert job_id is None
    assert db.query(GenerationJob).filter_by(kind="canon_compile").count() == 0


def test_running_the_job_on_a_ready_book_spends_nothing_and_does_not_reconcile(db, monkeypatch):
    source = _book(db)
    db.add(BookCompile(source_id=source.id, status="ready", model="claude-sonnet-5",
                       concept_count=1))
    db.commit()
    fake = _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))
    reconciled = _spy_reconcile(monkeypatch)

    job = GenerationJob(kind="canon_compile", status="pending",
                        params={"source_id": str(source.id)})
    db.add(job)
    db.commit()
    run_canon_compile_job(job.id)

    assert fake.calls == 0, "a compiled book was re-read — that is real money"
    assert reconciled == [], "an already-compiled book must not trigger a reconcile call"


# ---------------------------------------------------------------------------
# A fresh compile does the work, and reconciles the new book against the canon
# ---------------------------------------------------------------------------

def test_the_job_compiles_a_fresh_book_and_records_success(db, monkeypatch):
    source = _book(db)
    fake = _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))
    _spy_reconcile(monkeypatch)

    job = GenerationJob(kind="canon_compile", status="pending",
                        params={"source_id": str(source.id)})
    db.add(job)
    db.commit()
    run_canon_compile_job(job.id)

    db.expire_all()
    assert fake.calls == 1
    assert db.get(BookCompile, source.id).status == "ready"
    assert db.query(ConceptClaim).count() == 1
    assert db.get(GenerationJob, job.id).status == "succeeded"


def test_a_fresh_compile_reconciles_against_the_existing_canon(db, monkeypatch):
    """Reconcile is library-wide and it runs after a fresh compile: a new book's
    concepts must merge against the ones already in the canon."""
    source = _book(db)
    _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))
    reconciled = _spy_reconcile(monkeypatch)

    job = GenerationJob(kind="canon_compile", status="pending",
                        params={"source_id": str(source.id)})
    db.add(job)
    db.commit()
    run_canon_compile_job(job.id)

    assert reconciled == [True], "a fresh compile must reconcile the new book in"


def test_a_book_with_no_readable_text_does_not_reconcile_and_costs_nothing(db, monkeypatch):
    source = _book(db, pages={1: ("", "empty")})   # no readable text
    fake = _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))
    reconciled = _spy_reconcile(monkeypatch)

    job = GenerationJob(kind="canon_compile", status="pending",
                        params={"source_id": str(source.id)})
    db.add(job)
    db.commit()
    run_canon_compile_job(job.id)

    db.expire_all()
    assert fake.calls == 0, "an empty book must never reach the model"
    assert reconciled == [], "nothing was compiled, so nothing to reconcile"
    assert db.get(GenerationJob, job.id).status == "succeeded"


# ---------------------------------------------------------------------------
# The OCR-completion trigger — only when the book is fully read
# ---------------------------------------------------------------------------

def test_autocompile_fires_when_the_book_is_fully_ocrd(db):
    source = _book(db, pages={12: (_PROSE, "ready"), 13: (_PROSE, "ready")})

    job_id = autocompile_after_ocr(db, source.id)

    assert job_id is not None, "a fully-read, uncompiled book must auto-enqueue a compile"
    assert db.query(GenerationJob).filter_by(kind="canon_compile").count() == 1


def test_autocompile_does_NOT_fire_while_pages_are_still_pending(db):
    """A run parked on a rate limit leaves pages pending. Compiling then would
    read a half book and poison the money guard against the complete one."""
    source = _book(db, pages={12: (_PROSE, "ready"), 13: ("", "pending")})

    assert autocompile_after_ocr(db, source.id) is None
    assert db.query(GenerationJob).filter_by(kind="canon_compile").count() == 0


def test_autocompile_does_NOT_refire_on_an_already_compiled_book(db):
    source = _book(db, pages={12: (_PROSE, "ready")})
    db.add(BookCompile(source_id=source.id, status="ready", model="claude-sonnet-5"))
    db.commit()

    assert autocompile_after_ocr(db, source.id) is None
    assert db.query(GenerationJob).filter_by(kind="canon_compile").count() == 0


# ---------------------------------------------------------------------------
# The endpoint — pressing compile twice yields one job
# ---------------------------------------------------------------------------

def test_pressing_compile_twice_via_the_endpoint_yields_one_job(db, client, monkeypatch):
    monkeypatch.setattr("app.routers.library.run_canon_compile_job", lambda job_id: None)
    source = _book(db)

    first = client.post(f"/knowledge/sources/{source.id}/compile").json()
    second = client.post(f"/knowledge/sources/{source.id}/compile").json()

    assert second["job_id"] == first["job_id"]
    assert second["already_running"] is True
    assert db.query(GenerationJob).filter_by(kind="canon_compile").count() == 1


def test_pressing_compile_on_a_ready_book_starts_no_job(db, client, monkeypatch):
    monkeypatch.setattr("app.routers.library.run_canon_compile_job", lambda job_id: None)
    source = _book(db)
    db.add(BookCompile(source_id=source.id, status="ready", model="claude-sonnet-5"))
    db.commit()

    resp = client.post(f"/knowledge/sources/{source.id}/compile").json()

    assert resp.get("already_compiled") is True
    assert db.query(GenerationJob).filter_by(kind="canon_compile").count() == 0


# ---------------------------------------------------------------------------
# Force-recompile — the explicit "recompile on purpose" override
# ---------------------------------------------------------------------------

def test_force_recompiles_an_already_compiled_book_via_the_endpoint(db, client, monkeypatch):
    """`?force=true` is the tutor deliberately recompiling: pressing it on a book
    already in the canon DOES start a job (bypassing the money guard), where a plain
    press returns `already_compiled` and starts nothing. The `force` flag is carried
    onto the job so the runner re-reads rather than no-opping in `compile_book`."""
    ran = []
    monkeypatch.setattr("app.routers.library.run_canon_compile_job",
                        lambda job_id: ran.append(job_id))
    source = _book(db)
    db.add(BookCompile(source_id=source.id, status="ready", model="claude-sonnet-5"))
    db.commit()

    resp = client.post(f"/knowledge/sources/{source.id}/compile?force=true").json()

    assert "already_compiled" not in resp, "force must bypass the money guard"
    assert resp["already_running"] is False
    job_id = uuid.UUID(resp["job_id"])
    job = db.get(GenerationJob, job_id)
    assert job.kind == "canon_compile"
    assert job.params["force"] is True            # carried through to the runner
    assert ran == [job_id]                          # the (monkeypatched) runner was scheduled


def test_running_a_forced_job_re_reads_a_ready_book_and_reconciles(db, monkeypatch):
    """force=True in the job params makes the runner call compile_book(force=True),
    which re-reads an already-ready book and REPLACES its ledger — and, because its
    concepts changed, reconciles it back into the canon (unlike an unforced no-op on
    a ready book, which spends nothing and does not reconcile)."""
    source = _book(db)
    db.add(BookCompile(source_id=source.id, status="ready", model="claude-sonnet-5",
                       concept_count=1))
    db.commit()
    fake = _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))
    reconciled = _spy_reconcile(monkeypatch)

    job = GenerationJob(kind="canon_compile", status="pending",
                        params={"source_id": str(source.id), "force": True})
    db.add(job)
    db.commit()
    run_canon_compile_job(job.id)

    db.expire_all()   # the runner committed on its own session
    assert fake.calls == 1, "force must actually re-read the book"
    assert reconciled == [True], "a forced recompile changed the canon — it must reconcile"
    assert db.get(GenerationJob, job.id).status == "succeeded"
