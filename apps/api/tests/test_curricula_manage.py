"""Rename/delete for curriculum roots — the dedicated /curricula management
routes (spec 2026-07-20 §3). The generic /blocks routes stay untouched; these
add course-root validation and delete-side cleanup of the FK-less pointers
(ChatSession.root_id, CurriculumInterview.root_id)."""
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models.block import Block
from app.models.chat import ChatSession, Message
from app.models.interview import CurriculumInterview
from app.models.generation_job import GenerationJob

client = TestClient(app)


def _mk_course(db, title="Ήχος και Ενισχυτές"):
    course = Block(kind="course", title=title, is_template=True, order=0, plane="content")
    db.add(course); db.flush()
    module = Block(kind="module", title="Ενότητα 1", parent_id=course.id, order=0, plane="content")
    db.add(module); db.flush()
    lesson = Block(kind="lesson", title="Μάθημα 1", parent_id=module.id, order=0, plane="content",
                   meta={"draft_status": "ready"})
    db.add(lesson); db.commit()
    return course, module, lesson


def test_rename_curriculum():
    db = SessionLocal()
    try:
        course, _, _ = _mk_course(db)
        r = client.patch(f"/curricula/{course.id}", json={"title": "Νέος τίτλος"})
        assert r.status_code == 200
        assert r.json()["title"] == "Νέος τίτλος"
        db.expire_all()
        assert db.get(Block, course.id).title == "Νέος τίτλος"
    finally:
        db.close()


def test_rename_rejects_empty_title_and_non_roots():
    db = SessionLocal()
    try:
        course, module, _ = _mk_course(db)
        assert client.patch(f"/curricula/{course.id}", json={"title": ""}).status_code == 422
        assert client.patch(f"/curricula/{module.id}", json={"title": "x"}).status_code == 404
    finally:
        db.close()


def test_rename_rejects_whitespace_only_title():
    """The stripped title is validated BEFORE it is assigned to `course.title`
    (review fix) — a whitespace-only title must 422 same as an empty one, and
    must never land on the row even transiently."""
    db = SessionLocal()
    try:
        course, _, _ = _mk_course(db)
        original = course.title
        r = client.patch(f"/curricula/{course.id}", json={"title": "   "})
        assert r.status_code == 422
        db.expire_all()
        assert db.get(Block, course.id).title == original
    finally:
        db.close()


def test_delete_curriculum_cascades_and_cleans_bound_rows():
    db = SessionLocal()
    try:
        course, module, lesson = _mk_course(db)
        chat = ChatSession(root_id=course.id, locale="el")
        db.add(chat); db.flush()
        db.add(Message(session_id=chat.id, role="user", content="γεια"))
        # `title` is CurriculumInterview's one NOT NULL column with no default
        # (see models/interview.py) — required here or the insert 500s on a
        # constraint violation before the test ever reaches the route under test.
        db.add(CurriculumInterview(root_id=course.id, title=course.title))
        db.commit()
        chat_id, course_id = chat.id, course.id
        # Captured as plain UUIDs, same reason as chat_id/course_id above: the
        # DELETE route runs on ITS OWN session (Depends(get_db)), so this
        # test's `module`/`lesson` objects stay in this session's identity
        # map after their row is gone elsewhere. Post-`expire_all()`, even
        # touching `module.id` (a PK attribute) forces a refresh of the
        # instance and raises `ObjectDeletedError` instead of returning a
        # value — see tests/test_lesson_routes.py's `_lesson_with_one_long_
        # session` test for the same documented gotcha. `db.get()` with a
        # pre-captured plain id has no such problem.
        module_id, lesson_id = module.id, lesson.id

        r = client.delete(f"/curricula/{course_id}")
        assert r.status_code == 204
        db.expire_all()
        assert db.get(Block, course_id) is None
        assert db.get(Block, module_id) is None and db.get(Block, lesson_id) is None
        assert db.get(ChatSession, chat_id) is None
        assert db.query(CurriculumInterview).filter_by(root_id=course_id).count() == 0
    finally:
        db.close()


def test_delete_refuses_while_a_job_is_active():
    """409 when a `pending`/`running` `GenerationJob` actually references this
    root — via `params["root_id"]`, which is how `curriculum_draft` (Resume/
    redraft/deepen)/`curriculum_revise`/`module_generate` all carry it for
    their entire active lifetime (`result_root_id` is only set at finalize,
    i.e. once the job is no longer active — see the route's own comment)."""
    db = SessionLocal()
    try:
        course, _, _ = _mk_course(db)
        job = GenerationJob(
            kind="curriculum_draft", status="running", params={"root_id": str(course.id)},
        )
        db.add(job); db.commit()
        r = client.delete(f"/curricula/{course.id}")
        assert r.status_code == 409
        db.expire_all()
        assert db.get(Block, course.id) is not None
    finally:
        db.close()


def test_delete_allows_stuck_drafting_marker_with_no_active_job():
    """A lesson stuck at `draft_status == 'drafting'` with NO active job behind
    it (a stale marker from a past worker crash, e.g. a sweep that never ran)
    must NOT make deletion permanently impossible — this is the behavior
    change from the old lesson-only check."""
    db = SessionLocal()
    try:
        course, _, lesson = _mk_course(db)
        lesson.meta = {**(lesson.meta or {}), "draft_status": "drafting"}
        db.commit()
        course_id = course.id  # captured pre-delete — see the cascade test's own
        # comment on why touching a PK attribute post-delete raises ObjectDeletedError.
        r = client.delete(f"/curricula/{course_id}")
        assert r.status_code == 204
        db.expire_all()
        assert db.get(Block, course_id) is None
    finally:
        db.close()


def test_delete_nulls_generation_job_result_root_id_pointers():
    """Spec §3: a FINISHED job that points `result_root_id` at this root (e.g.
    the chat job-card the tutor deep-links from) must not keep pointing at a
    now-deleted curriculum after this route runs — 404 city for a click on a
    card that looks perfectly fine."""
    db = SessionLocal()
    try:
        course, _, _ = _mk_course(db)
        job = GenerationJob(
            kind="curriculum_draft", status="succeeded",
            params={"root_id": str(course.id)}, result_root_id=course.id,
        )
        db.add(job); db.commit()
        job_id = job.id

        r = client.delete(f"/curricula/{course.id}")
        assert r.status_code == 204
        db.expire_all()
        assert db.get(GenerationJob, job_id).result_root_id is None
    finally:
        db.close()


def test_delete_non_root_is_404():
    db = SessionLocal()
    try:
        _, module, _ = _mk_course(db)
        assert client.delete(f"/curricula/{module.id}").status_code == 404
    finally:
        db.close()
