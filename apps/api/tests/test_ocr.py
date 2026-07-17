import fitz
import pytest
from app.brain.ocr import (
    FIGURE_MARKER,
    FIGURE_PROMPT,
    MAX_PAGE_ATTEMPTS,
    OCR_PROMPT,
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
    fake = _Vision(["A photo of an amp face; the knobs are labelled VOLUME and MASTER."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    text = db.get(Page, page.id).text
    assert FIGURE_MARKER in text
    # everything BEFORE the marker is the book's own words; everything after is ours
    assert text.split(FIGURE_MARKER, 1)[0].strip() == _PUBLISHER_TEXT


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


def test_both_prompts_ask_for_a_bare_reply_because_claude_p_narrates():
    """`claude -p` returns chatty markdown around a transcription — it narrates
    that it zoomed in, it adds headers. The bridge deliberately adds no
    scrubbing (Task 4), so shaping the reply is this prompt's job. Regex-eating
    a model's prose would eventually eat a line of a real page with it."""
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
    monkeypatch.setattr("app.config.settings.llm_provider", "claude_cli")
    fake = _Vision(["Use a ¼-inch instrument cable between the guitar and the amp head."])
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    page = db.query(Page).filter_by(source_id=src.id).one()
    assert page.text_source == "claude"       # `claude_cli` is Claude — a wallet, not a model


def test_ocr_records_that_qwen_wrote_the_text(db, tmp_path, monkeypatch):
    src = _prep(db, monkeypatch, tmp_path)
    monkeypatch.setattr("app.config.settings.llm_provider", "qwen")
    fake = _Vision(["A page of text transcribed by the local model, long enough to chunk."])
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
    monkeypatch.setattr("app.config.settings.llm_provider", "claude_cli")
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
    monkeypatch.setattr("app.config.settings.llm_provider", "claude_cli")
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
