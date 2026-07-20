"""Editing the outline, Extend-with-chat, and the board's serialization
(Plan 13, Stages 6.3, 6.9, 6.10).

Three things the tutor could not do, and one he could not see:

  * ADD / DELETE / REORDER a module or a lesson before the expensive draft runs.
    "This is the engagement the tutor said is missing" — he was handed a curriculum
    and asked to accept or reject it; what he wanted was to work on it.
  * EXTEND WITH CHAT. Chris: "a button to Extend with chat where the user writes
    e.g. change this and give more detail about the Amp — and it actually follows
    his instruction."
  * SEE THE PROVENANCE. `block_to_tree` serialized nine fields and `meta` was not
    one of them — so the citation chips, tiers and gap badges were written to the
    database by the generator and then thrown away at the API boundary.
"""
import uuid

import pytest
from sqlalchemy import select

import app.curriculum.corpus as corpus_mod
import app.curriculum.refine as refine_mod
from app.curriculum.corpus import build_library_context
from app.curriculum.depth import floor_words, target_words
from app.curriculum.edit import (
    EditError,
    add_lesson,
    add_module,
    delete_block,
    move_block,
    reorder_block,
    requeue_lesson,
)
from app.curriculum.outline import materialize_outline
from app.curriculum.refine import refine_block, undo_refine
from app.curriculum.shape import plan_shape
from app.jobs.curriculum_draft import _claim, _lesson_size
from app.models.artifact import Artifact
from app.models.block import Block
from app.models.generation_job import GenerationJob

SHAPE = plan_shape(8, 1, 50)


class _FakeProvider:
    def __init__(self, result=None):
        self.result = result or {"title": "Tone", "body": "REWRITTEN with more about the Amp."}
        self.calls: list[dict] = []

    def guided_json(self, messages, schema, *, temperature=0.2, role="spec", max_tokens=None):
        self.calls.append({"messages": messages, "role": role})
        return self.result

    def count_tokens(self, text: str) -> int:
        return len(text) // 3 + 1


@pytest.fixture(autouse=True)
def _tokens(monkeypatch):
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: _FakeProvider())


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
    return materialize_outline(
        db, _outline(), title="Tone Fundamentals", language="en", shape=SHAPE,
        library=build_library_context(db, []),
    )


def _modules(db, root_id) -> list[Block]:
    return db.scalars(
        select(Block).where(Block.parent_id == root_id, Block.kind == "module")
        .order_by(Block.order)
    ).all()


def _orders(db, parent_id) -> list[int]:
    return [
        b.order for b in db.scalars(
            select(Block).where(Block.parent_id == parent_id).order_by(Block.order)
        ).all()
    ]


def _children_lessons(db, module_id) -> list[Block]:
    return db.scalars(
        select(Block).where(Block.parent_id == module_id, Block.kind == "lesson")
        .order_by(Block.order)
    ).all()


# ---------------------------------------------------------------------------
# Add / delete / reorder, with sibling order RENORMALISED
# ---------------------------------------------------------------------------

def test_a_new_module_is_appended_and_the_orders_stay_contiguous(db):
    root_id = _course(db)

    module = add_module(db, root_id, title="Amps", objective="tubes")

    assert module.order == 2
    assert module.meta["tier"] == "general_knowledge"
    assert module.meta["added_by_tutor"] is True
    assert _orders(db, root_id) == [0, 1, 2]


def test_a_module_can_be_inserted_in_the_middle(db):
    root_id = _course(db)
    first = _modules(db, root_id)[0]

    add_module(db, root_id, title="Inserted", after=first.id)

    titles = [m.title for m in _modules(db, root_id)]
    assert titles == ["Module 0", "Inserted", "Module 1"]
    assert _orders(db, root_id) == [0, 1, 2]


def test_deleting_a_module_CLOSES_THE_HOLE_it_leaves_in_order(db):
    """THE bug that only becomes visible once you can also INSERT. `DELETE
    /blocks/{id}` always left [0, 1, 3] — which sorts fine, so nobody noticed. Then
    a new module appended at `len(siblings)` = 3 collides with the survivor already
    sitting at 3, the tie is broken by whatever Postgres feels like, and the tutor's
    new module lands in the middle of his course.
    """
    root_id = _course(db)
    add_module(db, root_id, title="Third")
    modules = _modules(db, root_id)
    assert _orders(db, root_id) == [0, 1, 2]

    delete_block(db, modules[1].id)

    assert _orders(db, root_id) == [0, 1], "no hole"

    # ...and the very next insert must not collide with anything.
    add_module(db, root_id, title="Fourth")
    orders = _orders(db, root_id)
    assert orders == [0, 1, 2]
    assert len(set(orders)) == len(orders), "no duplicate order values"
    assert [m.title for m in _modules(db, root_id)] == ["Module 0", "Third", "Fourth"]


def test_a_new_lesson_is_QUEUED_so_the_next_resume_drafts_it(db):
    """`draft_status="queued"` is not bookkeeping — it IS the enqueue. He reads
    module 1, sees a lesson missing, adds it, presses Resume."""
    root_id = _course(db)
    module = _modules(db, root_id)[0]

    lesson = add_lesson(db, module.id, title="Barre chords", objective="finally")

    assert lesson.meta["draft_status"] == "queued"
    assert lesson.est_minutes == 50, "it inherits the course's session length"
    assert lesson.order == 4


def test_reordering_moves_a_module_one_position_and_renormalises(db):
    root_id = _course(db)
    modules = _modules(db, root_id)

    reorder_block(db, modules[1].id, direction="up")

    assert [m.title for m in _modules(db, root_id)] == ["Module 1", "Module 0"]
    assert _orders(db, root_id) == [0, 1]


def test_reordering_past_the_end_is_a_no_op_not_an_error(db):
    """The Up button on the first row is a button the tutor WILL press. It does
    nothing; it does not 500."""
    root_id = _course(db)
    first = _modules(db, root_id)[0]

    reorder_block(db, first.id, direction="up")

    assert [m.title for m in _modules(db, root_id)] == ["Module 0", "Module 1"]


def test_a_bad_direction_is_rejected(db):
    root_id = _course(db)
    with pytest.raises(EditError):
        reorder_block(db, _modules(db, root_id)[0].id, direction="sideways")


def test_adding_a_lesson_to_something_that_is_not_a_module_is_rejected(db):
    root_id = _course(db)
    with pytest.raises(EditError):
        add_lesson(db, root_id, title="nope")   # root is a course, not a module


# ---------------------------------------------------------------------------
# move_block — the cross-parent move edit.py never had (renormalises BOTH parents)
# ---------------------------------------------------------------------------

def test_move_block_re_homes_a_lesson_and_renormalises_both_parents(db):
    root_id = _course(db)
    m1, m2 = _modules(db, root_id)
    l0, l1, l2, l3 = _children_lessons(db, m1.id)   # the outline seeds 4 lessons/module

    move_block(db, l1.id, m2.id)                     # append L0.1 into m2

    m1_lessons = _children_lessons(db, m1.id)
    assert [x.title for x in m1_lessons] == ["L0.0", "L0.2", "L0.3"]
    assert [x.order for x in m1_lessons] == [0, 1, 2]        # old parent's hole closed
    m2_lessons = _children_lessons(db, m2.id)
    assert m2_lessons[-1].title == "L0.1"
    assert [x.order for x in m2_lessons] == [0, 1, 2, 3, 4]  # new parent contiguous
    assert m2_lessons[-1].parent_id == m2.id


def test_move_block_after_positions_within_the_destination(db):
    root_id = _course(db)
    m1, m2 = _modules(db, root_id)
    moving = _children_lessons(db, m1.id)[0]
    dest_first = _children_lessons(db, m2.id)[0]

    move_block(db, moving.id, m2.id, after=dest_first.id)

    titles = [x.title for x in _children_lessons(db, m2.id)]
    assert titles[1] == moving.title            # landed right after dest_first


def test_move_block_rejects_a_lesson_from_another_course(db):
    root_a = _course(db)
    root_b = _course(db)
    lesson_a = _children_lessons(db, _modules(db, root_a)[0].id)[0]
    dest_module_b = _modules(db, root_b)[0]
    with pytest.raises(EditError):
        move_block(db, lesson_a.id, dest_module_b.id)


def test_move_block_rejects_an_after_in_the_wrong_module(db):
    root_id = _course(db)
    m1, m2 = _modules(db, root_id)
    moving = _children_lessons(db, m1.id)[0]
    wrong_after = _children_lessons(db, m1.id)[1]   # a lesson NOT under the destination m2
    with pytest.raises(EditError):
        move_block(db, moving.id, m2.id, after=wrong_after.id)


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------

def test_the_outline_crud_routes(db, client):
    root_id = _course(db)

    r = client.post(f"/curricula/{root_id}/modules", json={"title": "Amps", "objective": "tubes"})
    assert r.status_code == 201, r.text
    module_id = r.json()["id"]

    r = client.post(f"/blocks/{module_id}/lessons", json={"title": "Tube bias"})
    assert r.status_code == 201, r.text
    assert r.json()["meta"]["draft_status"] == "queued"

    r = client.post(f"/blocks/{module_id}/reorder", json={"direction": "up"})
    assert r.status_code == 200, r.text

    r = client.delete(f"/blocks/{module_id}")
    assert r.status_code == 204

    db.expire_all()
    assert _orders(db, root_id) == [0, 1]


def test_adding_a_module_to_an_unknown_curriculum_404s(client):
    r = client.post(f"/curricula/{uuid.uuid4()}/modules", json={"title": "X"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# EXTEND WITH CHAT
# ---------------------------------------------------------------------------

def test_refine_follows_the_tutors_instruction_and_stashes_an_undo(db, monkeypatch):
    provider = _FakeProvider()
    monkeypatch.setattr(refine_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(refine_mod, "search", lambda *a, **k: [], raising=False)

    block = Block(kind="segment", title="Tone", body="The amp is loud.", language="en",
                  meta={"section": "theory", "citations": [
                      {"source_id": "abc", "source_title": "Book", "page": 19},
                  ]})
    db.add(block)
    db.commit()

    refine_block(db, block, "give more detail about the Amp")
    db.commit()

    assert block.body == "REWRITTEN with more about the Amp."
    assert block.meta["prev_body"] == "The amp is loud."
    assert block.meta["refined"] is True

    sent = " ".join(m["content"] for m in provider.calls[0]["messages"])
    assert "give more detail about the Amp" in sent
    assert "The amp is loud." in sent, "the block's existing content must reach the model"
    assert "Book p.19" in sent, "so must its provenance — a refine that forgets where the block came from turns a cited paragraph into an uncited one that still has the chip"


def test_undo_puts_it_back(db, monkeypatch):
    monkeypatch.setattr(refine_mod, "get_provider", lambda: _FakeProvider())
    monkeypatch.setattr(refine_mod, "search", lambda *a, **k: [], raising=False)

    block = Block(kind="segment", title="Tone", body="ORIGINAL", language="en", meta={})
    db.add(block)
    db.commit()

    refine_block(db, block, "change it")
    db.commit()
    assert block.body != "ORIGINAL"

    assert undo_refine(block) is True
    db.commit()
    db.expire_all()

    block = db.get(Block, block.id)
    assert block.body == "ORIGINAL"
    assert "prev_body" not in block.meta


def test_undo_with_nothing_stashed_is_refused_rather_than_clearing_the_body(db):
    block = Block(kind="segment", title="Tone", body="ORIGINAL", language="en", meta={})
    db.add(block)
    db.commit()

    assert undo_refine(block) is False
    assert block.body == "ORIGINAL"


def test_a_dead_retriever_does_not_take_the_extend_button_down_with_it(db, monkeypatch):
    """Retrieval is an IMPROVEMENT here, not a precondition — the block's own text
    and provenance are already in the prompt."""
    monkeypatch.setattr(refine_mod, "get_provider", lambda: _FakeProvider())

    def _boom(*a, **k):
        raise RuntimeError("embedder is down")

    monkeypatch.setattr(refine_mod, "search", _boom, raising=False)

    block = Block(kind="segment", title="Tone", body="ORIGINAL", language="en", meta={})
    db.add(block)
    db.commit()

    refine_block(db, block, "change it")   # must not raise

    assert block.body == "REWRITTEN with more about the Amp."


def test_the_refine_and_undo_routes(db, client, monkeypatch):
    import app.routers.curriculum as router_mod  # noqa: F401

    monkeypatch.setattr(refine_mod, "get_provider", lambda: _FakeProvider())
    monkeypatch.setattr(refine_mod, "search", lambda *a, **k: [], raising=False)

    block = Block(kind="segment", title="Tone", body="ORIGINAL", language="en", meta={})
    db.add(block)
    db.commit()
    block_id = block.id

    r = client.post(f"/blocks/{block_id}/refine",
                    json={"instruction": "more about the Amp"})
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "REWRITTEN with more about the Amp."

    r = client.post(f"/blocks/{block_id}/undo")
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "ORIGINAL"

    r = client.post(f"/blocks/{block_id}/undo")
    assert r.status_code == 422, "nothing left to undo"


def test_an_empty_refine_instruction_is_a_422_not_a_call(client, db):
    block = Block(kind="segment", title="Tone", body="ORIGINAL", language="en", meta={})
    db.add(block)
    db.commit()

    r = client.post(f"/blocks/{block.id}/refine", json={"instruction": ""})

    assert r.status_code == 422


# ---------------------------------------------------------------------------
# THE BOARD: meta is serialized, and artifacts arrive in ONE query
# ---------------------------------------------------------------------------

def test_the_tree_serializes_meta_so_the_provenance_chips_can_actually_render(db, client):
    """`block_to_tree` used to serialize nine fields and `meta` was not one of them.
    The generator wrote the tiers, the coverage notes and the citations, and the API
    threw them away — the board could not have rendered them if it wanted to,
    because they were not in the response."""
    root_id = _course(db)

    r = client.get(f"/curricula/{root_id}")

    assert r.status_code == 200, r.text
    tree = r.json()
    assert tree["meta"]["shape"]["lessons_total"] == 8
    module = tree["children"][0]
    assert module["meta"]["tier"] == "library"
    assert module["meta"]["coverage_note"] == "p.19"
    lesson = module["children"][0]
    assert lesson["meta"]["draft_status"] == "queued"


def test_the_whole_trees_artifacts_arrive_embedded_in_ONE_query(db, client):
    """Every segment leaf used to fire its own `GET /artifacts?block_id=` — ~120 in
    parallel on a single board render. That stampede IS the "Could not load attached
    artifacts" error: not a bug in the artifacts endpoint, just too many of it at
    once."""
    root_id = _course(db)
    lesson = _modules(db, root_id)[0].children[0]
    db.add(Artifact(kind="chord_diagram", title="Am", spec={"frets": []},
                    block_id=lesson.id))
    db.commit()

    r = client.get(f"/curricula/{root_id}")

    assert r.status_code == 200, r.text
    tree = r.json()
    embedded = tree["children"][0]["children"][0]["artifacts"]
    assert len(embedded) == 1
    assert embedded[0]["title"] == "Am"
    assert embedded[0]["kind"] == "chord_diagram"
    # And a block with none gets an empty list, not a null the client has to guard.
    assert tree["artifacts"] == []


def test_a_block_with_no_artifacts_still_serializes_cleanly(db, client):
    root_id = _course(db)
    r = client.get(f"/blocks/{root_id}")
    assert r.status_code == 200, r.text
    assert r.json()["artifacts"] == []


# ---------------------------------------------------------------------------
# THE EDITED OUTLINE IS THE ONE THAT GETS BUILT
#
# The outline editor lets the tutor change a lesson's minutes. If materialization
# overwrote that with the course's shape (it did), the number he typed would be
# displayed back at him in the editor and then thrown away at confirm — the exact
# class of bug `meta`-not-being-serialized was.
# ---------------------------------------------------------------------------

def test_an_edited_est_minutes_survives_materialization(db):
    outline = _outline()
    outline["modules"][0]["lessons"][0]["est_minutes"] = 90

    root_id = materialize_outline(
        db, outline, title="Tone Fundamentals", language="en", shape=SHAPE,
        library=build_library_context(db, []),
    )

    lessons = _modules(db, root_id)[0].children
    assert lessons[0].est_minutes == 90
    assert lessons[1].est_minutes == SHAPE.minutes_per_lesson  # untouched ones follow the shape


@pytest.mark.parametrize("bad", [0, -5, True, None, "long"])
def test_a_nonsense_est_minutes_falls_back_to_the_shape(db, bad):
    """`True` is the one that matters: `bool` is an `int` in Python, so a truthy
    JSON value would otherwise materialize a 1-minute lesson."""
    outline = _outline()
    outline["modules"][0]["lessons"][0]["est_minutes"] = bad

    root_id = materialize_outline(
        db, outline, title="T", language="en", shape=SHAPE,
        library=build_library_context(db, []),
    )
    assert _modules(db, root_id)[0].children[0].est_minutes == SHAPE.minutes_per_lesson


# ---------------------------------------------------------------------------
# DEEPEN — "this one is thin. Write it again, longer."
# ---------------------------------------------------------------------------

def test_deepen_requeues_the_lesson_and_schedules_the_ordinary_draft_job(db, client, monkeypatch):
    """No second pipeline: the lesson goes back to `queued` and the SAME fan-out
    picks it up, against the same cached library prefix."""
    import app.routers.curriculum as router_mod

    scheduled = []
    monkeypatch.setattr(router_mod, "run_curriculum_draft_job", scheduled.append)

    root_id = _course(db)
    lesson = _modules(db, root_id)[0].children[0]
    lesson.meta = {**(lesson.meta or {}), "draft_status": "ready", "word_count": 900}
    db.commit()

    r = client.post(f"/blocks/{lesson.id}/deepen")

    assert r.status_code == 202, r.text
    assert scheduled == [uuid.UUID(r.json()["job_id"])]

    db.expire_all()
    meta = db.get(Block, lesson.id).meta
    assert meta["draft_status"] == "queued"
    assert meta["deepen"] is True
    assert db.get(GenerationJob, uuid.UUID(r.json()["job_id"])).params["root_id"] == str(root_id)


def test_deepening_something_that_is_not_a_lesson_404s(db, client):
    root_id = _course(db)
    r = client.post(f"/blocks/{root_id}/deepen")
    assert r.status_code == 404


def test_the_deepen_flag_is_CONSUMED_at_claim_and_raises_that_lessons_target(db):
    """A flag left on the row would make every future Resume quietly redraft this
    lesson long, forever, for a button he pressed once."""
    root_id = _course(db)
    lesson = _modules(db, root_id)[0].children[0]
    requeue_lesson(db, lesson.id, deepen=True)

    plan = {
        "minutes_per_lesson": SHAPE.minutes_per_lesson,
        "teaching_minutes": SHAPE.teaching_minutes,
        "target_words": SHAPE.target_words_per_lesson,
        "floor_words": SHAPE.floor_words_per_lesson,
    }
    claimed = _claim(db, lesson.id)
    assert claimed is not None
    claimed_lesson, deepen, revise_instruction = claimed
    assert deepen is True
    assert revise_instruction is None

    size = _lesson_size(claimed_lesson, plan, deepen=deepen)
    assert size["target_words"] > SHAPE.target_words_per_lesson
    assert size["floor_words"] > SHAPE.floor_words_per_lesson

    db.expire_all()
    meta = db.get(Block, lesson.id).meta
    assert meta["draft_status"] == "drafting"
    assert "deepen" not in meta


def test_a_90_minute_lesson_is_drafted_to_90_minutes_worth_of_words(db):
    """The tutor gave this lesson a longer slot in the editor. Drafting it to the
    course's 40-minute word target would hand him half a lesson."""
    root_id = _course(db)
    lesson = _modules(db, root_id)[0].children[0]
    lesson.est_minutes = 90
    db.commit()

    plan = {
        "minutes_per_lesson": SHAPE.minutes_per_lesson,
        "teaching_minutes": SHAPE.teaching_minutes,
        "target_words": SHAPE.target_words_per_lesson,
        "floor_words": SHAPE.floor_words_per_lesson,
    }
    size = _lesson_size(lesson, plan, deepen=False)

    assert size["minutes"] == 90
    assert size["teaching_minutes"] == 90  # 50 means 50: no Q&A carve-out
    assert size["target_words"] == target_words(90)
    assert size["floor_words"] == floor_words(90)
