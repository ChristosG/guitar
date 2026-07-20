"""Task 1 (Spec A, apply side): surgical segment-level ops.

Today the smallest revise op rewrites a whole LESSON (`modify_lesson`). This adds
`add_segment` / `edit_segment` / `remove_segment` — ops that target one SEGMENT
inside a lesson — to the ops schema and `apply_revision`, plus the custom-segment
survival rule in `persist_lesson`: without it, any later lesson redraft would
silently delete a surgically-added segment along with the blueprint sections it
regenerates.

`apply_revision` still keeps its four properties for these ops too: ONE
transaction, ONE commit, rollback-on-exception, ZERO provider calls — apply only
creates/marks/deletes blocks. GENERATING a queued segment's body is a later task
(the job), not this one.

Mirrors test_revise_apply.py's fixture/skip-guard/setup_module conventions.
"""
import uuid

import pytest
from sqlalchemy import func, select, text

from app.db import Base, SessionLocal, engine
from app.models.block import Block
from app.curriculum.blueprint import default_blueprint, section_keys
from app.curriculum.ground import Passage
from app.curriculum.refine import undo_refine
from app.curriculum.segment_generate import generate_segment
from app.jobs.curriculum_revise import run_curriculum_revise_job
from app.llm.errors import LLMError
from app.models.generation_job import GenerationJob
import app.curriculum.revise as revise
import app.curriculum.segment_generate as segment_mod
import app.jobs.curriculum_revise as revise_job

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


def _children(db, parent_id, kind=None):
    q = select(Block).where(Block.parent_id == parent_id)
    if kind:
        q = q.where(Block.kind == kind)
    return db.scalars(q.order_by(Block.order)).all()


@pytest.fixture
def db_and_tree():
    """course (blueprint = code default) -> module -> lesson -> 2 segments
    (theory, exercises). `floor_words=10` on the lesson so `meets_floor`
    recomputation is exercised (8 words to start, under floor)."""
    db = SessionLocal()
    course = Block(kind="course", title="Tone", is_template=True, language="el",
                   meta={"brief": None, "source_ids": None,
                         "gap_policy": "general_knowledge", "blueprint": default_blueprint()})
    db.add(course)
    db.flush()
    module = Block(kind="module", title="M1", parent_id=course.id, order=0, language="el",
                   meta={"objective": "m1"})
    db.add(module)
    db.flush()
    lesson = Block(kind="lesson", title="L1", parent_id=module.id, order=0, language="el",
                   meta={"objective": "l1", "floor_words": 10})
    db.add(lesson)
    db.flush()
    seg1 = Block(kind="segment", title="Theory", body="word word word word word",
                 order=0, parent_id=lesson.id, language="el", meta={"section": "theory"})
    seg2 = Block(kind="segment", title="Exercises", body="word word word",
                 order=1, parent_id=lesson.id, language="el", meta={"section": "exercises"})
    db.add_all([seg1, seg2])
    db.commit()
    yield db, course, module, lesson, seg1, seg2
    db.close()


# ---------------------------------------------------------------------------
# validate_ops
# ---------------------------------------------------------------------------

def test_validate_ops_accepts_the_three_new_ops_with_required_fields(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "New bit",
         "instruction": "add a paragraph", "reason": "r"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "More theory",
         "instruction": "expand", "section_key": "theory", "reason": "r"},
        {"op": "edit_segment", "segment_id": str(seg1.id), "instruction": "simplify", "reason": "r"},
        {"op": "remove_segment", "segment_id": str(seg2.id), "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert [o["op"] for o in out["ops"]] == [
        "add_segment", "add_segment", "edit_segment", "remove_segment",
    ]


def test_validate_ops_rejects_add_segment_with_a_disabled_section_key(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Bad",
         "instruction": "x", "section_key": "not_a_real_section", "reason": "r"}]}
    out = revise.validate_ops(db, course.id, plan)
    assert out["ops"] == []


def test_validate_ops_rejects_edit_and_remove_segment_targeting_a_lesson_id(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    plan = {"summary": "s", "ops": [
        {"op": "edit_segment", "segment_id": str(lesson.id), "instruction": "x", "reason": "r"},
        {"op": "remove_segment", "segment_id": str(lesson.id), "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert out["ops"] == []


def test_validate_ops_rejects_segment_ops_targeting_a_segment_outside_the_course(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    other_course = Block(kind="course", title="Other", is_template=True, language="el", meta={})
    db.add(other_course)
    db.flush()
    other_module = Block(kind="module", title="OM", parent_id=other_course.id, order=0, language="el",
                         meta={})
    db.add(other_module)
    db.flush()
    other_lesson = Block(kind="lesson", title="OL", parent_id=other_module.id, order=0, language="el",
                         meta={})
    db.add(other_lesson)
    db.flush()
    other_seg = Block(kind="segment", title="OS", body="x", order=0, parent_id=other_lesson.id,
                      language="el", meta={})
    db.add(other_seg)
    db.commit()

    plan = {"summary": "s", "ops": [
        {"op": "edit_segment", "segment_id": str(other_seg.id), "instruction": "x", "reason": "r"},
        {"op": "remove_segment", "segment_id": str(other_seg.id), "reason": "r"},
    ]}
    out = revise.validate_ops(db, course.id, plan)
    assert out["ops"] == []


# ---------------------------------------------------------------------------
# apply_revision — add_segment
# ---------------------------------------------------------------------------

def test_apply_add_segment_without_section_key_is_custom(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    out = revise.apply_revision(db, course.id, {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Bonus: Pedal chains",
         "instruction": "explain pedal chaining order", "reason": "r"}]})
    assert out == {"applied": 1, "root_id": str(course.id)}

    segs = _children(db, lesson.id, "segment")
    assert [s.title for s in segs] == ["Theory", "Exercises", "Bonus: Pedal chains"]
    new = segs[-1]
    assert new.body == ""
    assert new.meta["segment_status"] == "queued"
    assert new.meta["segment_instruction"] == "explain pedal chaining order"
    assert new.meta["custom"] is True
    assert new.meta["section"] == "custom:bonus-pedal-chains"

    db.refresh(lesson)
    assert lesson.meta["word_count"] == 8            # 5 + 3, the new segment's body is ""
    assert lesson.meta["meets_floor"] is False        # 8 < floor_words=10


def test_apply_add_segment_with_enabled_section_key(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    revise.apply_revision(db, course.id, {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "More on scales",
         "instruction": "expand theory", "section_key": "theory", "reason": "r"}]})
    new = _children(db, lesson.id, "segment")[-1]
    assert new.meta["section"] == "theory"
    assert "custom" not in new.meta


# ---------------------------------------------------------------------------
# apply_revision — edit_segment, and the undo_refine contract match
# ---------------------------------------------------------------------------

def test_apply_edit_segment_matches_the_refine_contract_and_undo_restores_it(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    revise.apply_revision(db, course.id, {"summary": "s", "ops": [
        {"op": "edit_segment", "segment_id": str(seg1.id), "instruction": "simplify the wording",
         "reason": "r"}]})
    db.refresh(seg1)
    assert seg1.meta["segment_status"] == "queued"
    assert seg1.meta["segment_instruction"] == "simplify the wording"
    assert seg1.meta["prev_body"] == "word word word word word"
    assert seg1.meta["prev_title"] == "Theory"
    assert seg1.meta["refined"] is True
    assert seg1.meta["refine_instruction"] == "simplify the wording"

    db.refresh(lesson)
    assert lesson.meta["word_count"] == 8             # recomputed even though bodies unchanged

    # Simulate the LATER generation step overwriting the segment, then undo it —
    # this is undo_refine (refine.py:179-190) unchanged, proving the meta keys
    # edit_segment stashes are exactly what it restores/strips.
    seg1.body = "the generated replacement body"
    seg1.title = "Renamed by generation"
    db.commit()

    assert undo_refine(seg1) is True
    db.commit()
    db.expire_all()

    seg1 = db.get(Block, seg1.id)
    assert seg1.body == "word word word word word"
    assert seg1.title == "Theory"
    for key in ("prev_body", "prev_title", "refined", "refine_instruction"):
        assert key not in seg1.meta
    # segment_status/segment_instruction are NOT part of undo_refine's strip set —
    # they are left as-is, same as any other meta key it does not know about.
    assert seg1.meta["segment_status"] == "queued"


# ---------------------------------------------------------------------------
# apply_revision — remove_segment
# ---------------------------------------------------------------------------

def test_apply_remove_segment_deletes_renormalises_and_recomputes(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    seg3 = Block(kind="segment", title="Recap", body="word word", order=2, parent_id=lesson.id,
                language="el", meta={"section": "recap"})
    db.add(seg3)
    db.commit()

    out = revise.apply_revision(db, course.id, {"summary": "s", "ops": [
        {"op": "remove_segment", "segment_id": str(seg1.id), "reason": "r"}]})
    assert out["applied"] == 1

    segs = _children(db, lesson.id, "segment")
    assert [s.title for s in segs] == ["Exercises", "Recap"]
    assert [s.order for s in segs] == [0, 1]           # renormalised

    db.refresh(lesson)
    assert lesson.meta["word_count"] == 5              # 3 (Exercises) + 2 (Recap)
    assert lesson.meta["meets_floor"] is False


# ---------------------------------------------------------------------------
# apply_revision — transactionality
# ---------------------------------------------------------------------------

def test_apply_revision_rolls_back_the_whole_plan_on_a_later_bad_op(db_and_tree, monkeypatch):
    """A plan whose second op is invalid AT APPLY TIME (simulated by monkeypatching
    validate_ops to pass everything through unfiltered, standing in for a garbled
    id that slipped past a weakened validate) must roll back the first op too —
    ONE transaction, not partial application (Global Constraint #3)."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    before = {b.id: (b.title, b.order, b.parent_id, b.meta) for b in db.query(Block).all()}

    def passthrough(db_, root_id, raw):
        return {"summary": raw.get("summary") or "", "ops": raw.get("ops") or []}

    monkeypatch.setattr(revise, "validate_ops", passthrough)

    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Will be rolled back",
         "instruction": "x", "reason": "r"},
        {"op": "remove_segment", "segment_id": str(uuid.uuid4()),
         "reason": "bogus id — passed a weakened validate"},
    ]}
    with pytest.raises(Exception):
        revise.apply_revision(db, course.id, plan)
    db.rollback()

    after = {b.id: (b.title, b.order, b.parent_id, b.meta) for b in db.query(Block).all()}
    assert after == before


# ---------------------------------------------------------------------------
# persist_lesson — custom-segment survival
# ---------------------------------------------------------------------------

def _drafted_lesson() -> dict:
    """A minimal but schema-valid drafted lesson for the default blueprint —
    trimmed copy of test_blueprint_regression.py's `_lesson()` fixture shape."""
    def prose(n):
        return {"body": " ".join(["word"] * n), "citations": []}

    return {
        "title": "Sample lesson title here",
        "summary": " ".join(["sum"] * 12),
        "warm_up": prose(40),
        "theory": prose(500),
        "demonstration": prose(300),
        "exercises": {
            "body": " ".join(["ex"] * 120),
            "items": [
                {"title": "Ex one", "instructions": " ".join(["do"] * 120), "est_minutes": 5},
                {"title": "Ex two", "instructions": " ".join(["do"] * 80), "est_minutes": 5},
            ],
            "citations": [],
        },
        "common_mistakes": prose(90),
        "recap": prose(30),
        "homework": prose(35),
        "qa_prompts": {
            "body": " ".join(["qa"] * 20),
            "items": [
                {"question": " ".join(["q"] * 10), "answer_key": " ".join(["a"] * 15)},
            ],
            "citations": [],
        },
    }


def test_persist_lesson_preserves_custom_segments_appended_after(db):
    from app.curriculum.corpus import LibraryContext
    from app.curriculum.depth import measure
    from app.curriculum.draft import persist_lesson

    lesson_block = Block(kind="lesson", title="L1", language="el", meta={})
    db.add(lesson_block)
    db.flush()

    # A pre-existing blueprint segment that WILL be regenerated/replaced by the redraft.
    old_theory = Block(kind="segment", title="Old Theory", body="stale", order=0,
                       parent_id=lesson_block.id, language="el", meta={"section": "theory"})
    # A custom segment surgically added by add_segment (Task 1) — must SURVIVE.
    custom = Block(kind="segment", title="Bonus bit", body="", order=1,
                  parent_id=lesson_block.id, language="el",
                  meta={"custom": True, "section": "custom:bonus-bit",
                        "segment_status": "queued", "segment_instruction": "explain X"})
    db.add_all([old_theory, custom])
    db.commit()

    bp = default_blueprint()
    lesson = _drafted_lesson()
    library = LibraryContext(text="", token_count=0, fits=True)
    m = measure(lesson, bp, teaching_minutes=40)
    persist_lesson(db, lesson_block, lesson, m, library, bp, qa_minutes=10, teaching_minutes=40)
    db.commit()
    db.expire_all()

    lesson_block = db.get(Block, lesson_block.id)
    segments = sorted(lesson_block.children, key=lambda b: b.order)

    keys = [s.meta.get("section") for s in segments]
    assert keys[:-1] == list(section_keys(bp))         # blueprint sections regenerated, in order
    assert keys[-1] == "custom:bonus-bit"              # custom segment appended AFTER them

    assert segments[-1].id == custom.id                # the SAME row — not deleted/recreated
    assert segments[-1].body == ""                     # untouched by the redraft
    assert segments[-1].title == "Bonus bit"

    assert [s.order for s in segments] == list(range(len(segments)))   # sequential order


# ---------------------------------------------------------------------------
# Task 2 (Spec A, generation side): generate_segment
# ---------------------------------------------------------------------------

class _FakeProvider:
    """Mirrors `test_curriculum_editing.py`'s own `_FakeProvider` — a fixed
    result, captured calls."""

    def __init__(self, result=None):
        self.result = result or {"title": "New bit", "body": "word word word word"}
        self.calls: list[dict] = []

    def guided_json(self, messages, schema, *, temperature=0.2, role="draft", max_tokens=None):
        self.calls.append({"messages": messages, "role": role})
        return self.result


def _sample_passage(page: int = 12) -> Passage:
    return Passage(
        text="Practice the pedal chain in the same order every time.",
        source_id=uuid.uuid4(), source_title="Pedalboard Handbook",
        page_no=page, page_id=uuid.uuid4(), score=0.8,
    )


def test_generate_segment_fills_body_grounds_and_recomputes_word_count(db_and_tree, monkeypatch):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    passage = _sample_passage()
    monkeypatch.setattr(segment_mod, "ground_topic", lambda *a, **k: [passage])
    provider = _FakeProvider(result={
        "title": "Εργασίες για το σπίτι", "body": "word word word word",
    })
    monkeypatch.setattr(segment_mod, "get_provider", lambda: provider)

    seg3 = Block(kind="segment", title="Bonus: Pedal chains", body="", order=2,
                 parent_id=lesson.id, language="el",
                 meta={"segment_status": "queued",
                       "segment_instruction": "explain pedal chaining order"})
    db.add(seg3)
    db.commit()

    generate_segment(db, seg3)
    db.commit()

    assert seg3.title == "Εργασίες για το σπίτι"
    assert seg3.body == "word word word word"
    assert seg3.meta["segment_status"] == "done"
    assert "segment_instruction" not in seg3.meta
    assert seg3.meta["citations"] == [
        {"source_id": str(passage.source_id), "source_title": "Pedalboard Handbook", "page": 12},
    ]

    db.refresh(lesson)
    assert lesson.meta["word_count"] == 5 + 3 + 4       # seg1 (5) + seg2 (3) + seg3 (4)
    assert lesson.meta["meets_floor"] is True            # 12 >= floor_words=10

    sent = " ".join(m["content"] for m in provider.calls[0]["messages"])
    assert "L1" in sent, "the lesson's own title must reach the model"
    assert "Theory" in sent and "word word word word word" in sent, (
        "a sibling segment's title and body must reach the model, for consistency"
    )
    assert "explain pedal chaining order" in sent, "the tutor's instruction must reach the model"
    assert "Pedalboard Handbook" in sent, "the grounded passage must reach the model"


def test_generate_segment_propagates_the_providers_error_and_leaves_the_segment_untouched(
    db_and_tree, monkeypatch,
):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    monkeypatch.setattr(segment_mod, "ground_topic", lambda *a, **k: [])

    def _boom(*a, **k):
        raise LLMError("upstream", "the model is down")

    provider = _FakeProvider()
    provider.guided_json = _boom
    monkeypatch.setattr(segment_mod, "get_provider", lambda: provider)

    seg3 = Block(kind="segment", title="Bonus", body="", order=2, parent_id=lesson.id,
                 language="el", meta={"segment_status": "queued", "segment_instruction": "x"})
    db.add(seg3)
    db.commit()

    with pytest.raises(LLMError):
        generate_segment(db, seg3)

    assert seg3.body == ""
    assert seg3.meta["segment_status"] == "queued"
    assert seg3.meta["segment_instruction"] == "x"


# ---------------------------------------------------------------------------
# Task 2, job-level: run_curriculum_revise_job's per-segment generation loop +
# conditional draft chaining
# ---------------------------------------------------------------------------

def test_job_generates_queued_segments_and_does_not_chain_a_segment_only_plan(
    db_and_tree, monkeypatch,
):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    monkeypatch.setattr(segment_mod, "ground_topic", lambda *a, **k: [])
    monkeypatch.setattr(segment_mod, "get_provider", lambda: _FakeProvider())
    draft_calls: list[uuid.UUID] = []
    monkeypatch.setattr(revise_job, "run_curriculum_draft_job", lambda jid: draft_calls.append(jid))

    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Bonus",
         "instruction": "explain pedal chaining order", "reason": "r"},
    ]}
    job = GenerationJob(kind="curriculum_revise", status="pending",
                        params={"root_id": str(course.id), "instruction": "go", "plan": plan})
    db.add(job)
    db.commit()
    job_id = job.id

    run_curriculum_revise_job(job_id)

    db.expire_all()
    job = db.get(GenerationJob, job_id)
    assert job.status == "succeeded"
    assert job.progress["segments_total"] == 1
    assert job.progress["segments_done"] == 1
    assert job.progress["segments_failed"] == 0
    assert "draft_job_id" not in job.progress          # nothing was chained

    assert draft_calls == []
    draft_rows = db.scalar(
        select(func.count(GenerationJob.id)).where(GenerationJob.kind == "curriculum_draft")
    )
    assert draft_rows == 0

    new_seg = _children(db, lesson.id, "segment")[-1]
    assert new_seg.meta["segment_status"] == "done"
    assert new_seg.body == "word word word word"


def test_job_chains_the_draft_when_a_lesson_was_also_queued(db_and_tree, monkeypatch):
    """(c)'s second half: a plan with a `modify_lesson` (queuing a LESSON, not
    just a segment) still chains the ordinary draft fan-out — the conditional
    only skips the chain when NO lesson ended up queued."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    monkeypatch.setattr(segment_mod, "ground_topic", lambda *a, **k: [])
    monkeypatch.setattr(segment_mod, "get_provider", lambda: _FakeProvider())
    draft_calls: list[uuid.UUID] = []
    monkeypatch.setattr(revise_job, "run_curriculum_draft_job", lambda jid: draft_calls.append(jid))

    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Bonus",
         "instruction": "explain pedal chaining order", "reason": "r"},
        {"op": "modify_lesson", "lesson_id": str(lesson.id), "instruction": "make it punchier",
         "reason": "r"},
    ]}
    job = GenerationJob(kind="curriculum_revise", status="pending",
                        params={"root_id": str(course.id), "instruction": "go", "plan": plan})
    db.add(job)
    db.commit()
    job_id = job.id

    run_curriculum_revise_job(job_id)

    db.expire_all()
    job = db.get(GenerationJob, job_id)
    assert job.status == "succeeded"
    assert job.progress["segments_total"] == 1
    assert job.progress["segments_done"] == 1
    assert job.progress["draft_job_id"] == str(draft_calls[0])

    assert len(draft_calls) == 1
    draft_rows = db.scalar(
        select(func.count(GenerationJob.id)).where(GenerationJob.kind == "curriculum_draft")
    )
    assert draft_rows == 1


def test_job_marks_a_failing_segment_failed_and_still_generates_the_rest(db_and_tree, monkeypatch):
    """Per-segment failure isolation: one bad segment must not fail the whole
    row or stop the others (mirrors `curriculum_draft`'s per-lesson isolation)."""
    db, course, module, lesson, seg1, seg2 = db_and_tree
    monkeypatch.setattr(segment_mod, "ground_topic", lambda *a, **k: [])

    class _SelectiveProvider:
        def guided_json(self, messages, schema, *, temperature=0.2, role="draft", max_tokens=None):
            sent = " ".join(m["content"] for m in messages)
            if "make it fail" in sent:
                raise LLMError("upstream", "the model is down")
            return {"title": "Good bit", "body": "word word word word"}

    monkeypatch.setattr(segment_mod, "get_provider", lambda: _SelectiveProvider())
    monkeypatch.setattr(revise_job, "run_curriculum_draft_job", lambda jid: None)

    plan = {"summary": "s", "ops": [
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Will fail",
         "instruction": "make it fail", "reason": "r"},
        {"op": "add_segment", "lesson_id": str(lesson.id), "title": "Will succeed",
         "instruction": "make it good", "reason": "r"},
    ]}
    job = GenerationJob(kind="curriculum_revise", status="pending",
                        params={"root_id": str(course.id), "instruction": "go", "plan": plan})
    db.add(job)
    db.commit()
    job_id = job.id

    run_curriculum_revise_job(job_id)

    db.expire_all()
    job = db.get(GenerationJob, job_id)
    assert job.status == "succeeded"                    # a segment failure must not fail the row
    assert job.progress["segments_total"] == 2
    assert job.progress["segments_done"] == 1
    assert job.progress["segments_failed"] == 1

    # By ORDER, not title: a successful generation is allowed to rename the
    # segment (`SEGMENT_SCHEMA` mirrors `REFINE_SCHEMA`'s "keep unless the
    # instruction says otherwise"), so the original "Will succeed" title is not
    # a stable handle once the fake provider has returned its own.
    failed, succeeded = _children(db, lesson.id, "segment")[2:]
    assert failed.meta["segment_status"] == "failed"
    assert failed.meta["segment_error"]
    assert failed.body == ""
    assert succeeded.meta["segment_status"] == "done"
    assert succeeded.body == "word word word word"


# ---------------------------------------------------------------------------
# Task 3 (Spec B): the planner SEES the blueprint and the segment tree
#
# The 2026-07-20 "structurally impossible plan" incident: the planner could see
# modules and lessons but not the segment tree beneath them, nor the blueprint a
# lesson is actually built from, so it proposed ops the current shape could not
# hold. This gives it both, and steers it toward the surgical ops (Tasks 1-2)
# instead of `modify_lesson` for a targeted change.
# ---------------------------------------------------------------------------

def test_compact_tree_text_includes_segment_id_lines(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    txt = revise.compact_tree_text(db, course)
    assert f"    [{seg1.id}] Theory" in txt
    assert f"    [{seg2.id}] Exercises" in txt
    # still no bodies — the token discipline the module docstring is built on
    assert "word word word word word" not in txt


def test_build_revise_messages_blueprint_block_shows_enabled_and_disabled_keys(db_and_tree):
    db, course, module, lesson, seg1, seg2 = db_and_tree
    bp = default_blueprint()
    for s in bp["sections"]:
        if s["key"] == "homework":
            s["enabled"] = False
    course.meta = {**(course.meta or {}), "blueprint": bp}
    db.commit()

    msgs = revise.build_revise_messages(
        course_title=course.title, brief=None, language="el",
        tree_text=revise.compact_tree_text(db, course), instruction="add a warm-up drill",
        retrieved=None, course_meta=course.meta,
    )
    user = next(m["content"] for m in msgs if m["role"] == "user")
    assert "warm_up (Ζέσταμα)" in user                       # an ENABLED key + label
    assert "disabled: homework (Εργασία για το σπίτι)" in user  # the DISABLED key + label


def test_revise_tail_prefers_surgical_ops_and_states_coupled_blueprint_guidance():
    """(c): the rendered REVISE_TAIL (default, via the overrides resolver's
    fallback) tells the model to prefer the surgical ops over `modify_lesson`,
    and that a new RECURRING section is an `update_blueprint` + per-lesson
    `add_segment` pair in the SAME plan — never planned around silently."""
    from app.prompts.overrides import resolve

    rendered = resolve(None, revise.REVISE_SLICE_ID, revise.REVISE_TAIL)
    low = rendered.lower()
    # surgical-ops preference over modify_lesson
    assert "add_segment" in low and "modify_lesson" in low
    assert "before modify_lesson" in low or "prefer" in low
    # a new recurring section = update_blueprint + add_segment, same plan
    assert "update_blueprint" in low and "same plan" in low
    # an impossible-under-the-current-blueprint request must be said, not hidden
    assert "impossible" in low and "summary" in low


def test_plan_revision_grounds_the_tree_with_the_courses_own_blueprint(monkeypatch):
    """(d)-adjacent: `plan_revision` (not just `build_revise_messages` directly)
    threads the live course's blueprint through, so a real revise call sees the
    same enabled/disabled split `validate_ops` already enforces at apply time —
    the planner and the validator can no longer disagree about what add_segment's
    section_key may target."""
    db = SessionLocal()
    try:
        bp = default_blueprint()
        for s in bp["sections"]:
            if s["key"] == "qa_prompts":
                s["enabled"] = False
        course = Block(kind="course", title="Tone", is_template=True, language="el",
                       meta={"brief": None, "source_ids": None, "blueprint": bp})
        db.add(course)
        db.flush()
        module = Block(kind="module", title="M1", parent_id=course.id, order=0,
                       language="el", meta={"objective": "m1"})
        db.add(module)
        db.flush()
        lesson = Block(kind="lesson", title="L1", parent_id=module.id, order=0,
                       language="el", meta={"objective": "l1"})
        db.add(lesson)
        db.commit()

        captured = {}

        def _fake_provider():
            class _P:
                def guided_json(self, messages, schema, role="plan"):
                    captured["messages"] = messages
                    return {"summary": "s", "ops": []}
            return _P()

        monkeypatch.setattr(revise, "get_provider", _fake_provider)
        monkeypatch.setattr(revise, "ground_topic", lambda db, topic, *, source_ids=None, k=5: [])

        revise.plan_revision(db, course.id, instruction="add a warm-up drill")

        sent = " ".join(m["content"] for m in captured["messages"])
        assert "disabled: qa_prompts" in sent
        assert "warm_up (" in sent and "enabled:" in sent
    finally:
        db.close()
