import re
from unittest.mock import patch

import fitz
import pytest
from app.brain.ingest import IngestPayload, ingest_source
from app.brain.ocr import (
    FIGURE_END,
    FIGURE_MARKER,
    FIGURE_PROMPT,
    MAX_PAGE_ATTEMPTS,
    OCR_PROMPT,
    book_text,
    figure_text,
    ocr_source,
)
from app.models.knowledge import EMBED_DIM, Chunk, KnowledgeSource, Page


class _Vision:
    """Fake provider: scripted per-call vision() results."""
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.prompts = []          # every prompt this provider was asked with
        self.images = []           # every image payload it was handed

    def vision(self, image_bytes, prompt, *, media_type="image/jpeg"):
        self.calls += 1
        self.prompts.append(prompt)
        self.images.append(image_bytes)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def embed(self, texts, *, is_query=False):
        return [[0.1] * EMBED_DIM for _ in texts]


class _VisionWithFailingEmbed(_Vision):
    """Same scripted vision() as `_Vision`, but `embed()` raises on specific
    call numbers (1-based, one call per page whose OCR produced text)."""
    def __init__(self, results, fail_calls):
        super().__init__(results)
        self.fail_calls = set(fail_calls)
        self.embed_calls = 0

    def embed(self, texts, *, is_query=False):
        self.embed_calls += 1
        if self.embed_calls in self.fail_calls:
            raise RuntimeError("embed service hiccup")
        return [[0.1] * EMBED_DIM for _ in texts]


def _src_with_pending_pages(db, n):
    src = KnowledgeSource(type="pdf", title="Book", status="ingesting")
    db.add(src); db.commit()
    for i in range(1, n + 1):
        db.add(Page(source_id=src.id, page_no=i,
                    image_path=f"{src.id}/{i:04d}.jpg", status="pending"))
    db.commit()
    return src


def test_transcribes_each_pending_page_and_embeds_chunks_linked_to_it(db, tmp_path, monkeypatch):
    src = _src_with_pending_pages(db, 2)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    for i in (1, 2):
        p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
        (p / f"{i:04d}.jpg").write_bytes(b"jpeg")
    fake = _Vision(["Page one, about humbuckers and how their two coils cancel mains hum.", "Page two, about Tube Screamer overdrive pedals and their midrange hump."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert (result.total, result.ready, result.failed) == (2, 2, 0)
    pages = db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()
    assert [p.status for p in pages] == ["ready", "ready"]
    assert "humbuckers" in pages[0].text
    chunks = db.query(Chunk).filter_by(source_id=src.id).all()
    assert chunks and all(c.page_id is not None for c in chunks)
    # every chunk is attributable to a real page — this is what makes a
    # citation verifiable rather than merely claimed
    assert {c.page_id for c in chunks} <= {p.id for p in pages}


def test_a_failing_page_is_retried_once_then_marked_failed_without_losing_good_pages(
    db, tmp_path, monkeypatch
):
    src = _src_with_pending_pages(db, 2)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    for i in (1, 2):
        p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
        (p / f"{i:04d}.jpg").write_bytes(b"jpeg")
    # page 1 succeeds; page 2 fails twice (initial + one retry)
    fake = _Vision(["A good page of transcribed text about amplifier gain staging.", RuntimeError("vl timeout"), RuntimeError("vl timeout")])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert (result.total, result.ready, result.failed) == (2, 1, 1)
    pages = db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()
    assert pages[0].status == "ready"          # page 1 survived page 2's failure
    assert pages[0].text == "A good page of transcribed text about amplifier gain staging."
    assert pages[1].status == "failed"
    assert "vl timeout" in pages[1].ocr_error
    assert fake.calls == 3                     # 1 + (1 initial + 1 retry)


def test_a_page_the_model_reads_as_blank_is_empty_not_ready(db, tmp_path, monkeypatch):
    src = _src_with_pending_pages(db, 1)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
    (p / "0001.jpg").write_bytes(b"jpeg")
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: _Vision([""]))
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: _Vision([""]))

    result = ocr_source(db, src.id)

    assert (result.ready, result.failed) == (0, 0)
    assert db.query(Page).filter_by(source_id=src.id).one().status == "empty"


def test_a_response_near_the_max_tokens_ceiling_is_treated_as_suspected_truncation(
    db, tmp_path, monkeypatch
):
    """vision() doesn't expose finish_reason (see its docstring) — a silently
    truncated transcription would otherwise look identical to a complete one
    and get committed as `ready`, corrupting the citation store. A response
    whose length is suspiciously close to the max_tokens=4000 output ceiling
    is treated like any other vision() failure: retried once, then failed —
    never silently trusted as `ready`."""
    src = _src_with_pending_pages(db, 1)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
    (p / "0001.jpg").write_bytes(b"jpeg")
    # Well past the truncation-suspicion threshold, both attempts.
    huge = "word " * 4000
    fake = _Vision([huge, huge])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert (result.ready, result.failed) == (0, 1)
    page = db.query(Page).filter_by(source_id=src.id).one()
    assert page.status == "failed"
    assert "truncat" in page.ocr_error.lower()
    assert fake.calls == 2                     # initial + one retry, same as any failure


def test_a_normal_length_page_is_not_flagged_as_truncated(db, tmp_path, monkeypatch):
    src = _src_with_pending_pages(db, 1)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
    (p / "0001.jpg").write_bytes(b"jpeg")
    fake = _Vision(["A perfectly ordinary page of transcribed text."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert (result.ready, result.failed) == (1, 0)
    assert fake.calls == 1


# --- Review fix: embed() failures must not abort the batch or leave a page
# `ready` with zero chunks ------------------------------------------------

def test_embed_failure_on_one_page_does_not_abort_the_batch(db, tmp_path, monkeypatch):
    """`_embed_page` used to be called outside any try/except in `ocr_source`,
    and the page was already committed `ready` just before that call. So an
    `embed()` hiccup on page 1 crashed the whole run (pages 2-3 never even
    attempted) AND left page 1 durably `ready` with zero chunks — the exact
    "ready but actually empty" lie spec D6 exists to eliminate."""
    src = _src_with_pending_pages(db, 3)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    for i in (1, 2, 3):
        p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
        (p / f"{i:04d}.jpg").write_bytes(b"jpeg")
    fake = _VisionWithFailingEmbed(
        ["Page one text, transcribed from the scan and long enough to chunk.", "Page two text, transcribed from the scan and long enough to chunk.", "Page three text, transcribed from the scan and long enough to chunk."],
        fail_calls={1},   # embed() raises only for page 1's chunks
    )
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    # pages 2 and 3 were still attempted despite page 1's embed failure
    assert fake.calls == 3          # vision() called for all three pages
    assert fake.embed_calls == 3    # embed() attempted for all three pages

    pages = db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()
    assert pages[0].status == "failed"    # embed failed -> NOT ready
    assert pages[1].status == "ready"
    assert pages[2].status == "ready"
    assert (result.total, result.ready, result.failed) == (3, 2, 1)

    # no page is ever left `ready` with zero chunks
    for page in pages:
        chunk_count = db.query(Chunk).filter_by(page_id=page.id).count()
        if page.status == "ready":
            assert chunk_count >= 1, f"page {page.page_no} is ready but has no chunks"
        else:
            assert chunk_count == 0


def test_invariant_ready_pages_always_have_chunks(db, tmp_path, monkeypatch):
    """The D6 invariant this whole redesign exists to protect, restated for
    the OCR engine itself: a page must never be committed `ready` unless its
    chunks are durably committed alongside it."""
    src = _src_with_pending_pages(db, 2)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    for i in (1, 2):
        p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
        (p / f"{i:04d}.jpg").write_bytes(b"jpeg")
    # page 1's embed fails; page 2 succeeds fully
    fake = _VisionWithFailingEmbed(["Page one of the book, transcribed in full and quite legible.", "Page two of the book, transcribed in full and also legible."], fail_calls={1})
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    pages = db.query(Page).filter_by(source_id=src.id).all()
    assert any(p.status == "ready" for p in pages)   # sanity: the scenario is meaningful
    for page in pages:
        if page.status == "ready" and page.text:
            n = db.query(Chunk).filter_by(page_id=page.id).count()
            assert n >= 1, f"page {page.page_no}: status=ready but 0 chunks committed"


# --- Review fix: pages with image_path=None (D2 degenerate pages) must be
# skipped, not sent to vision() and not marked failed ---------------------

def test_page_with_no_image_path_is_skipped_not_failed(db, tmp_path, monkeypatch):
    """D2 degenerate pages (url/text sources get exactly one Page row with
    image_path=NULL) have nothing to OCR — their text, if any, came from
    extraction. Previously this reached `_transcribe_with_retry`, which did
    `os.path.join(media_dir, None)` and blew up with a raw
    "join() argument must be str, bytes, or os.PathLike object, not
    'NoneType'" error; being marked `failed`, it was re-picked-up and
    re-failed with that same cryptic message on every subsequent run."""
    src = KnowledgeSource(type="url", title="Some URL", status="ingesting")
    db.add(src); db.commit()
    page = Page(source_id=src.id, page_no=1, image_path=None, status="pending")
    db.add(page); db.commit()
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    fake = _Vision([])   # vision() must never be called for this page
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    assert fake.calls == 0
    reloaded = db.get(Page, page.id)
    assert reloaded.status == "pending"     # untouched, NOT failed
    assert reloaded.ocr_error is None


def test_missing_jpeg_on_disk_yields_a_clear_error_not_a_raw_traceback(db, tmp_path, monkeypatch):
    src = _src_with_pending_pages(db, 2)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    # only page 2's jpeg exists on disk; page 1's is missing entirely
    p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
    (p / "0002.jpg").write_bytes(b"jpeg")
    fake = _Vision(["Page two text, transcribed from the scan and long enough to chunk."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert (result.total, result.ready, result.failed) == (2, 1, 1)
    pages = db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()
    assert pages[0].status == "failed"
    assert pages[0].ocr_error is not None
    assert "nonetype" not in pages[0].ocr_error.lower()
    assert "traceback" not in pages[0].ocr_error.lower()
    # a clear, human-meaningful message, not just an unadorned errno string
    assert "not found" in pages[0].ocr_error.lower()
    assert pages[1].status == "ready"           # the batch continued past page 1


# --- Review fix (Finding 1): OCR success never rolled up to the source row.
# All pages could reach status="ready" with real text while the parent
# `KnowledgeSource` still said status="empty", char_count=0 — nothing ever
# re-derived it from the pages. That's what the Library UI renders, so a
# perfectly-OCR'd book still showed RED/broken with a "Retry" button. Mirrors
# the D6 rule `ingest_source` already applies (app/brain/ingest.py) — ready
# iff char_count > 0 — rather than inventing a new rule here. -------------

def test_ocr_source_rolls_up_to_ready_with_summed_char_count(db, tmp_path, monkeypatch):
    src = _src_with_pending_pages(db, 2)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    for i in (1, 2):
        p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
        (p / f"{i:04d}.jpg").write_bytes(b"jpeg")
    fake = _Vision(["Page one, about humbuckers and how their two coils cancel mains hum.", "Page two, about Tube Screamer overdrive pedals and their midrange hump."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    reloaded = db.get(KnowledgeSource, src.id)
    pages = db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()
    assert reloaded.status == "ready"
    assert reloaded.char_count == sum(len(p.text) for p in pages)
    assert reloaded.char_count > 0


def test_ocr_source_all_pages_blank_leaves_source_empty_never_ready(db, tmp_path, monkeypatch):
    """D6: a source whose pages all come back blank must never roll up to
    `ready` — that would repeat the exact "ready but actually empty" lie the
    rest of this module already guards against per-page."""
    src = _src_with_pending_pages(db, 2)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    for i in (1, 2):
        p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
        (p / f"{i:04d}.jpg").write_bytes(b"jpeg")
    fake = _Vision(["", ""])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    reloaded = db.get(KnowledgeSource, src.id)
    assert reloaded.status == "empty"
    assert reloaded.char_count == 0


def test_ocr_source_partial_success_rolls_up_to_PARTIAL_never_green(
    db, tmp_path, monkeypatch
):
    """CHANGED BEHAVIOR (Stage 7.2). This test used to assert `status == "ready"`
    for a book with a failed page — it pinned the exact lie the stage exists to
    kill: 74/77 pages read rolled up to a GREEN CHECKMARK with no retry path,
    forever, while `job.error` already said "3 page(s) unreadable". A source with
    real text AND failed pages is now `partial` (amber, "retry failed pages").
    The char_count rule is unchanged: only READY pages' text is countable."""
    src = _src_with_pending_pages(db, 2)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    for i in (1, 2):
        p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
        (p / f"{i:04d}.jpg").write_bytes(b"jpeg")
    # page 1 succeeds; page 2 fails twice (initial + one retry)
    fake = _Vision(["Good page text, transcribed from the scan and long enough to chunk.", RuntimeError("vl timeout"), RuntimeError("vl timeout")])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    reloaded = db.get(KnowledgeSource, src.id)
    pages = db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()
    assert pages[0].status == "ready"
    assert pages[1].status == "failed"
    assert reloaded.status == "partial"
    assert reloaded.char_count == len(pages[0].text)


def test_ocr_source_stopping_early_with_pages_still_pending_rolls_up_to_partial_not_ready(
    db, tmp_path, monkeypatch
):
    """LIVE BUG, verified against the tutor's real library: mid-run, Powers read
    `status="ready"` in the DB with 9 pages still `pending`. `_rollup_source_status`
    only ever asked "how many pages FAILED?" — it never asked "how many pages are
    still UNREAD?" — so a run a rate limit stops partway through (the exact shape
    an 8-12 hour read takes across several sittings; see `reocr_source`'s
    docstring on the 429 rule) rolled up GREEN with most of the book never looked
    at, and a curriculum built from it would believe the book complete.

    Three pages: page 1 reads clean, page 2 rate-limits (the 429 rule parks the
    run and refunds the attempt), page 3 is never even reached — the `break`
    exits the loop before it gets there. Two of three pages are `pending` when
    the rollup runs. `partial` is the only truthful value: real text exists (not
    `empty`), but the book is not done (not `ready`) — and per
    `SOURCE_USABLE_STATUSES` it is still fully citable for the one page that WAS
    read."""
    from app.llm.errors import LLMError

    src = _src_with_pending_pages(db, 3)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    for i in (1, 2, 3):
        p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
        (p / f"{i:04d}.jpg").write_bytes(b"jpeg")
    fake = _Vision([
        "Page one, about humbuckers and how their two coils cancel mains hum.",
        LLMError("rate_limit", "429 slow down"),
    ])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    pages = db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()
    assert [p.status for p in pages] == ["ready", "pending", "pending"]
    reloaded = db.get(KnowledgeSource, src.id)
    assert reloaded.status == "partial"  # NOT "ready" — 2 of 3 pages still unread
    assert reloaded.char_count == len(pages[0].text)


def test_ocr_source_rollup_failure_does_not_undo_committed_page_work(db, tmp_path, monkeypatch):
    """A rollup failure (e.g. the re-fetch/commit of the source row blowing
    up) must not cost already-committed page work — pages that reached
    `ready` in the per-page loop must stay `ready` regardless of what
    happens to the source-row rollup afterwards. Guarded the same
    swallow-and-log way as every other commit in `ocr_source` (module
    docstring, D5): a rollup hiccup must not crash the whole batch either."""
    src = _src_with_pending_pages(db, 1)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
    (p / "0001.jpg").write_bytes(b"jpeg")
    fake = _Vision(["Some transcribed text from a real page of the tutor's book."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    real_commit = db.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        # Let every per-page commit inside the loop succeed; blow up only on
        # the rollup's own commit (the last one `ocr_source` issues).
        if calls["n"] > 2:
            raise RuntimeError("rollup commit boom")
        return real_commit()

    monkeypatch.setattr(db, "commit", flaky_commit)

    result = ocr_source(db, src.id)   # must NOT raise — swallowed like any other commit here

    assert (result.total, result.ready, result.failed) == (1, 1, 0)
    page = db.query(Page).filter_by(source_id=src.id).one()
    assert page.status == "ready"           # already-committed page work survives
    assert page.text == "Some transcribed text from a real page of the tutor's book."


# --- Review fix (minor): a page stuck at ocr_running (process killed
# mid-vision()) must be resumable, not stuck forever -----------------------

def test_a_page_stuck_at_ocr_running_is_resumed_on_the_next_run(db, tmp_path, monkeypatch):
    """A hard process kill mid-vision() leaves a page at `ocr_running`
    forever if that status isn't in the re-pickup filter. Re-OCRing a page
    is idempotent (`_embed_page` deletes that page's existing chunks first),
    so it's safe to re-pick-up `ocr_running` pages too."""
    src = _src_with_pending_pages(db, 1)
    page = db.query(Page).filter_by(source_id=src.id).one()
    page.status = "ocr_running"
    db.commit()
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
    (p / "0001.jpg").write_bytes(b"jpeg")
    fake = _Vision(["Recovered page text, transcribed from the scan and long enough to chunk."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert (result.total, result.ready, result.failed) == (1, 1, 0)
    assert fake.calls == 1
    reloaded = db.get(Page, page.id)
    assert reloaded.status == "ready"
    assert reloaded.text == "Recovered page text, transcribed from the scan and long enough to chunk."


# --- Stage 7.2/7.4: the retry policy and the quality gate -------------------
#
# What these pin, in one sentence each: an `empty` page was NEVER retried by any
# path, so one transient "" from the model permanently lost a page of the book;
# and the gate that fixes that must not be so eager that a genuinely short page
# ("Chapter 3") turns a healthy book amber.

def _prep(db, monkeypatch, tmp_path, n=1):
    src = _src_with_pending_pages(db, n)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    d = tmp_path / str(src.id)
    d.mkdir(exist_ok=True)
    for i in range(1, n + 1):
        (d / f"{i:04d}.jpg").write_bytes(b"jpeg")
    return src


def test_an_empty_page_is_picked_up_again_on_the_next_run(db, tmp_path, monkeypatch):
    """The permanently-dead-page bug: `empty` was not in the pickup filter, so a
    page the model returned "" for once was never looked at again by ANY path."""
    src = _prep(db, monkeypatch, tmp_path)
    page = db.query(Page).filter_by(source_id=src.id).one()
    page.status = "empty"
    db.commit()

    fake = _Vision(["The page was readable the second time, and this is long enough to chunk."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert result.ready == 1
    assert db.get(Page, page.id).status == "ready"


def test_a_page_that_has_burned_its_attempts_is_left_alone(db, tmp_path, monkeypatch):
    """The other half of making `empty` retryable: a genuinely blank scan (a real
    book has several) must not be re-billed to a paid vision model on every retry
    of the book, forever. Three attempts, then it rests."""
    src = _prep(db, monkeypatch, tmp_path)
    page = db.query(Page).filter_by(source_id=src.id).one()
    page.status = "empty"
    page.ocr_attempts = MAX_PAGE_ATTEMPTS
    db.commit()

    fake = _Vision(["never called"])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert (result.total, fake.calls) == (0, 0)
    assert db.get(Page, page.id).status == "empty"


def test_every_pickup_spends_exactly_one_attempt(db, tmp_path, monkeypatch):
    src = _prep(db, monkeypatch, tmp_path)
    fake = _Vision(["A page of real transcribed text, long enough for the chunker to keep."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    assert db.query(Page).filter_by(source_id=src.id).one().ocr_attempts == 1


def test_the_model_narrating_that_a_page_is_blank_is_EMPTY_not_failed(db, tmp_path, monkeypatch):
    """A vision model asked to transcribe an unreadable page does not fail — it
    NARRATES ("There is no visible text on this page."), and that 38-char string
    got embedded, indexed and cited to the tutor (`retrieve.py`'s floor quotes the
    same junk chunk). It is an `empty` page — a true fact about the book — so it
    must NOT count as a failure and must NOT turn the source amber."""
    src = _prep(db, monkeypatch, tmp_path, n=2)
    fake = _Vision([
        "A real page of the tutor's book, with enough text on it to chunk and embed.",
        "There is no visible text on this page.",
    ])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    pages = db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()
    assert pages[1].status == "empty"
    assert pages[1].text is None
    assert result.failed == 0
    assert db.get(KnowledgeSource, src.id).status == "ready"   # green, correctly


def test_a_genuinely_short_page_with_real_content_stays_ready(db, tmp_path, monkeypatch):
    """THE FALSE-AMBER GUARD. "Chapter 3" is a real page. Failing it would turn a
    healthy book amber — the specific harm Stage 7.4 was warned about. Short is
    not the same as unreadable: the sentinel screen is a PATTERN match, and the
    garbage screen has a length floor beneath which it does not fire at all."""
    src = _prep(db, monkeypatch, tmp_path)
    fake = _Vision(["Chapter 3"])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    page = db.query(Page).filter_by(source_id=src.id).one()
    assert page.text == "Chapter 3"
    assert page.status != "failed"                  # chunkable or not, NEVER failed
    assert db.get(KnowledgeSource, src.id).status != "partial"


def test_a_mojibake_transcription_is_retried_then_failed_never_indexed(db, tmp_path, monkeypatch):
    """Mostly-not-language output is a decode failure, not a page. It gets the
    same one-retry-then-`failed` treatment as any other unreadable page — and it
    never reaches the index, where it would be citable as if it were the book."""
    src = _prep(db, monkeypatch, tmp_path)
    garbage = "�▓" * 60
    fake = _Vision([garbage, garbage])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert (result.ready, result.failed) == (0, 1)
    assert fake.calls == 2                              # initial + one retry
    page = db.query(Page).filter_by(source_id=src.id).one()
    assert page.status == "failed"
    assert db.query(Chunk).filter_by(page_id=page.id).count() == 0


def test_greek_prose_is_not_mistaken_for_garbage(db, tmp_path, monkeypatch):
    """Greek is a first-class language here. A screen built on `isascii()` would
    fail an entire Greek corpus — this one is built on unicode categories."""
    src = _prep(db, monkeypatch, tmp_path)
    greek = ("Η κιθάρα είναι "
             "ένα έγχορδο μο"
             "υσικό όργανο. "
             "Μιλάμε για τον "
             "ήχο της κιθάρας. ") * 3
    fake = _Vision([greek])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert result.ready == 1
    assert db.query(Page).filter_by(source_id=src.id).one().status == "ready"


def test_a_book_with_a_failed_page_is_partial_and_stays_citable(db, tmp_path, monkeypatch):
    """71/77 is not "Ready". The source rolls up AMBER — and stays fully readable
    and fully citable, which is what `partial` means and `empty` does not."""
    src = _prep(db, monkeypatch, tmp_path, n=3)
    fake = _Vision([
        "Page one of the book, with plenty of transcribed text on it to chunk.",
        RuntimeError("vl timeout"), RuntimeError("vl timeout"),
        "Page three of the book, with plenty of transcribed text on it to chunk.",
    ])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    source = db.get(KnowledgeSource, src.id)
    assert source.status == "partial"
    assert source.char_count > 0
    assert db.query(Chunk).filter_by(source_id=src.id).count() >= 2


# =========================================================================
# Task 9 — THE MERGE GAP
# =========================================================================
#
# Task 3b creates 43 `image_region` pages (Powers' tab pages). They KEEP their
# correct publisher text AND are queued for vision so the tab PICTURE gets
# described. Before this task, `ocr.py` did `page.text = text or None`
# unconditionally and `ocr_source` selected on `Page.status` alone — it never
# read `ocr_reason`. So on first pickup those 43 pages would have (a) paid a
# vision call to re-transcribe text we already had for free, and (b) SILENTLY
# DISCARDED the publisher text in favour of that transcription. That is the
# exact "confident, permanent, silently-wrong" failure this whole plan exists
# to fix, reproduced by the fix's own machinery.

# A real Powers tab page: publisher text (correct, free, from a real embedded
# font) with a tab picture beside it that no text layer can describe.
_PUBLISHER_TEXT = (
    "Exercise 14 — Alternate Picking Across the Strings\n\n"
    "Keep the pick angle constant and let the wrist do the work. Start at 60bpm "
    "and only raise the tempo once every note rings cleanly."
)


def _image_region_page(db, monkeypatch, tmp_path, text=_PUBLISHER_TEXT):
    """One page in the shape `paginate.py` leaves an `image_region` page: it
    already carries the publisher's own text, and it is queued for vision
    anyway — for the PICTURE, not the words."""
    src = _prep(db, monkeypatch, tmp_path)
    page = db.query(Page).filter_by(source_id=src.id).one()
    page.text = text
    page.ocr_reason = "image_region"
    db.commit()
    return src, page


def _inherited_ocr_page(db, monkeypatch, tmp_path):
    """The shape `paginate.py` leaves a scanned page: the GlyphLessFont layer
    has ALREADY been discarded (text=None), because it is someone else's
    Tesseract and it lost every fraction glyph in the book."""
    src = _prep(db, monkeypatch, tmp_path)
    page = db.query(Page).filter_by(source_id=src.id).one()
    page.ocr_reason = "inherited_ocr"
    db.commit()
    return src, page


def test_an_image_region_page_keeps_its_publisher_text_and_gains_a_description(
    db, tmp_path, monkeypatch
):
    """THE MERGE, and the single most important assertion in this file. The
    publisher's words are correct and were free; the description is ADDED to
    them. `page.text = text or None` would have thrown the words away."""
    src, page = _image_region_page(db, monkeypatch, tmp_path)
    description = ("A six-line tab staff. The first bar alternates between the "
                   "5th and 7th frets of the A string, marked with down and up "
                   "pick strokes above each note.")
    fake = _Vision([description])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    reloaded = db.get(Page, page.id)
    assert _PUBLISHER_TEXT in reloaded.text          # NOT discarded — merged
    assert description in reloaded.text              # and the picture is described
    assert reloaded.text.index(_PUBLISHER_TEXT) < reloaded.text.index(description)
    assert reloaded.status == "ready"
    assert reloaded.text_source is not None          # never NULL after a pickup


def test_a_figure_description_is_marked_so_it_cannot_be_read_as_a_quotation(
    db, tmp_path, monkeypatch
):
    """The canon compile reads this text later and has no way to ask which half
    is which. A description of a photo is NOT a sentence from the book, and
    quoting it as one would be a fabricated citation."""
    src, page = _image_region_page(db, monkeypatch, tmp_path)
    description = "A photo of an amp face; the knobs are labelled VOLUME and MASTER."
    fake = _Vision([description])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    text = db.get(Page, page.id).text
    # THE CONTRACT: text inside a [FIGURE]...[/FIGURE] region is ours; everything
    # outside one is the page's own words. Both ends of the region are present, so
    # the description is BOUNDED rather than merely begun.
    assert FIGURE_MARKER in text and FIGURE_END in text
    assert book_text(text) == _PUBLISHER_TEXT
    assert description not in book_text(text)


def test_an_image_region_page_is_asked_to_describe_the_picture_not_re_transcribe_it(
    db, tmp_path, monkeypatch
):
    """The other half of the duplicated-spend bug: asking a model to transcribe
    a page whose text we already hold is a vision call bought for nothing."""
    src, _page = _image_region_page(db, monkeypatch, tmp_path)
    fake = _Vision(["A tab staff."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    assert fake.prompts == [FIGURE_PROMPT]
    assert fake.prompts[0] != OCR_PROMPT


def test_re_reading_an_image_region_page_does_not_stack_a_second_description(
    db, tmp_path, monkeypatch
):
    """A page picked up twice (a retry, an embed hiccup, a resumed run) must
    merge onto the PUBLISHER text again — not onto the previous run's output,
    which would stack description onto description onto description."""
    src, page = _image_region_page(db, monkeypatch, tmp_path)
    fake = _Vision(["First description of the tab.", "Second description of the tab."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)
    # put it back in the pickup set, exactly as a failure/resume would
    page = db.get(Page, page.id)
    page.status = "pending"
    db.commit()
    ocr_source(db, src.id)

    text = db.get(Page, page.id).text
    assert text.count(FIGURE_MARKER) == 1
    assert "First description" not in text        # replaced, not stacked
    assert "Second description" in text
    assert _PUBLISHER_TEXT in text


def test_an_image_region_page_whose_picture_yields_nothing_keeps_its_text(
    db, tmp_path, monkeypatch
):
    """`page.text = text or None` on an empty response would blank a page whose
    publisher text is perfectly good, and roll it up as `empty` — a page of the
    tutor's book deleted by a model shrug."""
    src, page = _image_region_page(db, monkeypatch, tmp_path)
    fake = _Vision([""])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    reloaded = db.get(Page, page.id)
    assert reloaded.text == _PUBLISHER_TEXT      # kept, verbatim
    assert reloaded.status == "ready"            # NOT empty


def test_an_inherited_ocr_page_is_transcribed_whole_and_replaces_nothing(
    db, tmp_path, monkeypatch
):
    """The other branch: a scanned page has NO trustworthy text (paginate already
    dropped the GlyphLessFont layer), so vision transcribes the whole page and
    that transcription IS the page — no marker, nothing to merge onto."""
    src, page = _inherited_ocr_page(db, monkeypatch, tmp_path)
    transcription = "Use a ¼-inch instrument cable, never a speaker cable, between the guitar and the amp."
    fake = _Vision([transcription])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    reloaded = db.get(Page, page.id)
    assert reloaded.text == transcription
    assert fake.prompts == [OCR_PROMPT]
    assert reloaded.status == "ready"


# =========================================================================
# Final review, CRITICAL 1 — THE [FIGURE] CONTRACT ON THE TRANSCRIBE PATH
# =========================================================================
#
# The module used to state the contract POSITIONALLY ("everything from the first
# marker on is ours") while `OCR_PROMPT` asked for markers INTERLEAVED in reading
# order with no terminator. That contract is true in `describe` mode and FALSE on
# the transcribe path — i.e. on 845 of the tutor's 888 pages, none of which had a
# single test. Both failure directions were live, and the second is the one this
# whole module is architected against:
#
#   split at the first marker  -> the book's real prose after a figure is
#                                 discarded as "ours" -> content silently lost;
#   take only the marker's line -> a multi-line description's continuation reads
#                                 as the book's words -> OUR DESCRIPTION OF A
#                                 PHOTO IS CITED AS A VERBATIM QUOTATION WITH A
#                                 REAL PAGE NUMBER ON IT.
#
# The contract is now DELIMITED and total: text inside a [FIGURE]...[/FIGURE]
# region is ours, everything outside one is the page's own words, and an
# unterminated [FIGURE] runs to the end of the text. `book_text` is that rule,
# executable, for every reader downstream (Part B's canon compile above all).

# A real transcribed page shape: the author's prose, a figure in the middle of it
# where the picture actually sits, and THE AUTHOR'S PROSE RESUMING AFTERWARDS.
# Kahn p.63 is exactly this page.
_PROSE_BEFORE = "The humbucker cancels hum by wiring two coils out of phase."
_PROSE_AFTER = "Seth Lover filed the patent in 1955, and Gibson shipped it in 1957."
_FIGURE_BODY = ('A photo of a PAF humbucker with its cover removed, labelled\n'
                '"1957". Both coils and the maple spacer are visible.')


def _transcribed_page_with_a_figure() -> str:
    return (f"{_PROSE_BEFORE}\n"
            f"{FIGURE_MARKER}\n{_FIGURE_BODY}\n{FIGURE_END}\n"
            f"{_PROSE_AFTER}")


def test_book_text_keeps_prose_that_resumes_AFTER_a_figure_on_a_transcribed_page():
    """THE BUG, at its smallest. A transcribed page's prose does not stop at the
    first figure — the figure sits in the middle of it. Splitting at the first
    marker throws the rest of the page away; not splitting at all quotes our
    photo caption as the author's sentence."""
    text = _transcribed_page_with_a_figure()

    words = book_text(text)

    assert _PROSE_BEFORE in words
    assert _PROSE_AFTER in words, "the book's prose after the figure was discarded as ours"
    assert _FIGURE_BODY not in words, "our description of a photo read as the book's words"


def test_book_text_separates_every_figure_on_a_page_with_several():
    """Reading order means N figures interleaved with N+1 runs of prose. The rule
    is per-REGION, not per-page, so it holds however many there are."""
    text = (f"First paragraph.\n{FIGURE_MARKER} A wiring diagram. {FIGURE_END}\n"
            f"Second paragraph.\n{FIGURE_MARKER} A photo of a Tele. {FIGURE_END}\n"
            "Third paragraph.")

    words = book_text(text)

    for prose in ("First paragraph.", "Second paragraph.", "Third paragraph."):
        assert prose in words
    for ours in ("wiring diagram", "photo of a Tele"):
        assert ours not in words


def test_book_text_never_fuses_the_words_either_side_of_an_inline_figure():
    """A region lifted out of the middle of a line must not weld its neighbours
    into a word that is in neither the book nor our description."""
    assert "hum" in book_text(f"hum{FIGURE_MARKER}x{FIGURE_END}bucker")
    assert "humbucker" not in book_text(f"hum{FIGURE_MARKER}x{FIGURE_END}bucker")


def test_an_unterminated_figure_runs_to_the_end_of_the_text():
    """THE TOTALITY CLAUSE, and it exists for the pages already in his library:
    Powers' 43 describe-mode pages were written before there was a terminator, in
    a format whose rule was "everything from the marker on is ours" — which was
    TRUE for describe mode. So the reader keeps resolving that shape, in the only
    direction that cannot fabricate a citation.

    Verified against all 46 marker-carrying pages in the live library: zero are
    misread, so none of them needs re-reading."""
    legacy = f"{_PUBLISHER_TEXT}\n\n{FIGURE_MARKER} A six-line tab staff, 5th to 7th fret."

    assert book_text(legacy) == _PUBLISHER_TEXT


def test_a_page_that_is_nothing_but_an_unterminated_figure_has_no_words_of_its_own():
    """The OTHER legacy shape, and it is the sharper one — Powers p.47 and p.54,
    live in his library right now, are a bare `[FIGURE]` at position 0 followed
    by a description that runs for a THOUSAND CHARACTERS over several lines
    ("Measure 1: under 'Am' — columns of 1/2/2...").

    A reader that took only the marker's own line as ours would hand every line
    after the first to the canon compile as Maxwell Powers' own prose. The page
    has no words of its own: all of it is ours."""
    all_figure = (f"{FIGURE_MARKER} A tablature exercise in 4/4.\n"
                  "Measure 1: under \"Am\" — columns of 1/2/2 repeated twice.\n"
                  "Measure 2: under \"F\" — columns of 1/2/3 repeated.")

    assert book_text(all_figure) == ""


# --- figure_text: the contract's OTHER half ---------------------------------
#
# `book_text` answers "what did the author write?". The canon compile (Part B)
# needs the complement too — our description of the picture is real content it
# must not throw away (Powers is 40 pages of tab; Hunter p.57 is an amp photo),
# it just may never be quoted as the author's words. These live here, next to
# `book_text`, because they are the SAME rule read the other way round: a
# `figure_text` that is not exactly complementary would put text in NEITHER half
# (silently losing a page) or in BOTH (which is the fabrication).

def test_figure_text_returns_our_description_and_never_the_authors_prose():
    text = _transcribed_page_with_a_figure()

    ours = figure_text(text)

    assert _FIGURE_BODY in ours
    assert _PROSE_BEFORE not in ours, "the author's prose collected as our description"
    assert _PROSE_AFTER not in ours


def test_figure_text_collects_every_figure_on_a_page_with_several():
    text = (f"First paragraph.\n{FIGURE_MARKER} A wiring diagram. {FIGURE_END}\n"
            f"Second paragraph.\n{FIGURE_MARKER} A photo of a Tele. {FIGURE_END}\n"
            "Third paragraph.")

    ours = figure_text(text)

    assert "wiring diagram" in ours
    assert "photo of a Tele" in ours
    for prose in ("First paragraph.", "Second paragraph.", "Third paragraph."):
        assert prose not in ours


def test_figure_text_honours_the_totality_clause_on_a_legacy_powers_page():
    """The same unterminated shape `book_text` resolves to "" — 43 of Powers'
    pages, live in his library right now. All of it is ours, so all of it must
    come back here: this is the ONLY path by which that book's tab reaches the
    canon at all."""
    all_figure = (f"{FIGURE_MARKER} A tablature exercise in 4/4.\n"
                  "Measure 1: under \"Am\" — columns of 1/2/2 repeated twice.")

    ours = figure_text(all_figure)

    assert "tablature exercise in 4/4" in ours
    assert "Measure 1" in ours


def test_figure_text_and_book_text_partition_the_page_between_them():
    """THE INVARIANT, stated as a test. Every non-marker character of the page
    lands in exactly one half — never both (our words quoted as his), never
    neither (a page silently lost). Checked over all four shapes the library
    actually contains: well-formed, multiple, inline, and legacy-unterminated."""
    shapes = [
        _transcribed_page_with_a_figure(),
        f"a{FIGURE_MARKER}b{FIGURE_END}c{FIGURE_MARKER}d{FIGURE_END}e",
        f"hum{FIGURE_MARKER}x{FIGURE_END}bucker",
        f"{_PUBLISHER_TEXT}\n\n{FIGURE_MARKER} An unterminated tab staff.",
        "no markers at all, just the author's prose",
    ]
    def _chars(s: str) -> list[str]:
        # A MULTISET, not a concatenation: the two halves interleave on the page
        # (prose, figure, prose), so their order says nothing. What must hold is
        # that each character is accounted for exactly once.
        return sorted(re.sub(r"\s+", "", s))

    for text in shapes:
        both = _chars(book_text(text) + figure_text(text))
        page = _chars(text.replace(FIGURE_MARKER, "").replace(FIGURE_END, ""))
        assert both == page, f"page not partitioned: {text!r}"


def test_figure_text_leaves_no_markers_in_what_it_returns():
    """The markers are OUR delimiters, never content. A stray nested opener sits
    in text the rule already calls ours (`ocr.py`) — it must not travel into a
    prompt as if the page contained the literal string."""
    ours = figure_text(f"p{FIGURE_MARKER}a {FIGURE_MARKER} b{FIGURE_END}q")

    assert FIGURE_MARKER not in ours
    assert FIGURE_END not in ours


def test_figure_text_is_empty_for_a_page_with_no_pictures():
    assert figure_text("Just the author, writing about mahogany.") == ""
    assert figure_text("") == ""
    assert figure_text(None) == ""


def test_a_transcribed_page_round_trips_with_the_two_halves_still_separable(
    db, tmp_path, monkeypatch
):
    """End to end, through `ocr_source`, on the path 845 of 888 pages take: what
    lands in `Page.text` must be parseable back into the book's words and ours
    with no guessing."""
    src, page = _inherited_ocr_page(db, monkeypatch, tmp_path)
    fake = _Vision([_transcribed_page_with_a_figure()])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    reloaded = db.get(Page, page.id)
    assert reloaded.status == "ready"
    assert fake.prompts == [OCR_PROMPT]
    assert _PROSE_AFTER in book_text(reloaded.text)
    assert _FIGURE_BODY not in book_text(reloaded.text)


def test_a_transcription_whose_figure_is_never_closed_is_failed_not_stored(
    db, tmp_path, monkeypatch
):
    """The one shape we cannot parse and must not guess at. An open [FIGURE] with
    prose after it is either a description that swallowed the page's tail, or a
    page whose tail we are about to discard — and nothing downstream could tell.

    So it is a FAILED page: amber, retryable, visible. Cheap and honest, against
    a silent permanent lie in either direction. `describe` mode never reaches
    here — there, every word IS ours, so `_marked` can close the region itself
    without guessing at anything (see the test below)."""
    src, page = _inherited_ocr_page(db, monkeypatch, tmp_path)
    unclosed = f"{_PROSE_BEFORE}\n{FIGURE_MARKER}\n{_FIGURE_BODY}\n{_PROSE_AFTER}"
    fake = _Vision([unclosed, unclosed])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert (result.ready, result.failed) == (0, 1)
    assert fake.calls == 2                       # initial + one retry, like any failure
    reloaded = db.get(Page, page.id)
    assert reloaded.status == "failed"
    assert reloaded.text is None                 # never stored
    assert db.query(Chunk).filter_by(page_id=reloaded.id).count() == 0
    assert reloaded.ocr_reason == "inherited_ocr"   # not garbage — the markup, not the page


def test_a_clean_transcription_with_no_pictures_at_all_is_untouched(
    db, tmp_path, monkeypatch
):
    """THE FALSE-FAILURE GUARD for the screen above. Most pages have no figure on
    them; the screen must be silent on every one of them."""
    src, _page = _inherited_ocr_page(db, monkeypatch, tmp_path)
    plain = "A page of ordinary prose about gain staging, with no picture on it."
    fake = _Vision([plain])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert result.ready == 1
    assert db.query(Page).filter_by(source_id=src.id).one().text == plain


def test_a_describe_response_is_closed_for_the_model_rather_than_failed(
    db, tmp_path, monkeypatch
):
    """The asymmetry with the transcribe screen, and why it is not an oversight:
    in `describe` mode EVERY word of the response is ours — the prompt forbids
    transcription outright — so "the region ends where the response ends" is a
    fact, not a guess. Nothing is ambiguous, so nothing needs to fail; failing
    would only throw away a correct description of one of Powers' 43 tab pages."""
    src, page = _image_region_page(db, monkeypatch, tmp_path)
    fake = _Vision([f"{FIGURE_MARKER}\nA six-line tab staff, 5th to 7th fret."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    reloaded = db.get(Page, page.id)
    assert reloaded.status == "ready"
    assert "tab staff" in reloaded.text
    assert book_text(reloaded.text) == _PUBLISHER_TEXT   # the region got closed


def test_a_describe_response_that_ignores_the_markers_entirely_is_still_bounded(
    db, tmp_path, monkeypatch
):
    """`_marked`'s reason for existing, restated for the delimited contract: an
    UNMARKED description is indistinguishable from the book's words, and a
    HALF-marked one is worse — it reads as the book's words from the point the
    model stopped writing markup."""
    src, page = _image_region_page(db, monkeypatch, tmp_path)
    fake = _Vision(["A six-line tab staff, 5th to 7th fret."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    text = db.get(Page, page.id).text
    assert text.count(FIGURE_MARKER) == 1 and text.count(FIGURE_END) == 1
    assert book_text(text) == _PUBLISHER_TEXT


def test_re_reading_a_closed_describe_page_still_merges_onto_the_publisher_text(
    db, tmp_path, monkeypatch
):
    """Idempotency, re-proved against the delimited format: `_publisher_text` now
    strips REGIONS rather than truncating at the first marker, and a page picked
    up twice must still merge onto the publisher's words — not onto the previous
    run's description."""
    src, page = _image_region_page(db, monkeypatch, tmp_path)
    fake = _Vision([f"{FIGURE_MARKER}\nFirst description.\n{FIGURE_END}",
                    f"{FIGURE_MARKER}\nSecond description.\n{FIGURE_END}"])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)
    page = db.get(Page, page.id)
    page.status = "pending"
    db.commit()
    ocr_source(db, src.id)

    text = db.get(Page, page.id).text
    assert text.count(FIGURE_MARKER) == 1
    assert "First description" not in text
    assert "Second description" in text
    assert book_text(text) == _PUBLISHER_TEXT


def test_both_prompts_ask_for_a_figure_region_that_is_opened_AND_closed():
    """PROMPTS ARE THE CONTRACT'S OTHER HALF. `_marked` can only guarantee the
    describe path; on the transcribe path the model's compliance is what produces
    a parseable page, so the prompt must ask for the terminator explicitly — and
    say why, because a model told the stakes complies better than one given a
    bare rule."""
    for prompt in (OCR_PROMPT, FIGURE_PROMPT):
        assert FIGURE_MARKER in prompt
        assert FIGURE_END in prompt, f"the prompt never asks for {FIGURE_END}"


def test_the_transcribe_prompt_says_where_the_books_words_resume():
    """The interleaving is the whole difficulty of the transcribe path: prose,
    figure, prose. The prompt has to name the boundary, not just the marker."""
    assert "reading order" in OCR_PROMPT
    assert "resumes" in OCR_PROMPT


# --- Task 9: the prompt must also describe pictures -----------------------

def test_ocr_prompt_asks_for_the_text_verbatim_AND_a_description_of_any_figure():
    """An OCR layer cannot describe a PICTURE, and these are books about guitar
    TONE: the photo of a dialed-in amp face, the signal-chain diagram and
    Powers' 40 pages of tab ARE the content. Hunter p.57's amp photo (knobs
    labelled VOLUME, MASTER) produced literally nothing from Tesseract."""
    assert "verbatim" in OCR_PROMPT.lower()
    assert FIGURE_MARKER in OCR_PROMPT
    for word in ("photo", "diagram", "describ"):
        assert word in OCR_PROMPT.lower(), f"OCR_PROMPT never mentions {word!r}"


def test_both_prompts_forbid_invention():
    # a transcriber that "helpfully" fills gaps corrupts the ground truth the
    # whole app is supposed to trust
    for prompt in (OCR_PROMPT, FIGURE_PROMPT):
        assert "invent" in prompt.lower()


def test_both_prompts_ask_for_a_bare_reply_because_models_narrate():
    """Left unshaped, a transcription arrives wrapped in chatty markdown — the
    model narrates that it zoomed in, it adds headers. Nothing downstream
    scrubs it, deliberately, so shaping the reply is this prompt's job.
    Regex-eating a model's prose would eventually eat a line of a real page
    with it."""
    for prompt in (OCR_PROMPT, FIGURE_PROMPT):
        assert "preamble" in prompt.lower()


def test_figure_prompt_never_asks_for_a_transcription_of_the_page():
    """If it did, the merge would append the page's own words back onto the
    page's own words — the duplicated spend AND a doubled page."""
    assert "describ" in FIGURE_PROMPT.lower()
    assert "verbatim" not in FIGURE_PROMPT.lower()


# --- Task 9: provenance — text_source is never NULL after a pickup --------

def test_ocr_records_that_claude_wrote_the_text(db, tmp_path, monkeypatch):
    """Without this the tutor cannot tell a page Claude read from a page that
    still carries someone else's Tesseract — which is the entire question this
    plan exists to answer."""
    src = _prep(db, monkeypatch, tmp_path)
    monkeypatch.setattr("app.config.settings.llm_provider", "claude")
    fake = _Vision(["Use a ¼-inch instrument cable between the guitar and the amp head."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    page = db.query(Page).filter_by(source_id=src.id).one()
    assert page.text_source == "claude"


def test_an_unknown_provider_name_is_recorded_honestly_not_guessed(db, tmp_path, monkeypatch):
    """A provider the map has never heard of (a stale `LLM_PROVIDER=qwen` in an
    old .env, say) is a fact worth recording as itself: the column answers
    "whose words are these", and a guess would be a lie in user data."""
    src = _prep(db, monkeypatch, tmp_path)
    monkeypatch.setattr("app.config.settings.llm_provider", "qwen")
    fake = _Vision(["A page of text transcribed by some other model, long enough to chunk."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    assert db.query(Page).filter_by(source_id=src.id).one().text_source == "qwen"


def test_a_merged_page_credits_the_publisher_AND_the_model_not_just_one(
    db, tmp_path, monkeypatch
):
    """A merged page is genuinely both. "claude" alone would claim a
    transcription that never happened — the merge gap's own lie, told by the
    column instead of the text; "text_layer" alone would hide that a model put
    content on the page at all, which is the question this column exists to
    answer."""
    src, page = _image_region_page(db, monkeypatch, tmp_path)
    monkeypatch.setattr("app.config.settings.llm_provider", "claude")
    fake = _Vision(["A tab staff showing the first four bars."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    reloaded = db.get(Page, page.id)
    assert reloaded.text_source == "text_layer+claude"
    assert len(reloaded.text_source) <= 20        # models/knowledge.py: String(20)


def test_an_image_region_page_with_no_picture_described_still_credits_the_publisher(
    db, tmp_path, monkeypatch
):
    """Nothing was added, so nothing may be claimed: the text on this page is
    the publisher's, exactly as it was before the model looked at it."""
    src, page = _image_region_page(db, monkeypatch, tmp_path)
    monkeypatch.setattr("app.config.settings.llm_provider", "claude")
    fake = _Vision([""])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    assert db.get(Page, page.id).text_source == "text_layer"


def test_a_page_that_could_not_be_read_says_so_rather_than_leaving_text_source_null(
    db, tmp_path, monkeypatch
):
    """NULL means "written before anyone tracked this". A page this run just
    failed to read is not that — it is a page we tried and lost, and the column
    must say so rather than being indistinguishable from history."""
    src = _prep(db, monkeypatch, tmp_path)
    fake = _Vision([RuntimeError("vl timeout"), RuntimeError("vl timeout")])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    page = db.query(Page).filter_by(source_id=src.id).one()
    assert page.status == "failed"
    assert page.text_source == "failed"


# --- Task 9: the garbage screen -------------------------------------------

# Kahn p.40, verbatim: the page renders blank in BOTH MuPDF and poppler (it is
# damaged in the PDF itself, so no model can recover it) and Tesseract wrote
# these 544 characters for it — which today's pipeline ingests as page content.
_KAHN_P40_NOISE = (
    "7 ipgges x \na \nRar \nek \nen ee \n& \neile \na= \n@ \nFilip \n«@ \n' \n= \nae \n¢ \n® a "
    "\n_ \n- \n¥ \n= \n, \n» \nve \né \n~ \nas \nre \not \ni \nby \nay \nse \nen \nid \nor \nan"
)


def test_garbage_transcription_is_marked_failed_not_committed_as_content(
    db, tmp_path, monkeypatch
):
    """9 of 617 pages across the three scanned books are like this. What must NOT
    happen is 544 chars of "7 ipgges x a Rar ek en ee" entering the citation
    store as if it were a page of the tutor's book."""
    src = _prep(db, monkeypatch, tmp_path)
    fake = _Vision([_KAHN_P40_NOISE, _KAHN_P40_NOISE])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    page = db.query(Page).filter_by(source_id=src.id).one()
    assert page.status == "failed"
    assert page.text_source == "failed"
    assert page.ocr_reason == "ocr_garbage"
    assert result.ready == 0
    assert fake.calls == 2                                   # initial + one retry
    assert db.query(Chunk).filter_by(page_id=page.id).count() == 0


def test_the_garbage_screen_does_not_judge_a_figure_description(
    db, tmp_path, monkeypatch
):
    """`looks_like_ocr_garbage` screens a TRANSCRIPTION against the shape of real
    prose. A description of a TAB DIAGRAM is neither: it is inherently full of
    short tokens ("5 7 5", "E A D G B E"). Its own docstring warns that a false
    positive here THROWS AWAY correct content and marks the page failed — and
    the pages it would throw away are exactly Powers' 43 tab pages, the ones
    `image_region` exists for. So the screen belongs to transcribe mode only."""
    src, page = _image_region_page(db, monkeypatch, tmp_path)
    fake = _Vision([_KAHN_P40_NOISE])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    reloaded = db.get(Page, page.id)
    assert reloaded.ocr_reason == "image_region"    # routing survives — not clobbered
    assert _PUBLISHER_TEXT in reloaded.text         # and the publisher text is still there


# --- Task 9: 150dpi ------------------------------------------------------

def test_vision_pages_are_rendered_at_the_ocr_dpi_not_the_stored_110(
    db, tmp_path, monkeypatch
):
    """RENDER_DPI=110 is a QWEN CEILING ("image item with length 2080 exceeds
    pre-allocated encoder cache size 2048"), not a quality choice. Claude is
    high-res tier: 110dpi = 1,496 visual tokens, 150dpi = 2,714. Feeding it the
    stored 110dpi JPEG caps its fidelity on exactly the glyphs that matter —
    `¼` and `⅛` differ by a few pixels at page scale."""
    src = _prep(db, monkeypatch, tmp_path)
    monkeypatch.setattr("app.config.settings.ocr_render_dpi", 150)
    seen = {}

    def _fake_render(page, dpi):
        seen["dpi"] = dpi
        return b"\xff\xd8rendered-at-150"

    monkeypatch.setattr("app.brain.ocr._render_for_vision", _fake_render)
    fake = _Vision(["A page of the book transcribed from the 150dpi render."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    assert seen["dpi"] == 150
    assert fake.images == [b"\xff\xd8rendered-at-150"]   # the render, not the stored JPEG


def test_render_for_vision_rasterises_the_source_pdf_at_the_requested_dpi(
    db, tmp_path, monkeypatch
):
    """Not a mock: the stored JPEG is 110dpi and UPSCALING it recovers nothing.
    The only thing that can produce a real 150dpi page is the original PDF, so
    the original PDF has to still be here."""
    from app.brain.ocr import _render_for_vision

    src = _prep(db, monkeypatch, tmp_path)
    page = db.query(Page).filter_by(source_id=src.id).one()
    doc = fitz.open()
    doc.new_page(width=612, height=792)                 # US Letter @ 72pt/inch
    (tmp_path / str(src.id) / "source.pdf").write_bytes(doc.tobytes())

    data = _render_for_vision(page, 150)

    pix = fitz.Pixmap(data)
    assert (pix.width, pix.height) == (1275, 1650)      # 8.5in x 150dpi, 11in x 150dpi
    assert fitz.Pixmap(_render_for_vision(page, 110)).width == 935   # and the knob is real


def test_a_source_with_no_stored_pdf_falls_back_to_its_page_scan(
    db, tmp_path, monkeypatch
):
    """Every book ingested before this task has page scans and no PDF. Falling
    back to the 110dpi JPEG reads that book at 110dpi, which is what it was
    always going to be; RAISING here would make those books permanently
    un-re-readable instead."""
    src = _prep(db, monkeypatch, tmp_path)          # writes 0001.jpg, no source.pdf
    fake = _Vision(["Transcribed from the stored scan, because there is no PDF to re-render."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert result.ready == 1
    assert fake.images == [b"jpeg"]                  # `_prep` writes exactly these bytes


# =========================================================================
# Final review, CRITICAL 2 — THE LAYER `paginate` REFUSED, RE-ADOPTED BY
# `ingest` ~30 LINES LATER
# =========================================================================
#
# `paginate_source` routes on the font and correctly REFUSES a GlyphLessFont
# layer: `Page.text=None`, `status="pending"`, `ocr_reason="inherited_ocr"`.
# `ingest_source` then re-opens the SAME PDF bytes, re-extracts THE VERY LAYER
# paginate just refused, chunks it, embeds it, and links it by `page_id` to those
# pending pages. `retrieve.search` joins Chunk->Page only to resolve a page
# number — it does not filter on `Page.status` — so those chunks are fully
# retrievable and citable. Task 3 changed one half of the pipeline and nobody
# owned the other half.
#
# WHY IT SHIPPED, and this is the part worth remembering: the test that asserts
# the right invariant (`test_embed_failure_on_one_page_does_not_abort_the_batch`,
# "if ready: chunks >= 1 / else: chunks == 0") WAS VACUOUS. Every OCR test above
# builds its Page rows by hand via `_src_with_pending_pages`, so no ingest-time
# chunk ever existed to survive, and the `else` branch passed against an empty
# set. The fixture below is the missing piece: it goes through `ingest_source`
# FIRST, so the chunk store is populated the way a real upload populates it and
# the invariant can actually fail.
#
# THE INVARIANT, stated where it belongs — on the DATA, not on a status:
#
#     A PAGE WHOSE `text` IS NULL HAS NO CHUNKS.
#
# Not "a non-ready page has no chunks": an `image_region` page keeps correct
# PUBLISHER text (paginate.py) and must keep the chunks that go with it even when
# its figure description fails. Powers' 43 tab pages are exactly that page.

# The Kahn p.63 story, in miniature. The layer in the PDF is Tesseract's, and it
# lost the fraction glyph; what a model reads off the scan has it. The two are
# distinguishable ON SIGHT, which is what lets these tests say WHICH text a chunk
# is holding rather than merely how many there are.
_REFUSED_LAYER = (
    "Connections are made either with 4-inch stereo cables or with insert cables "
    "that have a 4-inch stereo connection at one end and two 4-inch mono cables "
    "at the other end of the signal run."
)
_WHAT_CLAUDE_READS = (
    "Connections are made either with ¼-inch stereo cables or with insert cables "
    "that have a ¼-inch stereo connection at one end and two ¼-inch mono cables "
    "at the other end of the signal run."
)


def _scanned_pdf_bytes(layers: list[str]) -> bytes:
    """A REAL PDF carrying a real text layer — `extract_text` genuinely extracts
    it, which is the whole mechanism under test."""
    doc = fitz.open()
    for body in layers:
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(fitz.Rect(72, 72, 540, 720), body, fontsize=11)
    return doc.tobytes()


def _upload(db, monkeypatch, tmp_path, layers, *, kind="ocr", images=False):
    """Upload a book THE WAY THE TUTOR UPLOADS ONE — through `ingest_source`.

    Only `text_layer_kind` is faked, and only because building a genuine
    GlyphLessFont PDF needs an OCR toolchain (see test_brain_textlayer.py, which
    is where that detector is actually tested). Everything else here is the real
    pipeline: a real PDF, real `paginate_source` routing, real `extract_text`,
    real `chunk_sections`, real `Chunk` rows linked by `page_id`.
    """
    src = KnowledgeSource(type="pdf", title="A scanned book", status="pending")
    db.add(src)
    db.commit()
    monkeypatch.setattr("app.brain.paginate.settings.media_dir", str(tmp_path))
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    monkeypatch.setattr("app.brain.ingest.get_embedder", lambda: _Vision([]))
    with patch("app.brain.paginate.text_layer_kind", return_value=kind), \
         patch("app.brain.paginate.has_content_images", return_value=images):
        ingest_source(db, src.id, IngestPayload(kind="pdf", data=_scanned_pdf_bytes(layers)))
    return src


def _chunks_of(db, page) -> list[str]:
    return [c.text for c in db.query(Chunk).filter_by(page_id=page.id).all()]


def _pages_of(db, src) -> list[Page]:
    return db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()


def test_the_fixture_reproduces_the_bug_an_upload_indexes_the_layer_we_refused(
    db, tmp_path, monkeypatch
):
    """THE CHARACTERISATION TEST, and it must keep passing. If this ever goes
    green-by-accident the tests below stop testing anything at all — which is
    precisely how the vacuous invariant above shipped.

    It also pins the PRODUCT ANSWER as it stands: between upload and a completed
    re-OCR, a scanned book's chunk store serves the untrusted layer. That is not
    a regression (it is exactly as wrong as before this branch) and it is not
    what this fix changes — see the report's open question."""
    src = _upload(db, monkeypatch, tmp_path, [_REFUSED_LAYER])
    page = _pages_of(db, src)[0]

    assert page.status == "pending", "paginate must refuse the inherited layer"
    assert page.text is None, "...and drop it"
    assert page.ocr_reason == "inherited_ocr"
    # ...and yet:
    chunks = _chunks_of(db, page)
    assert chunks, "the fixture is meaningless unless the upload really indexed something"
    assert any("4-inch" in c for c in chunks), (
        "ingest re-adopted the very layer paginate refused, linked to a page whose text is NULL"
    )


def test_a_freshly_uploaded_scanned_book_is_partial_not_ready(db, tmp_path, monkeypatch):
    """THE SERIOUS HALF of the live bug (verified against the tutor's real
    library), and the reason status must be correct at upload, not only after
    an OCR run: uploading a PDF deliberately does NOT auto-start OCR (see
    `reocr_source`'s docstring — an 8-12 hour run against a shared subscription
    cap must be a button the tutor presses, never a side effect of a file
    landing). So the FIRST status this source ever gets is the one
    `ingest_source` writes right here, and until this fix it was "ready":
    `extract_text` re-extracts the very GlyphLessFont layer `paginate_source`
    just refused (the fixture above) and its non-zero char_count alone decided
    D6's ready/empty split — with zero pages actually read by anything
    trustworthy. A tutor who searched his brand-new book got "4-inch stereo
    cables" back under a healthy green checkmark, with nothing anywhere
    suggesting a button existed to press. That window was indefinite, not
    transient — nothing else in this app was ever going to re-visit a "ready"
    source on its own.

    `partial` is now forced whenever a source still has a page `pending`/
    `ocr_running` (SOURCE_USABLE_STATUSES's docstring in models/knowledge.py):
    usable, such as it is, but visibly and honestly incomplete — which is what
    should have been prompting the tutor to press Re-read all along."""
    src = _upload(db, monkeypatch, tmp_path, [_REFUSED_LAYER])

    reloaded = db.get(KnowledgeSource, src.id)
    assert reloaded.status == "partial"  # NOT "ready" — the one page is still `pending`
    page = _pages_of(db, src)[0]
    assert page.status == "pending"


def test_a_page_that_ends_EMPTY_keeps_no_chunks_of_the_text_we_refused(
    db, tmp_path, monkeypatch
):
    """THE NASTIEST VARIANT, because the book still rolls up GREEN: `empty` pages
    are not counted as failures (`_rollup_source_status`), so a book whose only
    casualty is an `empty` page shows the tutor a healthy checkmark — while
    retrieval serves him Tesseract's "4-inch" as a citation with a real page
    number on it, and curriculum (which reads `Page.text` -> NULL) skips the same
    page. Silent, citable, permanently disagreeing consumers."""
    src = _upload(db, monkeypatch, tmp_path, [_REFUSED_LAYER])
    fake = _Vision(["There is no visible text on this page."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    page = _pages_of(db, src)[0]
    assert page.status == "empty"
    assert page.text is None
    assert _chunks_of(db, page) == [], "an unread page must not still be serving the refused layer"


def test_a_page_that_ends_FAILED_keeps_no_chunks_of_the_text_we_refused(
    db, tmp_path, monkeypatch
):
    """`_mark_failed` rolls back and writes a status; it never deleted the stale
    chunks. So a page we could not read went on serving someone else's OCR
    forever, while `Page.text` said NULL."""
    src = _upload(db, monkeypatch, tmp_path, [_REFUSED_LAYER])
    fake = _Vision([RuntimeError("vl timeout"), RuntimeError("vl timeout")])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    page = _pages_of(db, src)[0]
    assert page.status == "failed"
    assert page.text is None
    assert _chunks_of(db, page) == []


def test_a_rate_limited_page_keeps_no_chunks_of_the_text_we_refused(
    db, tmp_path, monkeypatch
):
    """The third terminal path: a 429 parks the run and puts the page back to
    `pending`. It spent an attempt on this page and refused its text; it must not
    leave the refused text behind in the index either."""
    from app.llm.errors import LLMError

    src = _upload(db, monkeypatch, tmp_path, [_REFUSED_LAYER])
    fake = _Vision([LLMError("rate_limit", "429 slow down")])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    page = _pages_of(db, src)[0]
    assert page.status == "pending"          # parked, will be retried
    assert page.text is None
    assert _chunks_of(db, page) == []


def test_a_page_that_reaches_READY_serves_only_the_words_we_actually_read(
    db, tmp_path, monkeypatch
):
    """The healing path, pinned rather than assumed: `_embed_page` deletes by
    `page_id` before re-adding, so a page that reaches `ready` replaces the
    refused layer instead of stacking on it. Chat must not be able to answer
    "4-inch" for a page Claude has read."""
    src = _upload(db, monkeypatch, tmp_path, [_REFUSED_LAYER])
    fake = _Vision([_WHAT_CLAUDE_READS])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    page = _pages_of(db, src)[0]
    assert page.status == "ready"
    chunks = _chunks_of(db, page)
    assert chunks, "a ready page must be citable"
    assert not any("4-inch" in c for c in chunks), "the refused layer survived a clean read"
    assert any("¼-inch" in c for c in chunks)


def test_an_IMAGE_REGION_page_whose_figure_fails_KEEPS_its_publisher_chunks(
    db, tmp_path, monkeypatch
):
    """THE LOAD-BEARING GUARD, and the reason the invariant is stated on `text`
    rather than on `status`.

    An `image_region` page (Powers' 43 tab pages) keeps its PUBLISHER text — real,
    correct, free. Only the figure description failed. A blanket "non-ready pages
    have no chunks" rule would strip all 43 of those pages out of the index the
    first time a tab picture failed to describe, silently deleting content the
    model never even disputed."""
    src = _upload(db, monkeypatch, tmp_path, [_REFUSED_LAYER], kind="digital", images=True)
    page_before = _pages_of(db, src)[0]
    assert page_before.ocr_reason == "image_region"
    assert page_before.text is not None, "the publisher's text is kept — that is the point"
    assert _chunks_of(db, page_before), "and it is indexed, correctly, from the upload"

    fake = _Vision([RuntimeError("vl timeout"), RuntimeError("vl timeout")])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    page = _pages_of(db, src)[0]
    assert page.status == "failed"           # the PICTURE failed
    assert page.text is not None             # the WORDS did not
    assert _chunks_of(db, page), "a failed figure must not delete the publisher's own indexed text"


def test_the_invariant_holds_across_a_whole_mixed_book_read_end_to_end(
    db, tmp_path, monkeypatch
):
    """The real shape of the 888-page run: some pages read, some blank, some
    unreadable — uploaded through `ingest_source`, so every page starts out
    carrying the refused layer and the invariant has something to fail against.

    Asserted on `text`, in both directions, for every page of the book."""
    src = _upload(db, monkeypatch, tmp_path, [_REFUSED_LAYER] * 4)
    fake = _Vision([
        _WHAT_CLAUDE_READS,                          # p1 -> ready
        "There is no visible text on this page.",    # p2 -> empty
        RuntimeError("vl timeout"),                  # p3 -> failed (initial)
        RuntimeError("vl timeout"),                  # p3 -> failed (retry)
        _WHAT_CLAUDE_READS,                          # p4 -> ready
    ])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    pages = _pages_of(db, src)
    assert [p.status for p in pages] == ["ready", "empty", "failed", "ready"]
    for page in pages:
        chunks = _chunks_of(db, page)
        if page.text is None:
            assert chunks == [], (
                f"page {page.page_no} has no text but is still serving {len(chunks)} chunk(s) "
                "of the layer we refused — curriculum reads NULL, retrieval reads Tesseract"
            )
        if page.status == "ready":
            assert chunks, f"page {page.page_no} is ready but has nothing to cite"
    # and nowhere in the book does the refused layer survive a completed read
    survivors = [c.text for c in db.query(Chunk).filter_by(source_id=src.id).all()
                 if "4-inch" in c.text]
    assert survivors == [], f"{len(survivors)} chunk(s) of refused Tesseract still indexed"
