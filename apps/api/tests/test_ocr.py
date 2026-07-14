import pytest
from app.brain.ocr import OCR_PROMPT, ocr_source
from app.models.knowledge import Chunk, KnowledgeSource, Page


class _Vision:
    """Fake provider: scripted per-call vision() results."""
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def vision(self, image_bytes, prompt, *, media_type="image/jpeg"):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def embed(self, texts, *, is_query=False):
        return [[0.1] * 2560 for _ in texts]


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
        return [[0.1] * 2560 for _ in texts]


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
    fake = _Vision(["Page one about humbuckers.", "Page two about tube screamers."])
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
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
    fake = _Vision(["Good page.", RuntimeError("vl timeout"), RuntimeError("vl timeout")])
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert (result.total, result.ready, result.failed) == (2, 1, 1)
    pages = db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()
    assert pages[0].status == "ready"          # page 1 survived page 2's failure
    assert pages[0].text == "Good page."
    assert pages[1].status == "failed"
    assert "vl timeout" in pages[1].ocr_error
    assert fake.calls == 3                     # 1 + (1 initial + 1 retry)


def test_a_page_the_model_reads_as_blank_is_empty_not_ready(db, tmp_path, monkeypatch):
    src = _src_with_pending_pages(db, 1)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
    (p / "0001.jpg").write_bytes(b"jpeg")
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: _Vision([""]))
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: _Vision([""]))

    result = ocr_source(db, src.id)

    assert (result.ready, result.failed) == (0, 0)
    assert db.query(Page).filter_by(source_id=src.id).one().status == "empty"


def test_ocr_prompt_forbids_invention():
    # a transcriber that "helpfully" fills gaps corrupts the ground truth the
    # whole app is supposed to trust
    assert "verbatim" in OCR_PROMPT.lower()


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
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
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
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
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
        ["Page one text.", "Page two text.", "Page three text."],
        fail_calls={1},   # embed() raises only for page 1's chunks
    )
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
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
    fake = _VisionWithFailingEmbed(["Page one.", "Page two."], fail_calls={1})
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
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
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
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
    fake = _Vision(["Page two text."])
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
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
    fake = _Vision(["Page one about humbuckers.", "Page two about tube screamers."])
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
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
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    reloaded = db.get(KnowledgeSource, src.id)
    assert reloaded.status == "empty"
    assert reloaded.char_count == 0


def test_ocr_source_partial_success_rolls_up_to_ready_counting_only_ready_pages(
    db, tmp_path, monkeypatch
):
    src = _src_with_pending_pages(db, 2)
    monkeypatch.setattr("app.brain.ocr.settings.media_dir", str(tmp_path))
    for i in (1, 2):
        p = tmp_path / str(src.id); p.mkdir(exist_ok=True)
        (p / f"{i:04d}.jpg").write_bytes(b"jpeg")
    # page 1 succeeds; page 2 fails twice (initial + one retry)
    fake = _Vision(["Good page text.", RuntimeError("vl timeout"), RuntimeError("vl timeout")])
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    ocr_source(db, src.id)

    reloaded = db.get(KnowledgeSource, src.id)
    pages = db.query(Page).filter_by(source_id=src.id).order_by(Page.page_no).all()
    assert pages[0].status == "ready"
    assert pages[1].status == "failed"
    assert reloaded.status == "ready"
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
    fake = _Vision(["Some transcribed text."])
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
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
    assert page.text == "Some transcribed text."


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
    fake = _Vision(["Recovered page text."])
    monkeypatch.setattr("app.brain.ocr.get_provider", lambda: fake)
    monkeypatch.setattr("app.brain.ocr.get_embedder", lambda: fake)

    result = ocr_source(db, src.id)

    assert (result.total, result.ready, result.failed) == (1, 1, 0)
    assert fake.calls == 1
    reloaded = db.get(Page, page.id)
    assert reloaded.status == "ready"
    assert reloaded.text == "Recovered page text."
