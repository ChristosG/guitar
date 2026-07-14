import pytest
from sqlalchemy import select, text

import app.brain.extract as extract_mod
from app.brain.ingest import IngestPayload, ingest_source
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models.knowledge import EMBED_DIM, Chunk, KnowledgeSource

# Skip cleanly (not error) when no DB is reachable — mirrors test_models_roundtrip.py.
# Every test below needs the DB (to create the KnowledgeSource row and read the
# outcome back); the happy-path test additionally needs the live embed server —
# per this task's instructions both are grouped under @pytest.mark.integration.
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


_THREE_PARAGRAPHS = (
    "A humbucker cancels 60-cycle mains hum by using two coils wound in opposite "
    "electrical and magnetic polarity: hum picked up equally by both coils cancels "
    "out, while the guitar string signal itself still adds constructively.\n\n"
    "Single-coil pickups, by contrast, are more susceptible to that hum but are "
    "prized by many players for a brighter, more articulate top end — the classic "
    "sound most associated with a Fender Stratocaster.\n\n"
    "A guitar's tone control is just a variable low-pass filter wired across the "
    "pickup: turning the knob down routes progressively more high-frequency signal "
    "through a capacitor to ground, which darkens the sound without changing volume."
)


def _make_source(db, title: str, *, status: str = "ingesting") -> "KnowledgeSource":
    source = KnowledgeSource(type="text", title=title, status=status, language="en")
    db.add(source)
    db.commit()
    return source


@pytest.mark.integration
def test_ingest_text_source_stores_real_embeddings_and_char_count():
    db = SessionLocal()
    try:
        source_id = _make_source(db, "Tone basics").id
        ingest_source(db, source_id, IngestPayload(kind="text", text=_THREE_PARAGRAPHS))
    finally:
        db.close()

    # Fresh session so this is a genuine DB round-trip, not the identity-map
    # object reused under expire_on_commit=False (mirrors test_models_roundtrip.py).
    db2 = SessionLocal()
    try:
        got = db2.get(KnowledgeSource, source_id)
        assert got is not None
        assert got.status == "ready"
        assert got.error is None
        assert got.char_count is not None and got.char_count > 0

        chunks = db2.scalars(select(Chunk).where(Chunk.source_id == source_id)).all()
        assert len(chunks) >= 1
        for c in chunks:
            assert len(c.embedding) == EMBED_DIM  # 384, real local-e5 embeddings
        # char_count is exactly the sum of the persisted chunks' text lengths.
        assert got.char_count == sum(len(c.text) for c in chunks)
        # Sanity: real content made it through extract->chunk->store unmangled.
        assert any("humbucker" in c.text for c in chunks)
    finally:
        db2.close()


@pytest.mark.integration
def test_ingest_empty_text_yields_empty_status_with_zero_chunks():
    """Plan 9 Task 5 / spec D6: a zero-character extraction is a successful,
    non-failing outcome, but it must NEVER be reported as "ready" — three
    real Wikipedia sources sat "ready" with 0 chars for two days and nobody
    noticed, because the UI painted them green. status="empty" makes that
    visibly distinguishable from a populated source.
    """
    db = SessionLocal()
    try:
        source_id = _make_source(db, "Empty source").id
        # Whitespace-only text -> extract_text("text", ...) returns [] (its own
        # documented contract) -> 0 sections/chunks, which is a successful,
        # non-failing outcome per this task's lifecycle contract.
        ingest_source(db, source_id, IngestPayload(kind="text", text="   \n\t  "))
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(KnowledgeSource, source_id)
        assert got is not None
        assert got.status == "empty"  # NOT "ready" (spec D6)
        assert got.error is None
        assert got.char_count == 0

        chunks = db2.scalars(select(Chunk).where(Chunk.source_id == source_id)).all()
        assert len(chunks) == 0
    finally:
        db2.close()


@pytest.mark.integration
def test_ingest_commits_ingesting_status_before_running_the_pipeline(monkeypatch):
    """Proves the brief's step 1 ("set status='ingesting'") is a real, durably
    committed write — visible to a concurrent reader on a different session —
    not just an in-memory attribute set alongside the rest of the pipeline.

    Starts the row at a status other than "ingesting" (a plain create/default
    row would already read "ingesting" per the model's own column default,
    which would make the assertion below true even if ingest_source never
    wrote anything — see KnowledgeSource.status's `default="ingesting"`).
    """
    db = SessionLocal()
    try:
        source_id = _make_source(db, "Mid-flight check", status="created").id
        seen: dict = {}

        def _peek_then_extract(*args, **kwargs):
            db2 = SessionLocal()
            try:
                seen["status"] = db2.get(KnowledgeSource, source_id).status
            finally:
                db2.close()
            return extract_mod.extract_text(*args, **kwargs)

        monkeypatch.setattr("app.brain.ingest.extract_text", _peek_then_extract)

        ingest_source(db, source_id, IngestPayload(kind="text", text="hi there"))

        assert seen["status"] == "ingesting"  # durably committed before extract ran
    finally:
        db.close()


@pytest.mark.integration
def test_ingest_failure_records_failed_status_and_error(monkeypatch):
    db = SessionLocal()
    try:
        source_id = _make_source(db, "Boom source").id

        def _boom(*_args, **_kwargs):
            raise RuntimeError("synthetic extraction failure")

        # Patch the name as bound inside ingest.py's own namespace (it does
        # `from app.brain.extract import extract_text`), not the origin module.
        monkeypatch.setattr("app.brain.ingest.extract_text", _boom)

        # Must NOT raise: ingest_source swallows pipeline exceptions and records
        # them on the row instead (see ingest.py's module docstring for why).
        ingest_source(db, source_id, IngestPayload(kind="text", text="irrelevant"))
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(KnowledgeSource, source_id)
        assert got is not None
        assert got.status == "failed"
        assert got.error is not None and "synthetic extraction failure" in got.error

        chunks = db2.scalars(select(Chunk).where(Chunk.source_id == source_id)).all()
        assert len(chunks) == 0  # nothing partial persisted on failure
    finally:
        db2.close()


@pytest.mark.integration
def test_ingest_retry_after_failure_clears_stale_error():
    """A source that failed once (e.g. the embed server was briefly down) and is
    re-ingested successfully must not keep showing the old error message — step 1
    ("set status='ingesting'") explicitly resets `error` before the pipeline runs.
    """
    db = SessionLocal()
    try:
        source_id = _make_source(db, "Retry source").id
        source = db.get(KnowledgeSource, source_id)
        source.status = "failed"
        source.error = "previous attempt: connection refused"
        db.commit()

        ingest_source(db, source_id, IngestPayload(kind="text", text=_THREE_PARAGRAPHS))
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(KnowledgeSource, source_id)
        assert got.status == "ready"
        assert got.error is None  # stale failure message must not survive a good retry
    finally:
        db2.close()


@pytest.mark.integration
def test_ingest_caps_total_extracted_text_at_max_ingest_chars(monkeypatch):
    """Fix 1 (review pass 2, resource exhaustion): kind="url" has no upstream
    size cap (unlike kind="text"'s MAX_TEXT_CHARS/kind="pdf" upload's
    MAX_UPLOAD_BYTES, both enforced in routers/knowledge.py) — a caller
    pointing kind="url" at a huge response body would otherwise drive an
    unbounded fetch -> chunk -> embed -> persist. ingest.py now caps *total*
    extracted text at MAX_INGEST_CHARS right after extract_text and before
    chunk_sections, so this one check also covers kind="pdf".

    MAX_INGEST_CHARS is monkeypatched down to a tiny value (rather than
    constructing a real ~2.1M-char payload) purely for test speed; extract_text
    is monkeypatched to hand back controlled Sections that comfortably cross
    that tiny cap; and the embed call is monkeypatched to zero-vectors of the
    right dimension so this test needs a live DB but not a live embed server.

    kind="url" now goes through paginate_source (Plan 9 Task 5): its
    single-Page path calls its OWN `extract_text` (app.brain.paginate's copy
    of the name, bound at import time) to fetch+store this source's one Page,
    and ingest_source reuses that Page's text instead of re-extracting (see
    ingest.py's ordering/double-fetch comment) — so the fake must be patched
    at BOTH import sites for this to stay real-network-free, and
    ingest_source ends up seeing the three sections re-joined onto that one
    Page rather than as three separate Sections. The cap math still lands on
    the same numbers either way: `_cap_total_chars` truncates mid-text
    regardless of section boundaries, and the join preserves ordering
    (a's, then b's, then c's), so the first 50 characters after the cap are
    still all "a"/"b" with no "c" — same assertions as before this task.
    """
    monkeypatch.setattr("app.brain.ingest.MAX_INGEST_CHARS", 50)

    huge_sections = [
        extract_mod.Section(heading=None, text="a" * 40, page=None),  # kept whole (40 <= 50)
        extract_mod.Section(heading=None, text="b" * 40, page=None),  # crosses the cap -> truncated to 10
        extract_mod.Section(heading=None, text="c" * 40, page=None),  # dropped entirely
    ]
    monkeypatch.setattr("app.brain.ingest.extract_text", lambda *a, **k: huge_sections)
    monkeypatch.setattr("app.brain.paginate.extract_text", lambda *a, **k: huge_sections)

    class _ZeroVectorProvider:
        def embed(self, texts, *, is_query=False):
            return [[0.0] * EMBED_DIM for _ in texts]

    monkeypatch.setattr("app.brain.ingest.get_embedder", lambda: _ZeroVectorProvider())

    db = SessionLocal()
    try:
        source_id = _make_source(db, "Huge URL source").id
        ingest_source(db, source_id, IngestPayload(kind="url", url="https://example.com/huge"))
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(KnowledgeSource, source_id)
        assert got is not None
        assert got.status == "ready"  # truncate-and-ingest, not a failure
        assert got.error is None
        assert got.char_count == 50  # capped at the monkeypatched MAX_INGEST_CHARS, not the original 120

        chunks = db2.scalars(select(Chunk).where(Chunk.source_id == source_id)).all()
        assert sum(len(c.text) for c in chunks) == got.char_count
        assert sum(len(c.text) for c in chunks) <= 50
        assert not any("c" in c.text for c in chunks)  # third section must not survive at all
    finally:
        db2.close()


@pytest.mark.integration
def test_ingest_db_error_after_partial_adds_rolls_back_everything(monkeypatch):
    """A later DB-level error (e.g. a value too long for a column) must discard
    ALL of this attempt's pending Chunk rows, not just the one that triggered
    it — proves the except-block's db.rollback() protects the "no partial
    writes on failure" guarantee even when the failure happens well into the
    persist loop (unlike the monkeypatched-extract_text failure test above,
    which fails before any Chunk is ever added).
    """
    from app.brain.chunk import ChunkDraft

    db = SessionLocal()
    try:
        source_id = _make_source(db, "Partial rollback source").id

        drafts = [
            ChunkDraft(text="this one embeds and adds just fine", section_path="ok", page=None),
            ChunkDraft(text="this one also embeds fine", section_path="y" * 600, page=None),  # > String(500)
        ]
        monkeypatch.setattr("app.brain.ingest.chunk_sections", lambda *_a, **_k: drafts)

        ingest_source(db, source_id, IngestPayload(kind="text", text="irrelevant, chunk_sections is patched"))
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        got = db2.get(KnowledgeSource, source_id)
        assert got.status == "failed"
        assert got.error  # the DB's own "value too long" error, verbatim

        chunks = db2.scalars(select(Chunk).where(Chunk.source_id == source_id)).all()
        assert len(chunks) == 0  # the FIRST (valid) chunk must not survive either
    finally:
        db2.close()


@pytest.mark.integration
def test_ingest_failure_recovery_itself_failing_does_not_propagate(monkeypatch):
    """Fix 3 (final review): the failure-recovery `except` block itself talks
    to the DB (rollback/get/commit) — if THAT also fails (e.g. the connection
    that just errored is now unusable, or an unrelated second DB hiccup),
    ingest_source must still not propagate anything to its caller (its one
    hard contract, like extract_text's, is "never raises"). Best-effort: the
    row may be left stranded at "ingesting" in this rare double-failure case
    (proven below) — worse than losing the human-readable error message, but
    still strictly better than crashing the caller (routers/knowledge.py's
    request handler).
    """
    db = SessionLocal()
    try:
        source_id = _make_source(db, "Recovery-failure source").id

        def _boom_extract(*_a, **_k):
            raise RuntimeError("synthetic primary failure")

        monkeypatch.setattr("app.brain.ingest.extract_text", _boom_extract)

        real_commit = db.commit
        state = {"calls": 0}

        def _flaky_commit():
            state["calls"] += 1
            # 1st commit = "ingesting"; 2nd = paginate_source's own Page-row
            # commit (Plan 9 Task 5 wired paginate_source into ingest_source,
            # ahead of extract_text in the pipeline); 3rd = the recovery write.
            if state["calls"] == 3:
                raise RuntimeError("synthetic secondary failure during recovery")
            return real_commit()

        monkeypatch.setattr(db, "commit", _flaky_commit)

        # Must NOT raise, despite BOTH the primary pipeline AND the recovery
        # path failing.
        ingest_source(db, source_id, IngestPayload(kind="text", text="irrelevant"))
    finally:
        db.close()

    # The recovery commit never landed, so the row is stranded at the last
    # value that WAS durably committed ("ingesting", from commit #1) — an
    # accepted, documented tradeoff for this double-failure edge case, not a
    # silent "ready"/success.
    db2 = SessionLocal()
    try:
        got = db2.get(KnowledgeSource, source_id)
        assert got is not None
        assert got.status == "ingesting"
    finally:
        db2.close()
