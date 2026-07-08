"""Integration tests for the `/curricula` and `/blocks` HTTP routes: tree
retrieval, block CRUD, segmentation, and per-student deep-clone assignment.

Builds template Block trees directly via ORM rows (course -> 2 modules -> 2
lessons each) rather than driving a live `generate_curriculum` call for the
fast tests — mirrors `test_segment.py`'s stated preference (guided_json
generation is 49-179s/call, see Plan 3 Task 2's report) and is not marked
`@pytest.mark.integration` for the same reason (`test_segment.py`: "that
marker means 'hits live vLLM' per pyproject.toml — this module only hits the
DB"). Exactly one test in this module (`test_generate_curriculum_endpoint_
returns_real_tree`) drives `POST /curricula/generate` against the real
LLM+embed stack and IS marked `@pytest.mark.integration`.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.brain.ingest import IngestPayload, ingest_source
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
from app.models.curriculum import Assignment
from app.models.knowledge import KnowledgeSource

# Skip cleanly (not error) when no DB is reachable — mirrors test_curriculum_generate.py.
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


client = TestClient(app)

_TONE_TEXT = (
    "Guitar tone starts at the pickups: single-coil pickups sound bright and "
    "articulate, while humbucker pickups sound thicker and quieter, with less "
    "hum. From the guitar the signal usually hits a pedalboard: an overdrive "
    "or distortion pedal adds gain and harmonic saturation, a compressor "
    "evens out picking dynamics, and a delay or reverb pedal adds space. The "
    "amplifier is the last and biggest tone-shaping stage: a tube amp driven "
    "into natural power-tube breakup sounds warmer and more dynamic than a "
    "clean solid-state amp with gain added only by a pedal."
)
_TONE_KEYWORDS = ("amp", "pedal", "pickup", "gain", "tube")


def _children(db, parent_id) -> list[Block]:
    return db.scalars(
        select(Block).where(Block.parent_id == parent_id).order_by(Block.order)
    ).all()


def _build_template_course(db) -> Block:
    """course -> 2 modules -> 2 lessons each. Modules and lessons are
    deliberately INSERTED out of `order` sequence (module order=1 row
    written first, order=0 second; same trick per lesson) so tree-shape
    assertions genuinely exercise `block_to_tree`'s children sort rather
    than coincidentally matching insertion order — `Block.children` (the
    ORM relationship) has no configured `order_by`, empirically verified to
    return children in insertion/PK order, not `.order` order.
    """
    course = Block(
        kind="course", title="Rhythm Fundamentals", order=0, language="en",
        is_template=True, target_profile={"level": "beginner"},
    )
    db.add(course)
    db.flush()

    modules_spec = [
        (1, "Module 2", [(1, "M2 Lesson 2", 45), (0, "M2 Lesson 1", 50)]),
        (0, "Module 1", [(1, "M1 Lesson 2", 35), (0, "M1 Lesson 1", 40)]),
    ]
    for m_order, m_title, lessons in modules_spec:
        module = Block(
            kind="module", title=m_title, order=m_order, parent_id=course.id, language="en",
        )
        db.add(module)
        db.flush()
        for l_order, l_title, minutes in lessons:
            db.add(Block(
                kind="lesson", title=l_title, order=l_order,
                parent_id=module.id, language="en", est_minutes=minutes,
            ))
    db.commit()
    return course


def _create_student(name: str = "Curriculum Test Student") -> dict:
    r = client.post("/students", json={"name": name, "preferred_language": "en"})
    assert r.status_code == 200, r.text
    return r.json()


# --- GET /curricula/{root_id} + GET /blocks/{id} ---------------------------

def test_get_curriculum_returns_nested_tree_sorted_by_order():
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        course_id = course.id
    finally:
        db.close()

    r = client.get(f"/curricula/{course_id}")
    assert r.status_code == 200, r.text
    tree = r.json()

    assert tree["id"] == str(course_id)
    assert tree["kind"] == "course"
    assert tree["title"] == "Rhythm Fundamentals"
    assert tree["plane"] == "content"
    assert tree["student_id"] is None
    assert [m["title"] for m in tree["children"]] == ["Module 1", "Module 2"]

    module1 = tree["children"][0]
    assert [l["title"] for l in module1["children"]] == ["M1 Lesson 1", "M1 Lesson 2"]
    assert all(l["kind"] == "lesson" and l["children"] == [] for l in module1["children"])
    assert module1["children"][0]["est_minutes"] == 40


def test_get_curriculum_404_for_unknown_id():
    r = client.get("/curricula/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_get_block_returns_subtree_for_a_non_root_block():
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        module1 = _children(db, course.id)[0]
        module1_id = module1.id
    finally:
        db.close()

    r = client.get(f"/blocks/{module1_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(module1_id)
    assert body["kind"] == "module"
    assert body["title"] == "Module 1"
    assert len(body["children"]) == 2


def test_get_block_404_for_unknown_id():
    r = client.get("/blocks/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


# --- PATCH /blocks/{id} -----------------------------------------------------

def test_patch_block_title_persists():
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        module1 = _children(db, course.id)[0]
        lesson = _children(db, module1.id)[0]
        lesson_id = lesson.id
    finally:
        db.close()

    r = client.patch(f"/blocks/{lesson_id}", json={"title": "Downstrokes & Upstrokes"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Downstrokes & Upstrokes"

    # Round-trip via a separate request — genuinely persisted, not just echoed.
    r2 = client.get(f"/blocks/{lesson_id}")
    assert r2.status_code == 200
    assert r2.json()["title"] == "Downstrokes & Upstrokes"


def test_patch_block_only_updates_provided_fields_and_leaves_siblings_alone():
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        module1 = _children(db, course.id)[0]
        lessons = _children(db, module1.id)
        lesson_a, lesson_b = lessons[0], lessons[1]
        lesson_a_id, lesson_b_id = lesson_a.id, lesson_b.id
        original_body = lesson_a.body
    finally:
        db.close()

    r = client.patch(f"/blocks/{lesson_a_id}", json={"est_minutes": 999})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["est_minutes"] == 999
    assert body["title"] == "M1 Lesson 1"   # untouched
    assert body["body"] == original_body    # untouched

    sibling = client.get(f"/blocks/{lesson_b_id}").json()
    assert sibling["est_minutes"] == 35     # sibling completely unaffected


def test_patch_block_404_for_unknown_id():
    r = client.patch("/blocks/00000000-0000-0000-0000-000000000000", json={"title": "x"})
    assert r.status_code == 404


def test_patch_block_null_title_is_ignored_not_500():
    """`Block.title` is a NOT NULL column but `BlockUpdate.title` accepts
    `null` at the HTTP boundary - an explicit `{"title": null}` used to
    reach `setattr(block, "title", None)` -> `db.commit()` -> a NOT NULL
    IntegrityError surfacing as a raw 500. Fixed: null fields are dropped
    from the update entirely (a NOT NULL column can't be cleared), so this
    is a 200 no-op on `title`, not a crash.
    """
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        module1 = _children(db, course.id)[0]
        lesson = _children(db, module1.id)[0]
        lesson_id = lesson.id
        original_title = lesson.title
    finally:
        db.close()

    r = client.patch(f"/blocks/{lesson_id}", json={"title": None})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == original_title

    # Round-trip via a separate request - genuinely unchanged, not just echoed.
    r2 = client.get(f"/blocks/{lesson_id}")
    assert r2.json()["title"] == original_title


def test_patch_block_empty_string_title_rejected_422():
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        module1 = _children(db, course.id)[0]
        lesson = _children(db, module1.id)[0]
        lesson_id = lesson.id
        original_title = lesson.title
    finally:
        db.close()

    r = client.patch(f"/blocks/{lesson_id}", json={"title": ""})
    assert r.status_code == 422, r.text

    # Rejected before any mutation - title is still whatever it was before.
    r2 = client.get(f"/blocks/{lesson_id}")
    assert r2.json()["title"] == original_title


# --- DELETE /blocks/{id} ----------------------------------------------------

def test_delete_block_cascades_children_and_leaves_siblings():
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        course_id = course.id
        module1 = _children(db, course.id)[0]
        module1_id, module1_lesson_count = module1.id, len(_children(db, module1.id))
    finally:
        db.close()
    assert module1_lesson_count == 2

    r = client.delete(f"/blocks/{module1_id}")
    assert r.status_code == 204
    assert client.get(f"/blocks/{module1_id}").status_code == 404

    db2 = SessionLocal()
    try:
        remaining = _children(db2, course_id)
        assert len(remaining) == 1  # only Module 2 left
        assert remaining[0].title == "Module 2"
    finally:
        db2.close()


def test_delete_unknown_block_404s():
    r = client.delete("/blocks/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


# --- GET /curricula (templates list) ---------------------------------------

def test_list_curricula_includes_template_root_with_expected_fields():
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        course_id = course.id
    finally:
        db.close()

    r = client.get("/curricula")
    assert r.status_code == 200, r.text
    items = {item["id"]: item for item in r.json()}
    assert str(course_id) in items
    item = items[str(course_id)]
    assert item["title"] == "Rhythm Fundamentals"
    assert item["language"] == "en"
    assert item["target_profile"] == {"level": "beginner"}
    assert item["created_at"]


def test_list_curricula_excludes_non_root_blocks():
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        module1 = _children(db, course.id)[0]
        module1_id = module1.id
    finally:
        db.close()

    r = client.get("/curricula")
    ids = {item["id"] for item in r.json()}
    assert str(module1_id) not in ids  # a module (parent_id set) is never a template root


# --- POST /blocks/{id}/segment ----------------------------------------------

def test_segment_course_returns_delivery_sessions():
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        course_id = course.id
    finally:
        db.close()

    r = client.post(f"/blocks/{course_id}/segment", json={"session_minutes": 50})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["kind"] == "delivery_root"
    assert body["plane"] == "delivery"
    assert body["children"], "expected at least one session"
    assert all(s["kind"] == "session" for s in body["children"])
    assert all(s["plane"] == "delivery" for s in body["children"])
    assert all(s["student_id"] is None for s in body["children"])
    assert sum(s["est_minutes"] for s in body["children"]) == 40 + 35 + 50 + 45


def test_segment_unknown_block_404s():
    r = client.post(
        "/blocks/00000000-0000-0000-0000-000000000000/segment", json={"session_minutes": 50}
    )
    assert r.status_code == 404


def test_segment_rejects_non_positive_session_minutes():
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        course_id = course.id
    finally:
        db.close()

    r = client.post(f"/blocks/{course_id}/segment", json={"session_minutes": 0})
    assert r.status_code == 422


# --- POST /curricula/{root_id}/assign ---------------------------------------

def test_assign_curriculum_deep_clones_into_a_new_student_scoped_tree():
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        course_id = course.id
    finally:
        db.close()

    student = _create_student()
    student_id = student["id"]

    r = client.post(f"/curricula/{course_id}/assign", json={"student_id": student_id})
    assert r.status_code == 200, r.text
    tree = r.json()

    new_root_id = tree["id"]
    assert new_root_id != str(course_id)              # a NEW root, distinct from the template
    assert tree["title"] == "Rhythm Fundamentals"      # structure/content preserved
    assert tree["student_id"] == student_id
    assert [m["title"] for m in tree["children"]] == ["Module 1", "Module 2"]

    for module in tree["children"]:
        assert module["student_id"] == student_id
        assert len(module["children"]) == 2
        for lesson in module["children"]:
            assert lesson["student_id"] == student_id
            assert lesson["children"] == []

    # is_template=False + student_id set on EVERY node, verified server-side
    # against the actual DB rows (not just the response body).
    db2 = SessionLocal()
    try:
        def _assert_assigned_subtree(block_id: str) -> None:
            b = db2.get(Block, uuid.UUID(block_id))
            assert b is not None
            assert b.is_template is False
            assert b.student_id == uuid.UUID(student_id)
            for kid in _children(db2, b.id):
                _assert_assigned_subtree(str(kid.id))

        _assert_assigned_subtree(new_root_id)

        # An Assignment row links the student to the TEMPLATE block (not the clone).
        assignments = db2.scalars(
            select(Assignment).where(Assignment.student_id == uuid.UUID(student_id))
        ).all()
        assert len(assignments) == 1
        assert assignments[0].curriculum_block_id == course_id

        # The template itself is untouched by the clone.
        template = db2.get(Block, course_id)
        assert template.is_template is True
        assert template.student_id is None
        assert len(_children(db2, course_id)) == 2
    finally:
        db2.close()


def test_assign_unknown_curriculum_404s():
    student = _create_student("Assign 404 Student")
    r = client.post(
        "/curricula/00000000-0000-0000-0000-000000000000/assign",
        json={"student_id": student["id"]},
    )
    assert r.status_code == 404


def test_assign_unknown_student_404s():
    db = SessionLocal()
    try:
        course = _build_template_course(db)
        course_id = course.id
    finally:
        db.close()

    r = client.post(
        f"/curricula/{course_id}/assign",
        json={"student_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert r.status_code == 404


# --- POST /curricula/generate (live) ----------------------------------------

@pytest.mark.integration
def test_generate_curriculum_endpoint_returns_real_tree():
    """Hits the live LLM+embed stack via POST /curricula/generate — allow it
    to be slow (guided-JSON generation measured at 49-179s/call, see Plan 3
    Task 2's report). Domain is uuid-tagged per run (not a fixed literal) —
    this codebase's own documented lesson (progress.md) for avoiding
    cross-run pollution on this shared, never-torn-down-mid-suite DB.
    """
    domain = f"tone-api-{uuid.uuid4().hex[:8]}"

    db = SessionLocal()
    try:
        source = KnowledgeSource(type="text", title="API Tone Basics", language="en", domain=domain)
        db.add(source)
        db.commit()
        ingest_source(db, source.id, IngestPayload(kind="text", text=_TONE_TEXT))
        db.refresh(source)
        assert source.status == "ready", f"seed ingestion failed: {source.status}/{source.error}"
    finally:
        db.close()

    r = client.post(
        "/curricula/generate",
        json={
            "title": "Guitar Tone Basics",
            "language": "en",
            "profile": {"level": "intermediate"},
            "domain": domain,
            "target_minutes_total": 600,
        },
    )
    assert r.status_code == 200, r.text
    tree = r.json()

    assert tree["kind"] == "course"
    assert tree["plane"] == "content"
    assert tree["student_id"] is None
    assert len(tree["children"]) >= 2
    assert all(m["kind"] == "module" for m in tree["children"])

    all_lessons = [l for m in tree["children"] for l in m["children"]]
    assert all_lessons
    assert all(l["kind"] == "lesson" and l["est_minutes"] for l in all_lessons)

    haystack = " ".join(
        f"{n['title']} {n.get('body') or ''}" for n in [tree, *tree["children"], *all_lessons]
    ).lower()
    assert any(k in haystack for k in _TONE_KEYWORDS), (
        f"expected one of {_TONE_KEYWORDS} in generated tree text, got: {haystack[:2000]!r}"
    )

    # The persisted root is a genuine template — checked directly against the DB.
    db2 = SessionLocal()
    try:
        root = db2.get(Block, uuid.UUID(tree["id"]))
        assert root.is_template is True
    finally:
        db2.close()

    # GET /curricula/{root} round-trips the same tree via a separate request.
    r2 = client.get(f"/curricula/{tree['id']}")
    assert r2.status_code == 200
    assert r2.json()["id"] == tree["id"]

    # And it shows up in the templates list.
    r3 = client.get("/curricula")
    ids = {item["id"] for item in r3.json()}
    assert tree["id"] in ids

    print("\nGenerated module titles:", [m["title"] for m in tree["children"]])
    print("Generated lesson titles:", [l["title"] for l in all_lessons])
