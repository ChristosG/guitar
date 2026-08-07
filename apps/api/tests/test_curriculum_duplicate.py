"""`POST /curricula/{root_id}/duplicate` — the tutor's backup before he lets the
AI rewrite something.

The thing under test is FIDELITY. A copy that is subtly poorer than the original
— missing segment prose, missing diagrams, a blueprint that did not come along —
fails silently: it looks like a curriculum, it opens like a curriculum, and it is
only discovered to be lossy on the morning he actually needs to fall back to it.
So most of what is here compares the copy against the source rather than against
a hardcoded expectation.
"""
from fastapi.testclient import TestClient

from app.curriculum.duplicate import copy_title, duplicate_curriculum
from app.db import SessionLocal
from app.main import app
from app.models.artifact import Artifact
from app.models.block import Block
from app.models.generation_job import GenerationJob

client = TestClient(app)


def _mk_course(db, *, title="Ήχος και Ενισχυτές", language="el"):
    """A course with the shape that actually ships: course -> module -> lesson
    -> segment, a blueprint on the root, provenance and word counts on the
    leaves. Returns (course, module, lesson, segment)."""
    course = Block(
        kind="course", title=title, is_template=True, order=0, plane="content",
        language=language,
        meta={
            "blueprint": {"version": 1, "sections": [
                {"key": "warmup", "label": {"el": "Ζέσταμα", "en": "Warm-up"}, "enabled": True},
            ]},
            "brief": "Ένα μάθημα για τον ήχο της κιθάρας.",
            "source_ids": ["11111111-1111-1111-1111-111111111111"],
        },
    )
    db.add(course); db.flush()
    module = Block(kind="module", title="Ενότητα 1: Αλυσίδα σήματος", parent_id=course.id,
                   order=0, plane="content", language=language,
                   body="Στόχος της ενότητας.", meta={"tier": 1})
    db.add(module); db.flush()
    lesson = Block(kind="lesson", title="Μάθημα 1", parent_id=module.id, order=0,
                   plane="content", language=language,
                   meta={"draft_status": "ready", "word_count": 420})
    db.add(lesson); db.flush()
    segment = Block(kind="segment", title="Ζέσταμα", parent_id=lesson.id, order=0,
                    plane="content", language=language,
                    body="Ξεκίνα με ανοιχτές χορδές και άκου τον ενισχυτή.",
                    meta={"section": "warmup", "citations": [{"page": 12}]})
    db.add(segment); db.commit()
    return course, module, lesson, segment


def _tree(db, root_id):
    """(kind, title, body, order) for the whole content subtree, depth-first, so
    two trees can be compared as plain data."""
    out = []

    def walk(node_id, depth):
        node = db.get(Block, node_id)
        out.append((depth, node.kind, node.title, node.body, node.order))
        kids = db.query(Block).filter(
            Block.parent_id == node_id, Block.plane == "content"
        ).order_by(Block.order).all()
        for k in kids:
            walk(k.id, depth + 1)

    walk(root_id, 0)
    return out


# ---- what a copy contains ---------------------------------------------------

def test_duplicate_reproduces_the_whole_tree_including_prose():
    db = SessionLocal()
    try:
        course, _, _, _ = _mk_course(db)
        r = client.post(f"/curricula/{course.id}/duplicate")
        assert r.status_code == 201
        copy_id = r.json()["id"]

        db.expire_all()
        source, copy = _tree(db, course.id), _tree(db, copy_id)
        # Every node, at every depth, with its prose — only the root title
        # differs, and it differs by exactly the suffix.
        assert len(copy) == len(source) == 4
        assert copy[0][2] == f"{source[0][2]} (αντίγραφο)"
        assert copy[1:] == source[1:]
    finally:
        db.close()


def test_duplicate_carries_the_blueprint_and_the_leaf_meta():
    """The blueprint IS the curriculum's shape — a copy without it re-drafts into
    a different course. Leaf `meta` carries citations and word counts, which are
    what make the board render provenance chips rather than bare prose."""
    db = SessionLocal()
    try:
        course, _, _, segment = _mk_course(db)
        copy_id = client.post(f"/curricula/{course.id}/duplicate").json()["id"]

        db.expire_all()
        copy = db.get(Block, copy_id)
        assert copy.meta["blueprint"] == db.get(Block, course.id).meta["blueprint"]
        assert copy.meta["brief"] == db.get(Block, course.id).meta["brief"]
        assert copy.meta["source_ids"] == db.get(Block, course.id).meta["source_ids"]

        seg_copy = db.query(Block).filter(
            Block.kind == "segment",
            Block.parent_id.in_(
                db.query(Block.id).filter(Block.kind == "lesson").subquery().select()
            ),
        ).all()
        cloned = [s for s in seg_copy if s.id != segment.id and s.title == segment.title]
        assert cloned, "the segment did not come along"
        assert cloned[0].meta["citations"] == [{"page": 12}]
        assert cloned[0].meta["section"] == "warmup"
    finally:
        db.close()


def test_duplicate_stamps_where_it_came_from():
    """Nothing reads these yet. They are what lets a later 'compare with the
    original' exist without a migration, which on a .deb is the difference
    between a feature and a release-day risk."""
    db = SessionLocal()
    try:
        course, _, _, _ = _mk_course(db)
        copy_id = client.post(f"/curricula/{course.id}/duplicate").json()["id"]
        db.expire_all()
        copy = db.get(Block, copy_id)
        assert copy.meta["copied_from"] == str(course.id)
        assert copy.meta["copied_at"]
    finally:
        db.close()


def test_duplicate_stamps_every_node_as_a_template_owned_by_nobody():
    """The opposite stamp from assignment's. Get this wrong and the copy is an
    invisible orphan: `GET /curricula` selects on `is_template`, so a copy
    stamped False exists in the database and nowhere in the app."""
    db = SessionLocal()
    try:
        course, _, _, _ = _mk_course(db)
        copy_id = client.post(f"/curricula/{course.id}/duplicate").json()["id"]

        db.expire_all()
        seen = []

        def walk(nid):
            n = db.get(Block, nid)
            seen.append(n)
            for k in db.query(Block).filter(Block.parent_id == nid).all():
                walk(k.id)

        walk(copy_id)
        assert len(seen) == 4
        assert all(n.is_template is True for n in seen)
        assert all(n.student_id is None for n in seen)

        listed = [c["id"] for c in client.get("/curricula").json()]
        assert copy_id in listed
    finally:
        db.close()


def test_duplicate_brings_attached_artifacts_and_repoints_them():
    """A copy whose lessons came back without their chord diagrams is visibly
    poorer than the original, which undermines the one thing it is for."""
    db = SessionLocal()
    try:
        course, _, lesson, segment = _mk_course(db)
        db.add(Artifact(kind="chord_diagram", title="Am",
                        spec={"name": "Am", "fingers": [], "baseFret": 1},
                        tags=["chord"], source="ai", block_id=segment.id))
        db.commit()

        copy_id = client.post(f"/curricula/{course.id}/duplicate").json()["id"]
        db.expire_all()

        arts = db.query(Artifact).filter(Artifact.title == "Am").all()
        assert len(arts) == 2, "the artifact was not cloned"
        blocks = {a.block_id for a in arts}
        assert segment.id in blocks
        # The clone hangs off a DIFFERENT block, and that block is in the copy.
        other = (blocks - {segment.id}).pop()
        assert other != segment.id
        node = db.get(Block, other)
        while node.parent_id is not None:
            node = db.get(Block, node.parent_id)
        assert str(node.id) == copy_id

        # Independent JSON objects — two rows must never share one dict.
        assert arts[0].spec == arts[1].spec
        assert arts[0].spec is not arts[1].spec
    finally:
        db.close()


def test_duplicate_leaves_the_source_untouched():
    """The whole point is that the original survives whatever happens next."""
    db = SessionLocal()
    try:
        course, _, _, _ = _mk_course(db)
        before = _tree(db, course.id)
        before_meta = dict(db.get(Block, course.id).meta)

        client.post(f"/curricula/{course.id}/duplicate")
        db.expire_all()

        assert _tree(db, course.id) == before
        assert db.get(Block, course.id).meta == before_meta
        assert "copied_from" not in db.get(Block, course.id).meta
    finally:
        db.close()


# ---- draft markers ----------------------------------------------------------

def test_duplicate_settles_a_stale_drafting_marker_but_keeps_the_real_ones():
    """`drafting` means a worker is part-way through. The copy has no worker, so
    on the copy it is a lie — the board would spin forever and `draft_progress`
    would read the fork as in-flight. `queued`/`ready`/`failed` are real states
    about real content and are copied verbatim."""
    db = SessionLocal()
    try:
        course, module, lesson, _ = _mk_course(db)
        lesson.meta = {**lesson.meta, "draft_status": "drafting"}
        db.add(Block(kind="lesson", title="Μάθημα 2", parent_id=module.id, order=1,
                     plane="content", meta={"draft_status": "queued"}))
        db.add(Block(kind="lesson", title="Μάθημα 3", parent_id=module.id, order=2,
                     plane="content", meta={"draft_status": "failed", "error": "boom"}))
        db.commit()

        copy_id = client.post(f"/curricula/{course.id}/duplicate").json()["id"]
        db.expire_all()

        mod = db.query(Block).filter(Block.parent_id == copy_id).one()
        by_title = {b.title: b for b in db.query(Block).filter(Block.parent_id == mod.id).all()}
        assert by_title["Μάθημα 1"].meta["draft_status"] == "queued"   # was drafting
        assert by_title["Μάθημα 2"].meta["draft_status"] == "queued"
        assert by_title["Μάθημα 3"].meta["draft_status"] == "failed"
        assert by_title["Μάθημα 3"].meta["error"] == "boom"

        # ...and the SOURCE still says drafting, because a worker really is on it.
        assert db.get(Block, lesson.id).meta["draft_status"] == "drafting"
    finally:
        db.close()


# ---- the delivery plane -----------------------------------------------------

def test_duplicate_does_not_copy_the_delivery_plane():
    """A segmented course carries a derived pacing plan alongside its content.
    That is not curriculum content; segmenting the COPY is a separate, later
    call. Same rule `clone_content_subtree` already applies for assignment."""
    db = SessionLocal()
    try:
        course, _, _, _ = _mk_course(db)
        db.add(Block(kind="delivery_root", title="Πρόγραμμα", parent_id=course.id,
                     order=99, plane="delivery"))
        db.commit()

        copy_id = client.post(f"/curricula/{course.id}/duplicate").json()["id"]
        db.expire_all()
        kids = db.query(Block).filter(Block.parent_id == copy_id).all()
        assert [k.plane for k in kids] == ["content"]
    finally:
        db.close()


# ---- naming -----------------------------------------------------------------

def test_copy_title_speaks_the_courses_own_language():
    """The course's language, NOT the UI locale. A Greek course duplicated from
    an English session is still Greek, and its title sits in a column of Greek
    module names."""
    assert copy_title("Ήχος", "el") == "Ήχος (αντίγραφο)"
    assert copy_title("Tone", "en") == "Tone (copy)"
    assert copy_title("Ήχος", "el-GR") == "Ήχος (αντίγραφο)"
    assert copy_title("Tone", "en-US") == "Tone (copy)"
    assert copy_title("Ήχος", None) == "Ήχος (αντίγραφο)"      # el is the default


def test_copy_title_cuts_the_title_never_the_marker():
    """`Block.title` is String(300), and duplicating a duplicate of a duplicate
    is a real thing a tutor does. When something has to give it is the original
    title — the suffix is what tells them apart."""
    long = "Α" * 300
    out = copy_title(long, "el")
    assert len(out) <= 300
    assert out.endswith(" (αντίγραφο)")
    # A trailing space the tutor left would otherwise read "Ήχος  (αντίγραφο)".
    assert copy_title("Ήχος ", "el") == "Ήχος (αντίγραφο)"


def test_a_duplicate_can_itself_be_duplicated_and_still_fits():
    db = SessionLocal()
    try:
        course, _, _, _ = _mk_course(db, title="Α" * 295)
        first = client.post(f"/curricula/{course.id}/duplicate").json()["id"]
        r = client.post(f"/curricula/{first}/duplicate")
        assert r.status_code == 201
        assert len(r.json()["title"]) <= 300
    finally:
        db.close()


# ---- the doors that must stay shut, and the one that must stay open ---------

def test_duplicate_404s_on_anything_that_is_not_a_curriculum_root():
    """A module id here would fork a subtree into a top-level course. The generic
    /blocks routes handle non-roots; this door is roots only."""
    db = SessionLocal()
    try:
        course, module, lesson, _ = _mk_course(db)
        assert client.post(f"/curricula/{module.id}/duplicate").status_code == 404
        assert client.post(f"/curricula/{lesson.id}/duplicate").status_code == 404
        assert client.post(
            "/curricula/00000000-0000-0000-0000-000000000000/duplicate"
        ).status_code == 404
        # ...and the real one still works, so the guard is not just refusing.
        assert client.post(f"/curricula/{course.id}/duplicate").status_code == 201
    finally:
        db.close()


def test_duplicate_is_allowed_while_a_job_is_writing_the_source():
    """THE DIFFERENCE FROM DELETE. Delete 409s here because deleting the tree out
    from under a draft worker strands it mid-write. Duplicate only READS, so
    there is nothing to strand — and this is exactly the moment a backup is worth
    most, so refusing would be backwards."""
    db = SessionLocal()
    try:
        course, _, _, _ = _mk_course(db)
        db.add(GenerationJob(kind="curriculum_draft", status="running",
                             params={"root_id": str(course.id)}))
        db.commit()

        assert client.delete(f"/curricula/{course.id}").status_code == 409
        assert client.post(f"/curricula/{course.id}/duplicate").status_code == 201
    finally:
        db.close()


def test_duplicate_service_refuses_a_non_root_without_the_router():
    """The guard lives in the service too, not only in the route — the chat
    copilot and any future caller reach the service directly."""
    import pytest

    from app.curriculum.duplicate import DuplicateError

    db = SessionLocal()
    try:
        _, module, _, _ = _mk_course(db)
        with pytest.raises(DuplicateError):
            duplicate_curriculum(db, module.id)
    finally:
        db.rollback()
        db.close()
