"""`/curricula` and `/blocks` routes: curriculum generation, tree retrieval,
block CRUD, deterministic segmentation, and per-student deep-clone
assignment.

One `APIRouter` (no `prefix=`, per-route explicit paths) since routes span
two URL roots (`/curricula/...` and `/blocks/...`) — mirrors how
`app/main.py` is told to "wire both routers" (this one + `students.router`),
not one-router-per-prefix.

No-auth PoC posture, same as `routers/knowledge.py` — no
authentication/authorization here either; this deploys origin-locked behind
Cloudflare for a single user.
"""
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.curriculum import interview as interview_service
from app.curriculum.assign import clone_content_subtree
from app.curriculum.segment import segment_block
from app.db import get_db
from app.jobs.runner import run_curriculum_job
from app.models.block import Block
from app.models.curriculum import Assignment
from app.models.generation_job import GenerationJob
from app.models.interview import CurriculumInterview
from app.models.student import Student
from app.schemas.curriculum import (
    AssignRequest,
    BlockTreeOut,
    BlockUpdate,
    CurriculumGenerateRequest,
    CurriculumListItem,
    SegmentRequest,
)
from app.schemas.interview import InterviewAnswerRequest, InterviewStartRequest, InterviewStateOut
from app.schemas.jobs import JobAccepted

router = APIRouter(tags=["curriculum"])


def block_to_tree(block: Block) -> dict:
    """Recursively serialize a Block subtree into the nested shape every
    route in this module returns: {id, kind, title, body, est_minutes,
    order, language, plane, student_id, children: [...]}, children ordered
    by `order`.

    Deliberately takes a single already-loaded `Block` (no `db` parameter,
    per the brief's exact signature) and walks `Block.children` — the ORM
    relationship — to reach descendants. Empirically verified (see this
    task's report) that this relationship resolves parent->children with
    correct direction/cardinality but with NO guaranteed order (the model
    declares no `order_by=` on it), hence the explicit `sorted(...,
    key=...order)` below rather than trusting relationship/DB return order.
    """
    return {
        "id": block.id,
        "kind": block.kind,
        "title": block.title,
        "body": block.body,
        "est_minutes": block.est_minutes,
        "order": block.order,
        "language": block.language,
        "plane": block.plane,
        "student_id": block.student_id,
        "children": [
            block_to_tree(child) for child in sorted(block.children, key=lambda b: b.order)
        ],
    }


def _get_block_or_404(db: Session, block_id: UUID) -> Block:
    block = db.get(Block, block_id)
    if block is None:
        raise HTTPException(status_code=404, detail="block not found")
    return block


def _get_interview_or_404(db: Session, interview_id: UUID) -> CurriculumInterview:
    interview = db.get(CurriculumInterview, interview_id)
    if interview is None:
        raise HTTPException(status_code=404, detail="interview not found")
    return interview


@router.get("/curricula", response_model=list[CurriculumListItem])
def list_curricula(db: Session = Depends(get_db)) -> list[CurriculumListItem]:
    roots = db.scalars(
        select(Block)
        .where(Block.is_template.is_(True), Block.parent_id.is_(None))
        .order_by(Block.created_at.desc())
    ).all()
    return [CurriculumListItem.model_validate(b, from_attributes=True) for b in roots]


@router.post("/curricula/interview", response_model=InterviewStateOut, status_code=201)
def start_curriculum_interview(
    payload: InterviewStartRequest, db: Session = Depends(get_db)
) -> dict:
    """Start the guided curriculum interview (Plan 12 Task 3, G2) — the
    first of its five code-driven steps ("who"). See
    `app.curriculum.interview`'s module docstring for why this whole flow
    is a state machine in code, not a free-form chat.
    """
    interview = interview_service.start_interview(db, title=payload.title, domain=payload.domain)
    return interview_service.render_state(db, interview)


@router.get("/curricula/interview/{interview_id}", response_model=InterviewStateOut)
def get_curriculum_interview(interview_id: UUID, db: Session = Depends(get_db)) -> dict:
    """Current state of an in-progress interview — a page refresh (or a
    lost connection) never loses progress, and never re-runs the "preview"
    step's LLM/retrieval calls: `render_state` only ever READS the cached
    `CurriculumInterview.preview` column, it never recomputes it.
    """
    interview = _get_interview_or_404(db, interview_id)
    return interview_service.render_state(db, interview)


@router.post("/curricula/interview/{interview_id}/answer", response_model=None)
def answer_curriculum_interview(
    interview_id: UUID,
    payload: InterviewAnswerRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Advance one step of the interview, or — on the final "confirm" step,
    once approved — enqueue the REAL grounded generation job and return
    `202 {job_id, status}` (mirrors `generate_curriculum_endpoint` below
    exactly: same `GenerationJob` row shape, same `BackgroundTasks.add_task
    (run_curriculum_job, job.id)` scheduling, same reason `run_curriculum_job`
    is imported at module level — so tests can monkeypatch
    `app.routers.curriculum.run_curriculum_job`; Starlette's `TestClient`
    runs `BackgroundTasks` in-process AFTER the response, so an unpatched
    test here would trigger a real generation run).

    A bad/blank/invalid answer (`app.curriculum.interview.answer_interview`
    returning `ok=False`) re-asks the SAME step's question with `error` set
    — `interview.step` never advances and this never raises. `db.commit()`
    happens unconditionally right after `answer_interview` returns, whether
    the answer was accepted or not: on an invalid answer nothing about the
    row actually changed (`step` is untouched), so the commit is a no-op;
    keeping it unconditional (rather than branching on `result["ok"]`)
    avoids two separate commit call sites for what is, either way, "this
    request is done touching the DB."
    """
    interview = _get_interview_or_404(db, interview_id)
    result = interview_service.answer_interview(db, interview, payload.answer)
    db.commit()

    if not result["ok"]:
        return interview_service.render_state(db, interview, error=result["error"])

    if result["done"]:
        job = GenerationJob(kind="curriculum", status="pending", params=result["params"])
        db.add(job)
        # `job.id`'s `default=uuid.uuid4` is a client-side default that
        # SQLAlchemy only actually generates at flush time — reading
        # `job.id` any earlier returns `None` (bug caught by this task's own
        # test: `interview.job_id` came back `None` after commit). Flush
        # BEFORE reading `job.id`, so the id `interview.job_id` records
        # below is the real one, not `None`.
        db.flush()
        interview.job_id = job.id
        db.commit()
        db.refresh(job)
        background_tasks.add_task(run_curriculum_job, job.id)
        return JSONResponse(status_code=202, content={"job_id": str(job.id), "status": job.status})

    return interview_service.render_state(db, interview)


@router.post("/curricula/generate", response_model=JobAccepted, status_code=202)
def generate_curriculum_endpoint(
    payload: CurriculumGenerateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> JobAccepted:
    """Enqueue curriculum generation and return immediately (Plan 8 Task 3)
    — this endpoint no longer runs `generate_curriculum` itself.
    `generate_curriculum` is a blocking guided-JSON LLM call measured at
    49-179s/call (Plan 3 Task 2's report), too slow for a synchronous
    request/response cycle and past Cloudflare's ~100s edge timeout under
    orange-cloud. Instead, this creates a `GenerationJob(status="pending")`
    row and schedules `run_curriculum_job` (`app.jobs.runner`, T2 — its own
    `SessionLocal()`) via `BackgroundTasks`; the actual generation, and its
    error handling (`GuidedJSONError`/`APIConnectionError`/`TransportError`/
    anything else -> `error_kind` "upstream"/"timeout"/"internal" on the
    row), now happens entirely off this request. Poll `GET /jobs/{job_id}`
    (`routers/jobs.py`) for the outcome; once `status == "succeeded"`,
    `result_root_id` is the same root id `GET /curricula/{root_id}` used to
    return here directly.

    `db.commit()` happens BEFORE this returns — NOT left for the background
    task, which Starlette/FastAPI run AFTER the response is sent — so an
    immediate `GET /jobs/{job_id}` poll is guaranteed to see the row.
    `run_curriculum_job` is imported at module level specifically so tests
    can `monkeypatch.setattr("app.routers.curriculum.run_curriculum_job",
    ...)`: Starlette's `TestClient` runs `BackgroundTasks` in-process, after
    the response — an unpatched test would trigger a real 49-179s LLM call.
    """
    params = {
        "title": payload.title,
        "language": payload.language,
        "profile": payload.profile,
        "domain": payload.domain,
        "target_minutes_total": payload.target_minutes_total,
    }
    # Only added when actually requested — keeps `params` byte-for-byte
    # identical to the pre-Task-2 shape for callers that don't use them (see
    # test_curriculum_generate_enqueue.py's
    # ..._persists_request_params_verbatim_on_the_job).
    #
    # `is not None`, NOT truthiness (review fix, MINOR): `payload.source_ids`
    # defaults to `None` (key omitted -> old/back-compat behaviour, unscoped
    # retrieval) but an explicitly-passed `[]` means "ground in nothing" —
    # `if payload.source_ids:` is falsy for BOTH, so it used to silently drop
    # an explicit `[]` from `params` entirely, and `generate_curriculum` would
    # then see its own `source_ids=None` default and fall back to whole-
    # library retrieval — the opposite of what an explicit `[]` asked for.
    if payload.source_ids is not None:
        params["source_ids"] = [str(s) for s in payload.source_ids]
    if payload.allow_general:
        params["allow_general"] = payload.allow_general
    job = GenerationJob(kind="curriculum", status="pending", params=params)
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(run_curriculum_job, job.id)

    return JobAccepted(job_id=job.id, status=job.status)


@router.get("/curricula/{root_id}", response_model=BlockTreeOut)
def get_curriculum(root_id: UUID, db: Session = Depends(get_db)) -> dict:
    return block_to_tree(_get_block_or_404(db, root_id))


@router.get("/blocks/{block_id}", response_model=BlockTreeOut)
def get_block(block_id: UUID, db: Session = Depends(get_db)) -> dict:
    return block_to_tree(_get_block_or_404(db, block_id))


@router.patch("/blocks/{block_id}", response_model=BlockTreeOut)
def update_block(block_id: UUID, payload: BlockUpdate, db: Session = Depends(get_db)) -> dict:
    """`exclude_none=True` (review fix, alongside `exclude_unset=True`): every
    `BlockUpdate` field is nullable at the HTTP boundary, but `Block.title`
    is a NOT NULL column — an explicit `{"title": null}` used to reach
    `setattr(block, "title", None)` -> `db.commit()` -> an unhandled
    IntegrityError (raw 500). Dropping null-valued fields from the update
    entirely (not just `title`) is a uniform, simple fix: a NOT NULL column
    can never be legally cleared anyway, and it means `{"field": null}` is
    now always a no-op rather than being valid for some fields (`body`,
    `est_minutes`) and crashing for others (`title`) depending on the
    column's own nullability — one rule for every field, applied here in the
    router rather than differently per column.

    `title == ""` is rejected outright (422) rather than silently accepted:
    an empty string satisfies the NOT NULL constraint (it isn't NULL) so it
    would otherwise sail through and leave a block with a blank title.
    """
    block = _get_block_or_404(db, block_id)
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if updates.get("title") == "":
        raise HTTPException(status_code=422, detail="title cannot be empty")
    for field, value in updates.items():
        setattr(block, field, value)
    db.commit()
    return block_to_tree(block)


@router.delete("/blocks/{block_id}", status_code=204, response_model=None)
def delete_block(block_id: UUID, db: Session = Depends(get_db)) -> None:
    block = _get_block_or_404(db, block_id)
    db.delete(block)  # ORM cascade="all, delete-orphan" + DB ON DELETE CASCADE both remove descendants
    db.commit()


@router.post("/blocks/{block_id}/segment", response_model=BlockTreeOut)
def segment_block_endpoint(
    block_id: UUID, payload: SegmentRequest, db: Session = Depends(get_db)
) -> dict:
    _get_block_or_404(db, block_id)  # 404 before doing any segmentation work

    segment_block(
        db,
        block_id,
        session_minutes=payload.session_minutes,
        cadence_per_week=payload.cadence_per_week,
        student_id=payload.student_id,
    )

    # Same lookup segment.py's own `_get_or_create_delivery_root` uses
    # internally (not imported from there — that helper is module-private —
    # small deliberate duplication over reaching into another module's `_`
    # helper).
    delivery_root = db.scalars(
        select(Block).where(
            Block.parent_id == block_id,
            Block.kind == "delivery_root",
            Block.plane == "delivery",
            Block.student_id == payload.student_id,
        )
    ).first()
    if delivery_root is None:
        # Currently unreachable: segment_block always finds-or-creates the
        # delivery_root unconditionally, even for zero leaves/sessions.
        # Guarded anyway rather than trusting that invariant blindly across
        # a module boundary.
        raise HTTPException(status_code=422, detail="segmentation produced no delivery root")
    return block_to_tree(delivery_root)


@router.post("/curricula/{root_id}/assign", response_model=BlockTreeOut)
def assign_curriculum(root_id: UUID, payload: AssignRequest, db: Session = Depends(get_db)) -> dict:
    template_root = _get_block_or_404(db, root_id)
    student = db.get(Student, payload.student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="student not found")

    new_root = clone_content_subtree(db, template_root, parent_id=None, student_id=payload.student_id)
    # curriculum_block_id references the TEMPLATE block (per Assignment's own
    # docstring: "A template curriculum Block handed to a specific Student"),
    # not the new clone — this is the audit link "this student was assigned
    # this template"; the clone's own id is what the response/tree exposes
    # for actually working with the student's instance.
    db.add(Assignment(student_id=payload.student_id, curriculum_block_id=template_root.id))
    db.commit()
    return block_to_tree(new_root)
