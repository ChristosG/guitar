"""`POST /curricula/{root_id}/redraft` — Plan C, Task 8.

A blueprint edit must NEVER re-draft (spec invariant #8: "Re-draft is OPT-IN").
This is the ONLY route that rewrites lessons that already drafted — it flips
EVERY non-gap lesson back to `queued` (READY ones included, which is exactly
what `POST .../draft` (Resume) does NOT do — Resume only picks up `queued`/
`failed`) and reschedules the ordinary fan-out, which already reads
`course.meta["blueprint"]` fresh in its own Phase A. So this route needs no
blueprint plumbing of its own: requeue, then let the existing job re-resolve it.
"""
import uuid

from sqlalchemy import select

from app.curriculum.corpus import build_library_context
from app.curriculum.edit import add_lesson
from app.curriculum.outline import TIER_GAP, materialize_outline
from app.curriculum.shape import plan_shape
from app.models.block import Block
from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource, Page

SHAPE = plan_shape(6, 1, 50)  # 6 lessons / 2 modules x 3


def _book(db) -> KnowledgeSource:
    source = KnowledgeSource(type="pdf", title="Book", language="en", status="ready")
    db.add(source)
    db.flush()
    db.add(Page(source_id=source.id, page_no=19,
                text="A humbucker cancels hum by pairing opposed coils. " * 4,
                status="ready"))
    db.commit()
    return source


def _outline(with_gap: bool = False) -> dict:
    modules = [
        {
            "title": f"Module {i}", "objective": "o", "tier": "library",
            "coverage_note": "p.19",
            "lessons": [
                {"title": f"L{i}.{j}", "objective": "o", "est_minutes": 50}
                for j in range(3)
            ],
        }
        for i in range(2)
    ]
    if with_gap:
        modules.append({
            "title": "Gap module", "objective": "o", "tier": "gap",
            "coverage_note": "not covered by your library", "lessons": [],
        })
    return {"title": "Tone Fundamentals", "modules": modules}


def _course(db, with_gap: bool = False) -> uuid.UUID:
    source = _book(db)
    return materialize_outline(
        db, _outline(with_gap), title="Tone Fundamentals", language="en", shape=SHAPE,
        library=build_library_context(db, [source.id]), source_ids=[source.id],
    )


def _lessons(db, root_id) -> list[Block]:
    module = Block.__table__.alias("m")
    return db.scalars(
        select(Block)
        .join(module, Block.parent_id == module.c.id)
        .where(module.c.parent_id == root_id, Block.kind == "lesson")
        .order_by(module.c.order, Block.order)
    ).all()


def _statuses(db, root_id) -> list[str | None]:
    return [(l.meta or {}).get("draft_status") for l in _lessons(db, root_id)]


def _patch_job(monkeypatch):
    """`run_curriculum_draft_job` imported at module level in
    `routers/curriculum.py` — same reason `run_outline_job`/the plain
    `/draft` resume route patch it: `TestClient` runs `BackgroundTasks`
    in-process AFTER the response, so an unpatched test would fire the real
    (LLM-calling) fan-out."""
    import app.routers.curriculum as router_mod

    scheduled: list[uuid.UUID] = []
    monkeypatch.setattr(router_mod, "run_curriculum_draft_job", scheduled.append)
    return scheduled


# ---------------------------------------------------------------------------
# The core behaviour
# ---------------------------------------------------------------------------


def test_redraft_requeues_every_lesson_including_already_ready_ones(db, client, monkeypatch):
    """THE POINT of this route rather than the plain `/draft` Resume: it flips
    READY lessons back to `queued` too."""
    scheduled = _patch_job(monkeypatch)

    root_id = _course(db)
    for lesson in _lessons(db, root_id):
        lesson.meta = {**(lesson.meta or {}), "draft_status": "ready", "word_count": 2000}
    db.commit()

    r = client.post(f"/curricula/{root_id}/redraft")

    assert r.status_code == 202, r.text
    job_id = uuid.UUID(r.json()["job_id"])
    assert scheduled == [job_id]

    db.expire_all()
    assert _statuses(db, root_id) == ["queued"] * 6

    job = db.get(GenerationJob, job_id)
    assert job.kind == "curriculum_draft"
    assert job.params["root_id"] == str(root_id)


def test_redraft_clears_a_previous_error(db, client, monkeypatch):
    _patch_job(monkeypatch)

    root_id = _course(db)
    lessons = _lessons(db, root_id)
    lessons[0].meta = {**(lessons[0].meta or {}), "draft_status": "failed", "error": "boom"}
    db.commit()

    client.post(f"/curricula/{root_id}/redraft")

    db.expire_all()
    lesson = db.get(Block, lessons[0].id)
    assert lesson.meta["draft_status"] == "queued"
    assert lesson.meta["error"] is None


def test_redraft_skips_lessons_under_a_gap_module(db, client, monkeypatch):
    """A gap module gets NO lessons at materialize time (`outline.py` — 'nothing
    to draft, no call is made'), but a tutor can add one manually later
    (`edit.add_lesson` never checks the module's tier). That lesson must not be
    swept into a redraft: nothing there was ever meant to be drafted."""
    _patch_job(monkeypatch)

    root_id = _course(db, with_gap=True)
    gap_module = db.scalars(
        select(Block).where(Block.parent_id == root_id, Block.kind == "module")
        .order_by(Block.order)
    ).all()[-1]
    assert gap_module.meta["tier"] == TIER_GAP

    manual_lesson = add_lesson(db, gap_module.id, title="Manually added", objective="o")
    manual_lesson.meta = {**manual_lesson.meta, "draft_status": "ready", "word_count": 1}
    for lesson in _lessons(db, root_id):
        if lesson.id != manual_lesson.id:
            lesson.meta = {**(lesson.meta or {}), "draft_status": "ready", "word_count": 2000}
    db.commit()

    client.post(f"/curricula/{root_id}/redraft")

    db.expire_all()
    assert db.get(Block, manual_lesson.id).meta["draft_status"] == "ready", (
        "a lesson under a gap module must not be redrafted"
    )
    others = [l for l in _lessons(db, root_id) if l.id != manual_lesson.id]
    assert all((l.meta or {}).get("draft_status") == "queued" for l in others)


def test_redraft_404s_on_an_unknown_curriculum(db, client):
    r = client.post(f"/curricula/{uuid.uuid4()}/redraft")
    assert r.status_code == 404


def test_redraft_404s_on_a_non_course_block(db, client):
    root_id = _course(db)
    module = db.scalars(
        select(Block).where(Block.parent_id == root_id, Block.kind == "module")
    ).first()
    r = client.post(f"/curricula/{module.id}/redraft")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Invariant #8 — NEVER coupled to a blueprint save
# ---------------------------------------------------------------------------


def test_a_blueprint_save_never_triggers_a_redraft(db, client, monkeypatch):
    """The blueprint routes (Task 4) never call the requeue — there is no
    coupling. Only an explicit POST to THIS route touches `draft_status`."""
    scheduled = _patch_job(monkeypatch)

    root_id = _course(db)
    for lesson in _lessons(db, root_id):
        lesson.meta = {**(lesson.meta or {}), "draft_status": "ready", "word_count": 2000}
    db.commit()

    from app.curriculum.blueprint import default_blueprint

    custom = default_blueprint()
    custom["sections"][0]["description"] = "A brand new warm-up description, tutor-written."
    r = client.put("/blueprint/default", json={"blueprint": custom})
    assert r.status_code == 200, r.text

    assert scheduled == [], "saving the settings-default blueprint must not schedule a draft job"
    db.expire_all()
    assert _statuses(db, root_id) == ["ready"] * 6, "a blueprint save must not touch any lesson's draft_status"
