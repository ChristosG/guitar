"""The lesson fan-out, live progress, and Resume (Plan 13, Stage 6.7).

THE FLAGSHIP PROOF, in unit form. What has to be true:

  * 20 lessons draft concurrently, in TEACHING ORDER, so module 1 is readable while
    module 5 is still being written.
  * A failed lesson is `failed` AND THE OTHER 19 FINISH. One bad lesson must not
    take the curriculum down.
  * A 429 goes back to `queued`, NOT `failed`. On a fresh Anthropic account,
    concurrent 32k-output drafts hit a per-minute output limit — and if that marked
    lessons `failed`, half the curriculum would die because the tutor generated it
    too fast, on exactly the demo this feature exists for.
  * A RESTART mid-draft leaves drafted lessons intact and interrupted ones `queued`
    (not `failed` — a restart is not a bad lesson), and Resume finishes only the
    remainder.
  * Progress is a GROUP BY over the blocks, never a counter on the job row.
"""
import uuid

import pytest
from sqlalchemy import func, select

import app.curriculum.corpus as corpus_mod
import app.curriculum.draft as draft_mod
import app.jobs.curriculum_draft as fanout_mod
from app.curriculum.corpus import build_library_context
from app.curriculum.depth import SECTION_WEIGHTS, SECTIONS
from app.curriculum.draft import draft_progress
from app.curriculum.outline import materialize_outline
from app.curriculum.shape import plan_shape
from app.jobs.curriculum_draft import run_curriculum_draft_job
from app.jobs.sweep import sweep_interrupted_lessons
from app.llm.errors import LLMError
from app.models.block import Block
from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource, Page

SHAPE = plan_shape(8, 1, 50)   # 8 lessons / 2 modules x 4


def _full_lesson(title="Drafted") -> dict:
    lesson = {"title": title, "summary": "Two sentences."}
    for name in SECTIONS:
        n = int(SECTION_WEIGHTS[name] * 2200)
        section = {"body": " ".join(["word"] * n), "citations": []}
        if name == "exercises":
            section["items"] = []
        if name == "qa_prompts":
            section["items"] = [{"question": "q", "answer_key": "a"}]
        lesson[name] = section
    return lesson


class _Provider:
    """Drafts every lesson, unless its title is in `fail_on` / `rate_limit_on`."""

    def __init__(self, *, fail_on=(), rate_limit_on=()):
        self.fail_on = set(fail_on)
        self.rate_limit_on = set(rate_limit_on)
        self.drafted: list[str] = []

    def guided_json(self, messages, schema, *, temperature=0.2, role="spec", max_tokens=None):
        sent = messages[-1]["content"]
        for title in self.rate_limit_on:
            if f"LESSON: {title}" in sent:
                raise LLMError("rate_limit", "429")
        for title in self.fail_on:
            if f"LESSON: {title}" in sent:
                raise RuntimeError("the model exploded")
        self.drafted.append(sent)
        return _full_lesson()

    def count_tokens(self, text: str) -> int:
        return len(text) // 3 + 1


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    provider = _Provider()
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)
    return provider


def _book(db) -> KnowledgeSource:
    source = KnowledgeSource(type="pdf", title="Book", language="en", status="ready")
    db.add(source)
    db.flush()
    db.add(Page(source_id=source.id, page_no=19,
                text="A humbucker cancels hum by pairing opposed coils. " * 4,
                status="ready"))
    db.commit()
    return source


def _outline() -> dict:
    return {
        "title": "Tone Fundamentals",
        "modules": [
            {
                "title": f"Module {i}", "objective": "o", "tier": "library",
                "coverage_note": "p.19",
                "lessons": [
                    {"title": f"L{i}.{j}", "objective": "o", "est_minutes": 50}
                    for j in range(4)
                ],
            }
            for i in range(2)
        ],
    }


def _course(db) -> uuid.UUID:
    source = _book(db)
    return materialize_outline(
        db, _outline(), title="Tone Fundamentals", language="en", shape=SHAPE,
        library=build_library_context(db, [source.id]), source_ids=[source.id],
    )


def _job(db, root_id) -> uuid.UUID:
    job = GenerationJob(
        kind="curriculum_draft", status="pending", params={"root_id": str(root_id)},
    )
    db.add(job)
    db.commit()
    return job.id


def _lessons(db, root_id) -> list[Block]:
    module = Block.__table__.alias("m")
    return db.scalars(
        select(Block)
        .join(module, Block.parent_id == module.c.id)
        .where(module.c.parent_id == root_id, Block.kind == "lesson")
        .order_by(module.c.order, Block.order)
    ).all()


def _status(db, root_id) -> list[str]:
    return [(l.meta or {}).get("draft_status") for l in _lessons(db, root_id)]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_every_queued_lesson_is_drafted_and_the_job_finishes(db, _provider):
    root_id = _course(db)
    job_id = _job(db, root_id)

    run_curriculum_draft_job(job_id)

    db.expire_all()
    assert _status(db, root_id) == ["ready"] * 8
    assert len(_provider.drafted) == 8

    job = db.get(GenerationJob, job_id)
    assert job.status == "succeeded"
    assert job.error is None
    assert job.result_root_id == root_id
    assert job.progress["phase"] == "done"


def test_lessons_are_drafted_in_teaching_order_so_module_one_finishes_first(
    db, _provider, monkeypatch
):
    """He opens the board and reads module 1 while module 5 is still being written.
    Drafting in tree order is what makes the first thing he looks at the first thing
    that arrives.

    The property under test is the SUBMISSION policy (tree order), so the pool is
    pinned to one worker: at the production width of 6, which of the first six
    submitted lessons reaches the fake provider first is thread-scheduling luck,
    and this assertion flickered under a loaded suite."""
    monkeypatch.setattr(fanout_mod.settings, "draft_concurrency_api", 1)
    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))

    firsts = [sent for sent in _provider.drafted[:2]]
    assert all("Module 0" in s for s in firsts), "module 0's lessons must be drafted first"


def test_a_drafted_lesson_carries_its_word_count_and_its_segments(db):
    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))

    db.expire_all()
    lesson = _lessons(db, root_id)[0]
    assert lesson.meta["draft_status"] == "ready"
    assert lesson.meta["word_count"] >= 1760
    assert lesson.meta["meets_floor"] is True
    segments = db.scalars(select(Block).where(Block.parent_id == lesson.id)).all()
    assert len(segments) == len(SECTIONS)


# ---------------------------------------------------------------------------
# Failure is per-lesson
# ---------------------------------------------------------------------------

def test_one_failed_lesson_does_not_take_the_other_seven_down(db, monkeypatch):
    provider = _Provider(fail_on={"L0.2"})
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    root_id = _course(db)
    job_id = _job(db, root_id)

    run_curriculum_draft_job(job_id)

    db.expire_all()
    statuses = _status(db, root_id)
    assert statuses.count("ready") == 7
    assert statuses.count("failed") == 1

    failed = next(l for l in _lessons(db, root_id) if l.meta["draft_status"] == "failed")
    assert failed.title == "L0.2"
    assert failed.meta["error"]

    # PARTIAL SUCCESS IS SUCCESS. Seven good lessons plus one row with a Retry
    # button — not a failed job with a red banner over seven good lessons.
    job = db.get(GenerationJob, job_id)
    assert job.status == "succeeded"
    assert "1 lesson(s) could not be drafted" in job.error


def test_a_429_puts_the_lesson_back_to_QUEUED_never_failed(db, monkeypatch):
    """THE rate-limit rule. A `failed` here would kill half a curriculum because the
    tutor generated it too fast — on a fresh Anthropic account, on the demo this
    feature exists for. `queued` is one Resume click from finished."""
    provider = _Provider(rate_limit_on={"L0.1", "L1.3"})
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))

    db.expire_all()
    statuses = _status(db, root_id)
    assert statuses.count("queued") == 2
    assert statuses.count("failed") == 0, "a 429 is NOT a failure"
    assert statuses.count("ready") == 6


def test_a_job_where_nothing_could_be_drafted_at_all_is_a_real_failure(db, monkeypatch):
    provider = _Provider(fail_on={f"L{i}.{j}" for i in range(2) for j in range(4)})
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    root_id = _course(db)
    job_id = _job(db, root_id)

    run_curriculum_draft_job(job_id)

    db.expire_all()
    job = db.get(GenerationJob, job_id)
    assert job.status == "failed"
    assert job.error_kind == "upstream"


# ---------------------------------------------------------------------------
# Progress — a GROUP BY, not a counter
# ---------------------------------------------------------------------------

def test_progress_counts_the_blocks_themselves(db):
    root_id = _course(db)

    before = draft_progress(db, root_id)
    assert before == {
        "total": 8, "queued": 8, "drafting": 0, "ready": 0, "failed": 0, "done": False,
    }

    run_curriculum_draft_job(_job(db, root_id))
    db.expire_all()

    after = draft_progress(db, root_id)
    assert after["ready"] == 8
    assert after["done"] is True


def test_a_curriculum_with_a_failed_lesson_is_still_DONE(db, monkeypatch):
    """19 ready + 1 failed IS done. The tutor gets his 19 lessons and a visible
    failure he can retry — not a progress bar stuck at 95% forever."""
    provider = _Provider(fail_on={"L0.2"})
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))
    db.expire_all()

    report = draft_progress(db, root_id)
    assert report["failed"] == 1
    assert report["done"] is True


def test_progress_endpoint_serves_the_board(db, client):
    root_id = _course(db)

    r = client.get(f"/curricula/{root_id}/progress")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 8
    assert body["queued"] == 8
    assert body["done"] is False


def test_progress_on_an_unknown_curriculum_is_a_404_not_a_zeroed_report(db, client):
    r = client.get(f"/curricula/{uuid.uuid4()}/progress")
    assert r.status_code == 404


def test_progress_surfaces_the_latest_failed_draft_jobs_reason(db, client):
    """No more silent "processing…". When the draft run for this curriculum FAILED,
    its reason rides the progress poll so the board can show why the lessons that
    are still queued never got written."""
    root_id = _course(db)

    # No draft run yet — nothing to surface.
    r = client.get(f"/curricula/{root_id}/progress")
    assert r.json()["draft_error"] is None

    failed = GenerationJob(
        kind="curriculum_draft", status="failed", error_kind="upstream",
        error="No lesson could be drafted. Check Settings, then Resume.",
        params={"root_id": str(root_id)}, result_root_id=root_id,
    )
    db.add(failed)
    db.commit()

    r = client.get(f"/curricula/{root_id}/progress")
    assert r.json()["draft_error"] == "No lesson could be drafted. Check Settings, then Resume."

    # A LATER draft run that succeeds clears it — the newest row is the successful one.
    ok = GenerationJob(
        kind="curriculum_draft", status="succeeded", error=None,
        params={"root_id": str(root_id)}, result_root_id=root_id,
    )
    db.add(ok)
    db.commit()

    r = client.get(f"/curricula/{root_id}/progress")
    assert r.json()["draft_error"] is None


# ---------------------------------------------------------------------------
# RESTART + RESUME — the flagship recovery story
# ---------------------------------------------------------------------------

def test_the_boot_sweep_puts_interrupted_lessons_back_to_queued_not_failed(db):
    """A lesson mid-draft when the container restarted has nothing WRONG with it —
    no model judged it, no content is bad. Marking it `failed` would paint the
    tutor's board red after a routine deploy and tell him his curriculum broke, when
    the truth is "click Resume"."""
    root_id = _course(db)
    lessons = _lessons(db, root_id)
    # Simulate the restart: two lessons were in flight.
    for lesson in lessons[:2]:
        lesson.meta = {**lesson.meta, "draft_status": "drafting"}
    lessons[2].meta = {**lessons[2].meta, "draft_status": "ready", "word_count": 2000}
    db.commit()

    swept = sweep_interrupted_lessons(db)

    assert swept == 2
    db.expire_all()
    statuses = _status(db, root_id)
    assert statuses.count("queued") == 7
    assert statuses.count("failed") == 0, "a restart is not a bad lesson"
    assert statuses.count("ready") == 1, "an already-drafted lesson SURVIVES the restart"


def test_an_interrupted_rewrite_with_existing_text_goes_back_to_ready_not_queued(db):
    """The lesson panel's apply and the revise engine's re-draft both set `drafting`
    on a lesson that ALREADY HAS TEXT — it is being REWRITTEN, not drafted from
    nothing. A restart mid-rewrite must leave that text alone: `ready`, not
    `queued`, because `queued` is what tells Resume to draft the lesson from
    scratch and would throw away a perfectly good lesson body."""
    root_id = _course(db)
    lessons = _lessons(db, root_id)

    # This lesson was mid-REWRITE: `drafting`, but it already has a segment with a
    # real body underneath it (the text the rewrite was revising).
    rewriting = lessons[0]
    rewriting.meta = {**rewriting.meta, "draft_status": "drafting"}
    db.add(Block(
        parent_id=rewriting.id, order=0, kind="segment", title="Intro",
        body="Some already-drafted lesson text.",
    ))

    # This lesson was mid-DRAFT from nothing: `drafting`, no segments at all yet.
    drafting_fresh = lessons[1]
    drafting_fresh.meta = {**drafting_fresh.meta, "draft_status": "drafting"}
    db.commit()

    swept = sweep_interrupted_lessons(db)

    assert swept == 2
    db.expire_all()
    rewriting_after = db.get(Block, rewriting.id)
    assert rewriting_after.meta["draft_status"] == "ready"
    # NO error line on the finished lesson: its text survived the restart, and
    # `meta.error` is rendered as a red sentence that nothing would ever clear.
    assert rewriting_after.meta["error"] is None

    fresh_after = db.get(Block, drafting_fresh.id)
    assert fresh_after.meta["draft_status"] == "queued"
    assert fresh_after.meta["error"] == "interrupted by a restart — press Resume"


def test_resume_drafts_only_the_remainder(db, _provider):
    """The flagship proof: drafted lessons survive, and Resume finishes only what is
    left — it does not re-draft (or re-bill) the fifteen that already landed."""
    root_id = _course(db)
    lessons = _lessons(db, root_id)
    for lesson in lessons[:5]:
        lesson.meta = {**lesson.meta, "draft_status": "ready", "word_count": 2000}
    db.commit()

    run_curriculum_draft_job(_job(db, root_id))

    db.expire_all()
    assert _status(db, root_id) == ["ready"] * 8
    assert len(_provider.drafted) == 3, "only the 3 still-queued lessons were drafted"


def test_resume_is_a_request_that_schedules_a_background_task(db, client, monkeypatch):
    """Resume is a BUTTON. A `BackgroundTask` can only be scheduled by a request —
    there is no worker process, and this stage deliberately did not add one."""
    import app.routers.curriculum as router_mod

    scheduled = []
    monkeypatch.setattr(router_mod, "run_curriculum_draft_job", scheduled.append)

    root_id = _course(db)
    r = client.post(f"/curricula/{root_id}/draft")

    assert r.status_code == 202, r.text
    job_id = uuid.UUID(r.json()["job_id"])
    assert scheduled == [job_id]

    db.expire_all()
    job = db.get(GenerationJob, job_id)
    assert job.kind == "curriculum_draft"
    assert job.params["root_id"] == str(root_id)


class _SegmentProvider:
    """Minimal fake for `segment_generate.get_provider()` — mirrors
    `test_surgical_revise.py`'s own `_FakeProvider`, trimmed to what this test
    needs."""

    def guided_json(self, messages, schema, *, temperature=0.2, role="draft", max_tokens=None):
        return {"title": "Bonus", "body": "generated body"}


def test_resume_also_drains_a_queued_segment_without_a_second_job(db, client, monkeypatch):
    """Finding #3 (whole-branch review): a segment `apply_revision`'s
    `add_segment`/`edit_segment` queued has no recovery of its own if the
    revise job's own apply-mode chain never generates it (interrupted, or a
    segment-only plan that never chained a draft at all). Resume must drain
    it too, alongside its existing lesson sweep — and without spinning up a
    SECOND/pointless job just for that: only the one `curriculum_draft` job
    row this endpoint always created is created here either way."""
    import app.curriculum.segment_generate as segment_mod
    import app.routers.curriculum as router_mod

    scheduled = []
    monkeypatch.setattr(router_mod, "run_curriculum_draft_job", scheduled.append)
    monkeypatch.setattr(segment_mod, "ground_topic", lambda *a, **k: [])
    monkeypatch.setattr(segment_mod, "get_provider", lambda: _SegmentProvider())

    root_id = _course(db)
    lesson = _lessons(db, root_id)[0]
    seg = Block(kind="segment", title="Bonus", body="", order=99, parent_id=lesson.id,
                language="en", meta={"segment_status": "queued", "segment_instruction": "x"})
    db.add(seg)
    db.commit()
    seg_id = seg.id

    jobs_before = db.scalar(select(func.count()).select_from(GenerationJob))
    r = client.post(f"/curricula/{root_id}/draft")
    assert r.status_code == 202, r.text
    job_id = uuid.UUID(r.json()["job_id"])
    assert scheduled == [job_id]           # the lesson job was still scheduled, unchanged

    db.expire_all()
    seg = db.get(Block, seg_id)
    assert seg.meta["segment_status"] == "done"    # the queued segment was drained
    assert seg.body == "generated body"

    jobs_after = db.scalar(select(func.count()).select_from(GenerationJob))
    assert jobs_after == jobs_before + 1   # exactly the one curriculum_draft row — no second job


def test_resuming_a_finished_curriculum_drafts_nothing_and_costs_nothing(db, _provider):
    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))
    _provider.drafted.clear()

    run_curriculum_draft_job(_job(db, root_id))

    assert _provider.drafted == [], "a Resume with nothing queued must not re-bill him"


def test_a_lesson_the_tutor_adds_after_the_fact_is_picked_up_by_the_next_resume(db, _provider):
    """He reads module 1, sees a lesson missing, adds it, presses Resume. It gets
    written with the same cached library prefix as the other nineteen —
    `draft_status="queued"` is not bookkeeping, it IS the enqueue."""
    from app.curriculum.edit import add_lesson

    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))
    _provider.drafted.clear()

    module = db.scalars(
        select(Block).where(Block.parent_id == root_id, Block.kind == "module")
        .order_by(Block.order)
    ).first()
    add_lesson(db, module.id, title="Barre chords", objective="finally")

    run_curriculum_draft_job(_job(db, root_id))

    assert len(_provider.drafted) == 1
    assert "Barre chords" in _provider.drafted[0]
    db.expire_all()
    assert draft_progress(db, root_id)["total"] == 9


def test_a_deleted_curriculum_fails_the_job_cleanly_rather_than_crashing(db):
    root_id = _course(db)
    job_id = _job(db, root_id)
    db.delete(db.get(Block, root_id))
    db.commit()

    run_curriculum_draft_job(job_id)   # must not raise

    db.expire_all()
    job = db.get(GenerationJob, job_id)
    assert job.status == "failed"
    assert "no longer exists" in job.error


def test_resume_retries_failed_lessons_the_way_every_surface_promises(db, _provider):
    """The job error, the progress bar's failedHint, and the module docstring
    all tell the tutor "press Resume to retry" a failed lesson — but the
    fan-out's work list used to filter on `queued` only, so the button did
    nothing for exactly the lessons it was pointed at."""
    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))

    lessons = _lessons(db, root_id)
    failed = lessons[3]
    failed.meta = {**(failed.meta or {}), "draft_status": "failed", "error": "boom"}
    db.commit()

    before = len(_provider.drafted)
    run_curriculum_draft_job(_job(db, root_id))
    db.expire_all()

    assert (db.get(Block, failed.id).meta or {})["draft_status"] == "ready"
    # And ONLY the failed one was re-billed — the ready nineteen were not.
    assert len(_provider.drafted) == before + 1


# ---------------------------------------------------------------------------
# The tutor's prompt overrides reach the fan-out — resolved ONCE, in Phase A
# ---------------------------------------------------------------------------


def test_the_whole_fanout_writes_lessons_with_his_edited_prompt(db, _provider):
    """END TO END, through the REAL job: he edits the lesson prompt in Settings, and
    every lesson of a 8-lesson fan-out is written with HIS text.

    THIS IS THE TEST THAT PINS `overrides.snapshot`. The workers must not resolve
    anything themselves — `_draft_one` hands its connection back before the model call
    (:215, "This one line is what keeps the progress poll answering"), so a
    `resolve(db, ...)` down in the builder would hold a pool connection across every
    minutes-long `guided_json`, once per worker. Phase A reads the overrides once and
    passes a plain dict, exactly as it already does for `student_brief`.

    So this asserts BOTH halves at once: his text is on the wire (Phase A snapshotted
    it and threaded it), and it got there without the workers querying for it.
    """
    from app.curriculum.draft import LESSON_SLICE_ID, LESSON_TAIL
    from app.prompts import overrides

    sentinel = "ΔΙΔΑΣΚΩ ΠΑΝΤΑ ΜΕ ΤΡΑΓΟΥΔΙΑ ΑΠΟ ΤΟ ΠΡΩΤΟ ΛΕΠΤΟ"
    overrides.save(db, LESSON_SLICE_ID, f"{sentinel}\n{LESSON_TAIL}")

    provider = _provider
    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))

    assert len(provider.drafted) == 8, "the fan-out did not draft every lesson"
    for sent in provider.drafted:
        assert sentinel in sent, "a lesson was drafted with the code default"
    assert _status(db, root_id) == ["ready"] * 8


# ---------------------------------------------------------------------------
# RETRIEVAL GROUNDING — the revise chain, and the former REFUSE case
#
# To CHANGE a lesson the model may use its OWN knowledge: the library is optional
# per-lesson grounding (`ground_topic`), never the whole-library gate that refused.
# ---------------------------------------------------------------------------


def _grounding_job(db, root_id) -> uuid.UUID:
    job = GenerationJob(
        kind="curriculum_draft", status="pending",
        params={"root_id": str(root_id), "grounding": "retrieval"},
    )
    db.add(job)
    db.commit()
    return job.id


def _spy_ground_topic(monkeypatch, counter):
    from app.curriculum.ground import Passage

    def _fake(db, topic, *, source_ids=None, k=5):
        counter.append(topic)
        return [Passage(text="A retrieved passage.", source_id=uuid.uuid4(),
                        source_title="Book", page_no=19, page_id=uuid.uuid4(), score=0.9)]

    monkeypatch.setattr(draft_mod, "ground_topic", _fake)


def test_grounding_retrieval_drafts_via_ground_topic_never_the_whole_library(db, monkeypatch):
    """The revise chain sets `grounding="retrieval"`: every lesson is grounded by
    per-lesson `ground_topic` (all chunks, compiled or not) plus the model's own
    knowledge — the whole-library router is NOT consulted, so it can never refuse."""
    grounded: list[str] = []
    _spy_ground_topic(monkeypatch, grounded)

    def _must_not_run(db, source_ids):
        raise AssertionError("build_curriculum_context must not be consulted for grounding=retrieval")

    monkeypatch.setattr(fanout_mod, "build_curriculum_context", _must_not_run)

    root_id = _course(db)
    run_curriculum_draft_job(_grounding_job(db, root_id))

    db.expire_all()
    assert len(grounded) == 8, "each of the 8 lessons was grounded via retrieval"
    assert _status(db, root_id) == ["ready"] * 8
    job = db.scalars(
        select(GenerationJob).where(GenerationJob.kind == "curriculum_draft")
        .order_by(GenerationJob.created_at.desc())
    ).first()
    assert job.status == "succeeded"


def test_a_too_large_uncompiled_selection_degrades_to_retrieval_not_a_refusal(db, monkeypatch):
    """The former REFUSE case (too large to read whole AND a contributing book not
    compiled) no longer fails the run — it degrades to the SAME per-lesson retrieval.
    No `CurriculumContextError` escapes; the lessons reach `ready`."""
    from app.curriculum.corpus import CurriculumContextError

    grounded: list[str] = []
    _spy_ground_topic(monkeypatch, grounded)

    def _refuse(db, source_ids):
        raise CurriculumContextError(
            "too large + uncompiled", uncompiled=[{"id": "x", "title": "Uncompiled Book"}])

    monkeypatch.setattr(fanout_mod, "build_curriculum_context", _refuse)

    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))       # NO grounding flag — the generation path

    db.expire_all()
    assert len(grounded) == 8, "the refuse case grounded every lesson via retrieval"
    assert _status(db, root_id) == ["ready"] * 8
    job = db.scalars(
        select(GenerationJob).where(GenerationJob.kind == "curriculum_draft")
        .order_by(GenerationJob.created_at.desc())
    ).first()
    assert job.status == "succeeded", "a degrade-to-retrieval run succeeds, it does not fail"
    assert job.error is None


def test_generation_happy_path_still_reads_the_whole_library_not_retrieval(db, monkeypatch):
    """UNCHANGED: a fitting selection (no grounding flag) is read WHOLE — the
    per-lesson retrieval fallback is not consulted at all."""
    grounded: list[str] = []
    _spy_ground_topic(monkeypatch, grounded)

    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))

    db.expire_all()
    assert grounded == [], "a fitting library is read whole; retrieval must not fire"
    assert _status(db, root_id) == ["ready"] * 8


def test_build_retrieval_context_flags_too_large_but_keeps_an_empty_selection_empty(db):
    """`build_retrieval_context` forces `fits=False` on a real library (so the draft
    grounds per-lesson) but returns an empty selection untouched (nothing to
    retrieve — the draft teaches from general knowledge)."""
    from app.curriculum.corpus import build_retrieval_context

    source = _book(db)
    ctx = build_retrieval_context(db, [source.id])
    assert ctx.fits is False
    assert ctx.is_empty is False
    assert ctx.page_index, "the real page index is preserved for citation validation"

    empty = build_retrieval_context(db, [])       # deliberately no sources
    assert empty.is_empty is True


# ---------------------------------------------------------------------------
# THE TUTOR'S SECTIONS SURVIVE A REDRAFT (Unit 1, Task 1.4)
#
# Deepen / Redraft / a `modify_lesson` re-draft all land in `_draft_one`. A
# section he edited by hand is taken OUT of the schema, shown to the model as
# fixed text, and left alone by `persist_lesson` — row, id and marker.
# ---------------------------------------------------------------------------

TUTOR_THEORY = "Αυτό το έγραψε ο καθηγητής με το χέρι και δεν το ξαναγράφει κανείς."


def _hand_edit_theory(db, lesson) -> Block:
    theory = db.scalars(
        select(Block).where(Block.parent_id == lesson.id, Block.kind == "segment")
    ).all()
    seg = next(s for s in theory if (s.meta or {}).get("section") == "theory")
    seg.body = TUTOR_THEORY
    seg.meta = {**(seg.meta or {}),
                "tutor_edited": {"at": "2026-09-12T10:00:00+00:00",
                                 "prev_body": "ό,τι είχε γράψει το AI", "count": 1}}
    return seg


def test_draft_one_keeps_tutor_edited_sections_and_shows_them_as_fixed(db, monkeypatch):
    """He rewrote the theory himself, then pressed Deepen. The model is told the
    theory is HIS and is not given a slot to write it into; everything else is
    rewritten to fit it, and his row comes out the other side unchanged."""
    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))          # a first, ordinary draft
    db.expire_all()

    lesson = _lessons(db, root_id)[0]
    theory = _hand_edit_theory(db, lesson)
    theory_id = theory.id
    warm_up = next(
        s for s in db.scalars(
            select(Block).where(Block.parent_id == lesson.id, Block.kind == "segment")
        ).all() if (s.meta or {}).get("section") == "warm_up"
    )
    warm_up.body = "ΠΑΛΙΟ warm_up"
    # Deepen: the one lesson goes back to `queued`, everything else stays `ready`.
    lesson.meta = {**(lesson.meta or {}), "draft_status": "queued", "deepen": True}
    db.commit()

    captured: dict = {}
    real_draft_lesson = fanout_mod.draft_lesson

    def _capture(db_, **kwargs):
        captured.update(kwargs)
        return real_draft_lesson(db_, **kwargs)

    monkeypatch.setattr(fanout_mod, "draft_lesson", _capture)

    run_curriculum_draft_job(_job(db, root_id))
    db.expire_all()

    # (1) The model was told which sections are his, and was not given a slot for them.
    assert captured["exclude_sections"] == {"theory"}
    assert captured["fixed_sections"] == {"theory": TUTOR_THEORY}

    # (2) His row survived the redraft: same id, same body, still marked his.
    segments = {
        (s.meta or {}).get("section"): s
        for s in db.scalars(
            select(Block).where(Block.parent_id == lesson.id, Block.kind == "segment")
        ).all()
    }
    assert segments["theory"].id == theory_id
    assert segments["theory"].body == TUTOR_THEORY
    assert "tutor_edited" in (segments["theory"].meta or {})

    # (3) The rest of the lesson WAS rewritten, and lost the AI-write marker.
    assert segments["warm_up"].body != "ΠΑΛΙΟ warm_up"
    assert "tutor_edited" not in (segments["warm_up"].meta or {})
    assert (db.get(Block, lesson.id).meta or {}).get("draft_status") == "ready"


def test_a_revise_redraft_does_not_send_a_kept_section_twice(db, monkeypatch):
    """`revise_current` is "here is what the lesson says now, keep the rest intact".
    A kept section is already shown as FIXED text — sending it in both blocks would
    pay for the tutor's theory twice on every revise re-draft."""
    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))
    db.expire_all()

    lesson = _lessons(db, root_id)[0]
    _hand_edit_theory(db, lesson)
    lesson.meta = {**(lesson.meta or {}), "draft_status": "queued",
                   "revise_instruction": "κάνε τις ασκήσεις πιο δύσκολες"}
    db.commit()

    captured: dict = {}
    real_draft_lesson = fanout_mod.draft_lesson

    def _capture(db_, **kwargs):
        captured.update(kwargs)
        return real_draft_lesson(db_, **kwargs)

    monkeypatch.setattr(fanout_mod, "draft_lesson", _capture)

    run_curriculum_draft_job(_job(db, root_id))
    db.expire_all()

    assert captured["fixed_sections"] == {"theory": TUTOR_THEORY}
    assert "theory" not in captured["revise_current"], "shown as fixed, not as 'current'"
    assert "exercises" in captured["revise_current"], "the rest of the lesson is still shown"


# ---------------------------------------------------------------------------
# `lesson_ids` (Unit 3) — a draft job pointed at specific lessons, so the
# guided add-lesson chain can draft ONE new lesson without dragging every
# other straggler under the same root into the same fan-out.
# ---------------------------------------------------------------------------

SMALL_SHAPE = plan_shape(2, 1, 50)  # 2 lessons / 1 module


def _small_outline() -> dict:
    return {
        "title": "Tone Fundamentals",
        "modules": [
            {
                "title": "Module 0", "objective": "o", "tier": "library",
                "coverage_note": "p.19",
                "lessons": [
                    {"title": f"L0.{j}", "objective": "o", "est_minutes": 50}
                    for j in range(2)
                ],
            },
        ],
    }


def _small_course(db) -> uuid.UUID:
    source = _book(db)
    return materialize_outline(
        db, _small_outline(), title="Tone Fundamentals", language="en", shape=SMALL_SHAPE,
        library=build_library_context(db, [source.id]), source_ids=[source.id],
    )


def _job_for(db, root_id, **extra_params) -> uuid.UUID:
    job = GenerationJob(
        kind="curriculum_draft", status="pending",
        params={"root_id": str(root_id), **extra_params},
    )
    db.add(job)
    db.commit()
    return job.id


def test_lesson_ids_restricts_the_fan_out_to_only_those_lessons(db, _provider):
    root_id = _small_course(db)
    l1, l2 = _lessons(db, root_id)

    job_id = _job_for(db, root_id, lesson_ids=[str(l2.id)])
    run_curriculum_draft_job(job_id)

    db.expire_all()
    l1_after = db.get(Block, l1.id)
    l2_after = db.get(Block, l2.id)
    assert l1_after.meta.get("draft_status") == "queued", "l1 was not in lesson_ids"
    assert l2_after.meta["draft_status"] == "ready"
    # On the LESSON: line — the drafted lesson's own identity. L0.0 DOES appear in
    # L0.1's prompt since Task 3.2, as its previous lesson, which is the point of
    # the neighbours block and not a second lesson being drafted.
    assert any("LESSON: L0.1" in sent for sent in _provider.drafted)
    assert not any("LESSON: L0.0" in sent for sent in _provider.drafted)
    assert any("ΠΡΟΗΓΟΥΜΕΝΟ ΜΑΘΗΜΑ: L0.0" in sent for sent in _provider.drafted)

    job = db.get(GenerationJob, job_id)
    assert job.status == "succeeded"

    # The report still counts the WHOLE root, not just the wanted lesson(s).
    progress = draft_progress(db, root_id)
    assert progress["queued"] == 1


def test_lesson_ids_finalize_fails_only_when_every_wanted_lesson_failed(db, monkeypatch):
    """The root still has an un-attempted (queued) lesson, so the fan-out did not
    finish the whole curriculum — but the finalize verdict is scoped to
    `lesson_ids`, not the whole root: the ONE lesson the job was asked to draft
    failing is what makes THIS job `failed`, not the untouched sibling."""

    class _FailingProvider:
        def guided_json(self, messages, schema, *, temperature=0.2, role="spec", max_tokens=None):
            raise LLMError("upstream", "the model answered with garbage")

        def count_tokens(self, text: str) -> int:
            return len(text) // 3 + 1

    provider = _FailingProvider()
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    root_id = _small_course(db)
    l1, l2 = _lessons(db, root_id)

    job_id = _job_for(db, root_id, lesson_ids=[str(l2.id)])
    run_curriculum_draft_job(job_id)

    db.expire_all()
    l1_after = db.get(Block, l1.id)
    l2_after = db.get(Block, l2.id)
    assert l1_after.meta.get("draft_status") == "queued", "never attempted — not in lesson_ids"
    assert l2_after.meta["draft_status"] == "failed"

    job = db.get(GenerationJob, job_id)
    assert job.status == "failed"


# ---------------------------------------------------------------------------
# Task 3.2 — THE NEIGHBOURS MAP AND THE TUTOR'S PER-LESSON BRIEF
#
# `_positions` told every lesson WHERE it sits and ordered it not to re-teach
# what came before — without ever showing it what that was. `_neighbours` is the
# missing half: the titles and objectives on either side, and the module's other
# lessons, computed once in Phase A exactly like the positions map.
# ---------------------------------------------------------------------------

def test_neighbours_gives_each_lesson_the_titles_on_either_side_of_it(db):
    root_id = _course(db)
    lessons = _lessons(db, root_id)          # 2 modules x 4, in teaching order

    n = fanout_mod._neighbours(db, root_id)

    middle = n[str(lessons[1].id)]           # L0.1
    assert middle["prev"] == "L0.0 — o"
    assert middle["next"] == "L0.2 — o"
    assert middle["siblings"] == "L0.0, L0.2, L0.3"
    # The edges say «—» rather than pretending a lesson is there.
    assert n[str(lessons[0].id)]["prev"] == "—"
    assert n[str(lessons[3].id)]["next"] == "—"
    # A module's lessons never see the next module's — that is what POSITION is for.
    assert n[str(lessons[4].id)]["prev"] == "—"
    assert "L0.3" not in n[str(lessons[4].id)]["siblings"]


def test_the_fanout_hands_every_worker_its_neighbours_and_the_tutors_brief(db, monkeypatch):
    """What Phase A computed reaches the drafting call — the neighbours for THIS
    lesson, and the brief the tutor wrote on it (`meta.brief`)."""
    root_id = _course(db)
    run_curriculum_draft_job(_job(db, root_id))
    db.expire_all()

    lesson = _lessons(db, root_id)[1]        # L0.1, a lesson with both neighbours
    lesson.meta = {**(lesson.meta or {}), "draft_status": "queued",
                   "brief": "Ξύλο μπράτσου: Maple έναντι Mahogany."}
    db.commit()

    captured: dict = {}
    real_draft_lesson = fanout_mod.draft_lesson

    def _capture(db_, **kwargs):
        captured.update(kwargs)
        return real_draft_lesson(db_, **kwargs)

    monkeypatch.setattr(fanout_mod, "draft_lesson", _capture)
    run_curriculum_draft_job(_job(db, root_id))

    assert captured["tutor_brief"] == "Ξύλο μπράτσου: Maple έναντι Mahogany."
    assert captured["neighbours"] == {"prev": "L0.0 — o", "next": "L0.2 — o",
                                      "siblings": "L0.0, L0.2, L0.3"}
