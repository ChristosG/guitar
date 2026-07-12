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

    result = ocr_source(db, src.id)

    assert (result.ready, result.failed) == (1, 0)
    assert fake.calls == 1
