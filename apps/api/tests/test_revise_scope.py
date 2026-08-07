"""A revision confined to ONE module.

WHY THIS EXISTS. Chris asked: "on the 'Guitar Tone latest version' I want to
make that module instead of 3 lessons to have 5. how can I do that?" The op
vocabulary could already express it — `insert_lesson` has taken a `module_id`
since the beginning — but nothing told the planner WHICH module he meant, and
`plan_revision` fed it the entire course: 24 lessons of context to edit 3 of
them.

WHAT SCOPING ACTUALLY BUYS, and it is not only a shorter prompt. Unscoped, an
op that edits a lesson in a different module is merely unlikely. Scoped, it is
structurally impossible: `_tree_ids` narrows the id maps, and every per-op check
in `validate_ops` is already a membership test against those maps. The two
families that slip past that — course-level ops with no id to narrow, and
segment ops that resolve against the course — are refused explicitly. All three
routes are tested here, because "the model probably won't" is not a guarantee.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.curriculum.blueprint import default_blueprint
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
from app.models.generation_job import GenerationJob
import app.curriculum.revise as revise

client = TestClient(app)

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


@pytest.fixture
def two_modules():
    """course -> M1 (2 lessons, first with a segment) + M2 (1 lesson).

    Two modules is the whole point: with one, every scoping bug passes.
    """
    db = SessionLocal()
    course = Block(kind="course", title="Ήχος", is_template=True, language="el",
                   meta={"brief": None, "source_ids": None,
                         "gap_policy": "general_knowledge", "blueprint": default_blueprint()})
    db.add(course); db.flush()

    m1 = Block(kind="module", title="Από το πετάλι στον ενισχυτή", parent_id=course.id,
               order=0, language="el", meta={"objective": "αλυσίδα σήματος"})
    m2 = Block(kind="module", title="Χορδές", parent_id=course.id, order=1,
               language="el", meta={"objective": "υλικά"})
    db.add_all([m1, m2]); db.flush()

    l1 = Block(kind="lesson", title="Πετάλια", parent_id=m1.id, order=0, language="el",
               meta={"objective": "l1"})
    l2 = Block(kind="lesson", title="Ενισχυτές", parent_id=m1.id, order=1, language="el",
               meta={"objective": "l2"})
    l3 = Block(kind="lesson", title="Υλικά χορδών", parent_id=m2.id, order=0, language="el",
               meta={"objective": "l3"})
    db.add_all([l1, l2, l3]); db.flush()

    s1 = Block(kind="segment", title="Ζέσταμα", body="κείμενο", order=0,
               parent_id=l1.id, language="el", meta={"section": "theory"})
    s3 = Block(kind="segment", title="Άλλο", body="κείμενο", order=0,
               parent_id=l3.id, language="el", meta={"section": "theory"})
    db.add_all([s1, s3]); db.commit()

    yield db, course, m1, m2, l1, l2, l3, s1, s3
    db.close()


# ---- the tree the model sees ----------------------------------------------

def test_the_scoped_tree_shows_one_module_and_hides_the_other(two_modules):
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules

    whole = revise.compact_tree_text(db, course)
    assert "Χορδές" in whole and "Υλικά χορδών" in whole

    scoped = revise.compact_tree_text(db, course, scope_module_id=m1.id)
    assert "Από το πετάλι στον ενισχυτή" in scoped
    assert "Πετάλια" in scoped and "Ενισχυτές" in scoped
    assert "Χορδές" not in scoped
    assert "Υλικά χορδών" not in scoped


def test_the_scoped_tree_keeps_the_same_line_format(two_modules):
    """The model must not have to learn a second shape depending on how it was
    called — only the number of lines changes."""
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    scoped = revise.compact_tree_text(db, course, scope_module_id=m1.id)
    assert f"M1 [{m1.id}]" in scoped
    assert f"  L1 [{l1.id}]" in scoped


# ---- what a scoped plan may and may not do ---------------------------------

def _plan(ops):
    return {"summary": "s", "ops": ops}


def test_scope_keeps_an_insert_lesson_aimed_at_the_scoped_module(two_modules):
    """The ask that started all this: 3 lessons -> 5."""
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    out = revise.validate_ops(db, course.id, _plan([
        {"op": "insert_lesson", "module_id": str(m1.id), "title": "Νέο μάθημα",
         "objective": "στόχος", "reason": "r"},
    ]), scope_module_id=m1.id)
    assert [o["op"] for o in out["ops"]] == ["insert_lesson"]
    assert out["dropped"] == []


def test_scope_drops_an_insert_lesson_aimed_at_another_module(two_modules):
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    out = revise.validate_ops(db, course.id, _plan([
        {"op": "insert_lesson", "module_id": str(m2.id), "title": "Λάθος",
         "objective": "στόχος", "reason": "r"},
    ]), scope_module_id=m1.id)
    assert out["ops"] == []
    assert len(out["dropped"]) == 1


def test_scope_drops_a_modify_lesson_in_another_module(two_modules):
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    ops = [{"op": "modify_lesson", "lesson_id": str(l3.id), "instruction": "x", "reason": "r"}]

    unscoped = revise.validate_ops(db, course.id, _plan(list(ops)))
    assert [o["op"] for o in unscoped["ops"]] == ["modify_lesson"]

    scoped = revise.validate_ops(db, course.id, _plan(list(ops)), scope_module_id=m1.id)
    assert scoped["ops"] == []


def test_scope_refuses_course_level_ops_outright(two_modules):
    """`insert_module` would create a SIBLING of the module being revised;
    blueprint ops reshape every lesson in the course. Neither belongs in a
    revision the tutor scoped to one module, and neither has an id to narrow —
    so they are refused by name."""
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    out = revise.validate_ops(db, course.id, _plan([
        {"op": "insert_module", "title": "Νέα ενότητα", "objective": "o",
         "tier": "library", "lessons": [{"title": "a", "objective": "b"}], "reason": "r"},
        {"op": "set_section_enabled", "section_key": "theory", "enabled": False, "reason": "r"},
    ]), scope_module_id=m1.id)

    assert out["ops"] == []
    assert len(out["dropped"]) == 2
    assert all("scoped to one module" in d["reason"] for d in out["dropped"])


def test_scope_drops_a_segment_op_from_another_module(two_modules):
    """Segment ops resolve against the COURSE, which is right unscoped and too
    loose here — a segment in another module passes that check."""
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    ops = [{"op": "edit_segment", "segment_id": str(s3.id), "instruction": "x", "reason": "r"}]

    unscoped = revise.validate_ops(db, course.id, _plan(list(ops)))
    assert [o["op"] for o in unscoped["ops"]] == ["edit_segment"]

    scoped = revise.validate_ops(db, course.id, _plan(list(ops)), scope_module_id=m1.id)
    assert scoped["ops"] == []
    assert "outside the module" in scoped["dropped"][0]["reason"]


def test_scope_keeps_a_segment_op_inside_the_scoped_module(two_modules):
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    out = revise.validate_ops(db, course.id, _plan([
        {"op": "edit_segment", "segment_id": str(s1.id), "instruction": "x", "reason": "r"},
    ]), scope_module_id=m1.id)
    assert [o["op"] for o in out["ops"]] == ["edit_segment"]


def test_unscoped_behaviour_is_byte_for_byte_what_it_was(two_modules):
    """The regression guard. Every existing caller passes no scope, and must be
    completely unaffected."""
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    ops = [
        {"op": "insert_lesson", "module_id": str(m2.id), "title": "Ν", "objective": "ο", "reason": "r"},
        {"op": "modify_lesson", "lesson_id": str(l3.id), "instruction": "x", "reason": "r"},
        {"op": "edit_segment", "segment_id": str(s3.id), "instruction": "x", "reason": "r"},
    ]
    out = revise.validate_ops(db, course.id, _plan(ops))
    assert [o["op"] for o in out["ops"]] == ["insert_lesson", "modify_lesson", "edit_segment"]
    assert out["dropped"] == []


# ---- the prompt ------------------------------------------------------------

def test_a_scoped_prompt_names_the_module_and_forbids_reaching_out(two_modules):
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    msgs = revise.build_revise_messages(
        course_title="Ήχος", brief=None, language="el",
        tree_text="tree", instruction="πέντε μαθήματα", scope_title=m1.title,
    )
    user = msgs[1]["content"]
    assert "Από το πετάλι στον ενισχυτή" in user
    assert "SCOPE" in user


def test_an_unscoped_prompt_gains_no_scope_paragraph(two_modules):
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    msgs = revise.build_revise_messages(
        course_title="Ήχος", brief=None, language="el",
        tree_text="tree", instruction="κάτι",
    )
    assert "SCOPE — READ THIS" not in msgs[1]["content"]


def test_plan_revision_refuses_a_module_that_is_not_under_this_course(two_modules):
    """A caller mistake must fail loudly. Narrowing on a foreign id would empty
    the maps and drop every op with a confusing reason instead."""
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    with pytest.raises(revise.ReviseError):
        revise.plan_revision(db, course.id, instruction="x", scope_module_id=l1.id)
    with pytest.raises(revise.ReviseError):
        revise.plan_revision(db, course.id, instruction="x", scope_module_id=uuid.uuid4())


# ---- the route -------------------------------------------------------------

def test_the_route_stores_the_scope_on_the_job(two_modules):
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    r = client.post(f"/curricula/{course.id}/revise",
                    json={"instruction": "πέντε μαθήματα", "scope_module_id": str(m1.id)})
    assert r.status_code == 202
    job = db.get(GenerationJob, uuid.UUID(r.json()["job_id"]))
    assert job.params["scope_module_id"] == str(m1.id)


def test_the_route_422s_on_a_scope_that_is_not_a_module_of_this_course(two_modules):
    """A 422 the tutor sees now, rather than a job that starts, spends a planner
    call and fails minutes later."""
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    assert client.post(f"/curricula/{course.id}/revise",
                       json={"instruction": "x", "scope_module_id": str(l1.id)}).status_code == 422
    assert client.post(f"/curricula/{course.id}/revise",
                       json={"instruction": "x", "scope_module_id": str(uuid.uuid4())}).status_code == 422


def test_an_unscoped_revise_stores_no_scope(two_modules):
    db, course, m1, m2, l1, l2, l3, s1, s3 = two_modules
    r = client.post(f"/curricula/{course.id}/revise", json={"instruction": "κάτι"})
    assert r.status_code == 202
    job = db.get(GenerationJob, uuid.UUID(r.json()["job_id"]))
    assert "scope_module_id" not in job.params
