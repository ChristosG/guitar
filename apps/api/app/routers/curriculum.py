"""`/curricula` and `/blocks` routes: curriculum generation, tree retrieval,
block CRUD, deterministic segmentation, and per-student deep-clone
assignment.

One `APIRouter` (no `prefix=`, per-route explicit paths) since routes span
two URL roots (`/curricula/...` and `/blocks/...`) — mirrors how
`app/main.py` is told to "wire both routers" (this one + `students.router`),
not one-router-per-prefix.

Auth: every route here sits behind the `gt_session` password gate
(`app/auth/middleware.py`), which is a whole-API ASGI middleware rather than a
per-router dependency — so there is nothing to declare in this file. One tutor,
one password; there is still no authorization model, because there is nobody to
authorize against anybody else.
"""
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.curriculum import edit as edit_service
from app.curriculum import from_chat as from_chat_service
from app.curriculum import interview as interview_service
from app.curriculum.assign import clone_content_subtree
from app.curriculum.draft import draft_progress
from app.curriculum.export_docx import build_curriculum_docx, filename_for
from app.curriculum.outline import TIER_GAP
from app.curriculum.refine import refine_block, undo_refine
from app.curriculum.revise import validate_ops
from app.curriculum.segment import segment_block
from app.db import get_db
from app.i18n import locale_dep
from app.jobs.curriculum_draft import run_curriculum_draft_job
from app.jobs.curriculum_revise import run_curriculum_revise_job
from app.jobs.module_generate import run_module_generate_job
from app.jobs.runner import run_curriculum_job, run_outline_job
from app.llm.factory import require_llm_configured
from app.models.artifact import Artifact
from app.models.block import Block
from app.models.chat import ChatSession
from app.models.curriculum import Assignment
from app.models.generation_job import GenerationJob
from app.models.interview import CurriculumInterview
from app.models.student import Student
from app.schemas.chat import ChatSessionCreated
from app.schemas.curriculum import (
    AssignRequest,
    BlockTreeOut,
    BlockUpdate,
    CurriculumGenerateRequest,
    CurriculumListItem,
    CurriculumRenameRequest,
    DraftProgressOut,
    LessonCreate,
    LessonFromChat,
    ModuleCreate,
    ModuleGenerateRequest,
    PlanningBriefIn,
    PlanningBriefOut,
    RefineRequest,
    ReorderRequest,
    ReviseRequest,
    SegmentRequest,
)
from app.schemas.interview import InterviewAnswerRequest, InterviewStartRequest, InterviewStateOut
from app.schemas.jobs import JobAccepted

router = APIRouter(tags=["curriculum"])


def block_to_tree(block: Block, artifacts: dict[UUID, list] | None = None) -> dict:
    """Recursively serialize a Block subtree, children ordered by `order`.

    `meta` IS IN THE RESPONSE NOW, AND ITS ABSENCE WAS A REAL BUG. This function
    used to serialize nine fields, and provenance was not among them. So the
    citation chips, the grounding tiers and the gap badges were written to the
    database by the generator and then silently dropped at the API boundary — the
    board could not have rendered them if it had tried, because they were not in
    the response. The feature existed everywhere except where the tutor could see
    it.

    `artifacts` is a PRE-FETCHED `{block_id: [Artifact]}` map, passed down rather
    than queried per node. Every segment leaf used to fetch its own via `GET
    /artifacts?block_id=` — about 120 parallel requests on one board render, which
    is the actual cause of the "Could not load attached artifacts" error. One query
    up front, zero on the way down.

    Walks `Block.children` (the ORM relationship), which resolves parent->children
    correctly but with NO guaranteed order — the model declares no `order_by=` —
    hence the explicit `sorted`.
    """
    artifacts = artifacts or {}
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
        "meta": block.meta,
        "artifacts": artifacts.get(block.id, []),
        "children": [
            block_to_tree(child, artifacts)
            for child in sorted(block.children, key=lambda b: b.order)
        ],
    }


def _subtree_ids(block: Block) -> list[UUID]:
    ids = [block.id]
    for child in block.children:
        ids.extend(_subtree_ids(child))
    return ids


def _artifacts_for(db: Session, block: Block) -> dict[UUID, list[Artifact]]:
    """Every artifact attached anywhere in `block`'s subtree, in ONE query.

    This is the whole fix for the artifacts N+1 (see `block_to_tree`). The `IN`
    list is the subtree's block ids — a few hundred at the very worst, which is one
    query Postgres does not notice, against ~120 HTTP round trips the browser very
    much did.
    """
    ids = _subtree_ids(block)
    if not ids:
        return {}
    rows = db.scalars(select(Artifact).where(Artifact.block_id.in_(ids))).all()
    out: dict[UUID, list[Artifact]] = {}
    for artifact in rows:
        out.setdefault(artifact.block_id, []).append(artifact)
    return out


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
    interview = interview_service.start_interview(db, title=payload.title)
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


@router.post(
    "/curricula/interview/{interview_id}/answer",
    response_model=None,
    # Every step of the interview either calls the model synchronously (preview)
    # or enqueues a job that will (confirm). Checking the key HERE means the
    # tutor is told "open Settings" before he answers five questions.
    dependencies=[Depends(require_llm_configured)],
)
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

    if result.get("generate_outline"):
        # The outline call reads the whole library and runs ~3 min — past
        # Cloudflare's ~100s edge cap, so it runs OFF this request exactly like the
        # draft job below (and `generate_curriculum_endpoint`). Enqueue and return
        # 202 with NO root_id: nothing is materialized yet, so the frontend polls the
        # job and then re-fetches the outline — it must NOT open the board, which is
        # what the root_id-bearing confirm 202 signals. `interview.job_id` holds this
        # outline job's id (the draft job does not exist until confirm, so the column
        # is free), which lets a refresh mid-run resume the poll via
        # `render_state`'s `job_id`. `run_outline_job` is imported at module level so
        # tests can monkeypatch `app.routers.curriculum.run_outline_job` — same reason
        # as `run_curriculum_draft_job`; Starlette's TestClient runs BackgroundTasks
        # in-process after the response, so an unpatched test would fire the real call.
        job = GenerationJob(
            kind="curriculum_outline",
            status="pending",
            params={"interview_id": str(interview.id)},
        )
        db.add(job)
        db.flush()  # client-side uuid default is generated at FLUSH — see the draft branch
        interview.job_id = job.id
        db.commit()
        db.refresh(job)
        background_tasks.add_task(run_outline_job, job.id)
        return JSONResponse(
            status_code=202,
            content={"job_id": str(job.id), "status": job.status},
        )

    if result["done"]:
        # The TREE ALREADY EXISTS by the time we get here — `_answer_confirm`
        # materialized it, every lesson `queued`. So the 202 carries `root_id` as
        # well as `job_id`, and the tutor's board opens INSTANTLY on a real
        # curriculum with a progress bar, instead of on a spinner waiting for a job
        # that will not produce anything to look at for four minutes.
        job = GenerationJob(
            kind="curriculum_draft",
            status="pending",
            params={"root_id": str(interview.root_id)},
        )
        db.add(job)
        # `job.id`'s `default=uuid.uuid4` is a client-side default SQLAlchemy only
        # generates at FLUSH time — read it any earlier and it is None (a bug this
        # module's tests caught once already). Flush before reading it.
        db.flush()
        interview.job_id = job.id
        db.commit()
        db.refresh(job)
        background_tasks.add_task(run_curriculum_draft_job, job.id)
        return JSONResponse(
            status_code=202,
            content={
                "job_id": str(job.id),
                "root_id": str(interview.root_id),
                "status": job.status,
            },
        )

    return interview_service.render_state(db, interview)


@router.post(
    "/curricula/generate",
    response_model=JobAccepted,
    status_code=202,
    # 409 BEFORE the GenerationJob row exists — see `require_llm_configured`.
    dependencies=[Depends(require_llm_configured)],
)
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
        "brief": payload.brief,
        "weeks": payload.weeks,
        "sessions_per_week": payload.sessions_per_week,
        "minutes_per_session": payload.minutes_per_session,
        "target_minutes_total": payload.target_minutes_total,
        "gap_policy": payload.gap_policy,
        "allow_general": payload.allow_general,
    }
    # `is not None`, NOT truthiness: `source_ids` defaults to `None` ("everything
    # in the library") but an explicit `[]` means "none of it" — and `if
    # payload.source_ids:` is falsy for BOTH, so it used to drop the explicit `[]`
    # and fall back to the whole library, i.e. the exact opposite of what was asked.
    if payload.source_ids is not None:
        params["source_ids"] = [str(s) for s in payload.source_ids]
    if payload.student_id is not None:
        params["student_id"] = str(payload.student_id)
    job = GenerationJob(kind="curriculum", status="pending", params=params)
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(run_curriculum_job, job.id)

    return JobAccepted(job_id=job.id, status=job.status)


@router.get("/curricula/{root_id}", response_model=BlockTreeOut)
def get_curriculum(root_id: UUID, db: Session = Depends(get_db)) -> dict:
    block = _get_block_or_404(db, root_id)
    return block_to_tree(block, _artifacts_for(db, block))


def _get_course_root_or_404(db: Session, root_id: UUID) -> Block:
    """The management routes below act on CURRICULUM ROOTS only — a module or
    lesson id must 404 here (the generic /blocks routes handle those), so a
    frontend bug can never cascade-delete a whole course through this door
    while claiming to remove one lesson.

    `is_template` is deliberately NOT checked here: the spec says "template
    course root", but `clone_content_subtree` (`POST /curricula/{root_id}
    /assign`) stamps its per-student clone with `kind="course"`,
    `parent_id=None` AND `is_template=False` — so a delivery clone's root
    passes every check this function does make. That is accepted as
    harmless rather than tightened: nothing in the UI today hands one of
    THOSE root ids back to a `/curricula/...` management route (rename/
    delete/export/draft/revise all start from the template board), so this
    is a door that is technically open but never actually reachable.
    Narrowing it (an explicit `is_template` check) is left for whenever a
    delivery-side manage surface actually exists to need it.
    """
    block = db.get(Block, root_id)
    if block is None or block.kind != "course" or block.parent_id is not None:
        raise HTTPException(status_code=404, detail="curriculum not found")
    return block


@router.get("/curricula/{root_id}/export.docx")
def export_curriculum_docx(root_id: UUID, db: Session = Depends(get_db)) -> StreamingResponse:
    course = _get_course_root_or_404(db, root_id)
    buf = build_curriculum_docx(db, course)
    fname = filename_for(course)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            # ASCII fallback + RFC 5987 UTF-8 name, because the real name is Greek.
            "Content-Disposition":
                f"attachment; filename=\"curriculum.docx\"; filename*=UTF-8''{quote(fname)}",
        },
    )


@router.patch("/curricula/{root_id}", response_model=CurriculumListItem)
def rename_curriculum(
    root_id: UUID, payload: CurriculumRenameRequest, db: Session = Depends(get_db)
) -> CurriculumListItem:
    course = _get_course_root_or_404(db, root_id)
    stripped = payload.title.strip()
    if not stripped:
        raise HTTPException(status_code=422, detail="title cannot be empty")
    course.title = stripped
    db.commit()
    return CurriculumListItem.model_validate(course, from_attributes=True)


@router.delete("/curricula/{root_id}", status_code=204, response_model=None)
def delete_curriculum(root_id: UUID, db: Session = Depends(get_db)) -> None:
    course = _get_course_root_or_404(db, root_id)

    # Refuse while a job is ACTIVELY writing this curriculum: deleting the tree
    # from under the draft worker strands the job mid-write (it re-reads its
    # lesson block between sections). This used to check for a lesson stuck at
    # `draft_status == "drafting"` instead — which looked equivalent but isn't:
    # a worker crash (OOM, container restart) can leave a lesson at "drafting"
    # forever with nothing left running to finish it (`jobs/sweep.py` is what's
    # supposed to flip that back to "queued" at boot, but a crash-before-sweep
    # window, or a sweep that itself never ran, leaves the marker stuck). A
    # stale marker like that must never make deletion PERMANENTLY impossible —
    # so the real check is "is a job actually in flight", not "does a lesson
    # merely claim to be".
    #
    # `curriculum_draft`/`curriculum_revise`/`module_generate` all thread the
    # root id through `params["root_id"]` (see `resume_curriculum_draft`,
    # `redraft_curriculum`, `revise_curriculum`, `generate_curriculum_module`,
    # and the interview's own confirm branch above) for their entire
    # pending/running lifetime — `result_root_id` is only populated at
    # finalize (`jobs/curriculum_draft.py`'s own comment: "set on every draft
    # job that reaches its finalize step"), i.e. exactly when the job is no
    # longer active. So `params["root_id"]` is the field that actually tells
    # us a job is in flight; `result_root_id` is checked too, defensively, in
    # case some future job kind ever sets it earlier.
    active_job = db.scalars(
        select(GenerationJob).where(
            GenerationJob.status.in_(("pending", "running")),
            or_(
                GenerationJob.result_root_id == course.id,
                GenerationJob.params["root_id"].as_string() == str(course.id),
            ),
        )
    ).first()
    if active_job is not None:
        raise HTTPException(
            status_code=409,
            detail="lessons are still being drafted — wait for the draft to finish before deleting",
        )

    # The FK-less pointers (deliberate — see models/chat.py's root_id comment):
    # an EXPLICIT curriculum delete takes its bound revise-chat sessions and
    # interviews with it. That comment's concern is board edits wiping
    # conversations as a SIDE effect; this is the tutor saying "delete this
    # course", and a revise chat about a course that no longer exists is
    # noise in his sidebar, not a record worth keeping.
    #
    # Only `ChatSession` itself needs an explicit delete here. `Message.
    # session_id` and `ApprovalRequest.session_id` ARE real ForeignKeys with
    # `ondelete="CASCADE"` (models/chat.py) — unlike `ChatSession.root_id`,
    # which is deliberately FK-less — so the database drops both tables' rows
    # for us the moment their owning `ChatSession` row is deleted. Deleting
    # them here too would just be redundant round trips to a table the DB is
    # already about to empty.
    session_ids = db.scalars(
        select(ChatSession.id).where(ChatSession.root_id == course.id)
    ).all()
    if session_ids:
        db.query(ChatSession).filter(ChatSession.id.in_(session_ids)).delete(synchronize_session=False)
    db.query(CurriculumInterview).filter(CurriculumInterview.root_id == course.id).delete(
        synchronize_session=False
    )
    # Spec §3: null out any (necessarily now-finished, since the active-job
    # check above already refused a delete otherwise) `GenerationJob.
    # result_root_id` pointing at this root. Those rows are an audit/poll
    # record, not a live reference (`models/generation_job.py`'s own
    # docstring: "a dangling id here is expected"), but chat renders job cards
    # that deep-link `result_root_id` -> `/curricula/{root_id}` — left
    # pointing at a deleted course, that link 404s the moment the tutor
    # clicks it. Clearing it here means the card still shows its own history
    # (kind, status, error) without the dead link.
    db.query(GenerationJob).filter(GenerationJob.result_root_id == course.id).update(
        {"result_root_id": None}, synchronize_session=False
    )
    # `edit_service.delete_block` commits internally (the existing `DELETE
    # /blocks` route calls it bare) — this commit runs BEFORE it so that if
    # `delete_block` itself fails, the bound rows above (chat sessions,
    # interviews, job pointers) are already gone but the course block is
    # still sitting on the board. That is the recoverable half of this
    # failure: the tutor sees the same course, minus its now-orphaned chat
    # history, and a plain RETRY of the delete finishes the job. The reverse
    # order — deleting the course first, cleanup second — could never be
    # retried if cleanup then failed: the course is already gone, so the next
    # delete attempt 404s on `_get_course_root_or_404` before it ever reaches
    # the cleanup it still needs to run, leaving orphaned sessions/interviews/
    # job pointers with no route left that can clean them up.
    db.commit()

    edit_service.delete_block(db, course.id)


@router.get("/curricula/{root_id}/progress", response_model=DraftProgressOut)
def get_curriculum_progress(root_id: UUID, db: Session = Depends(get_db)) -> dict:
    """What the board polls while the lessons are being written.

    A GROUP BY over the lesson blocks, computed fresh on every poll — NOT a counter
    on the job row. The blocks are what the tutor is looking at; a cached count
    would be a second truth, and it would disagree with the tree the first time he
    deleted a lesson mid-draft.

    Cheap on purpose: this runs every 2 seconds, and if it cannot get a database
    connection it times out, and a timed-out progress poll looks exactly like the
    flagship feature being broken. (See `jobs/curriculum_draft.py` on why the draft
    workers hold no connection across the model call, and `config.db_pool_size`.)
    """
    _get_block_or_404(db, root_id)
    return {"root_id": root_id, **draft_progress(db, root_id),
            "draft_error": _latest_draft_error(db, root_id)}


def _latest_draft_error(db: Session, root_id: UUID) -> str | None:
    """The reason the most RECENT draft run for this curriculum failed, or None.

    The revise chain (and generation) run the draft on their OWN `curriculum_draft`
    job row; if that row `failed` — no API key, every lesson upstream-failed — its
    lessons sit `queued` and, without this, the board shows a silent "processing…".
    Surface the job's own `error` instead. Scoped to the LATEST such job so a Resume
    that later succeeds clears it (the newest draft row is then the successful one).

    `result_root_id` is set on every draft job that reaches its finalize step
    (success and the all-failed case alike); a job that died in Phase A before that
    is an internal error the board does not need to name, so this quietly finds
    nothing for it — the block counts still tell the true story."""
    latest = db.scalars(
        select(GenerationJob)
        .where(GenerationJob.kind == "curriculum_draft",
               GenerationJob.result_root_id == root_id)
        .order_by(GenerationJob.created_at.desc())
    ).first()
    return latest.error if latest is not None and latest.status == "failed" else None


@router.get("/curricula/{root_id}/chat-session", response_model=ChatSessionCreated)
def get_or_create_curriculum_chat_session(
    root_id: UUID, locale: str = Depends(locale_dep), db: Session = Depends(get_db)
) -> ChatSessionCreated:
    """GET-or-create the ONE chat session bound to this curriculum — the
    "Revise with AI" drawer's own conversation (chat overhaul persistence
    fix). The drawer used to call `POST /chat` on every open: fine for the
    first open, but a page reload resets the drawer's own React state, so it
    span a BRAND-NEW session every time — orphaning whatever conversation was
    already under way (still sitting, intact, in the database — just
    unreachable from the drawer again). Keying off `ChatSession.root_id`
    (Unit D, Task D2a's nullable column, unchanged by this endpoint) instead
    of a client-held id fixes that: the drawer calls this once per open, and
    a reload finds the SAME row.

    Most-recently-created wins when more than one session is bound to this
    root: the tutor's own "Clear chat" affordance starts a NEW session for
    the same `root_id` (plain `POST /chat`, unchanged) rather than deleting
    the old one — an intentional new conversation, not a bug — so the next
    open of this endpoint must resume THAT one, not an earlier orphan.
    `locale` comes from `X-App-Locale` like every other route with no body
    to carry it (`app.i18n.locale_dep`) — only used for a session this call
    itself creates; an existing session keeps whatever locale it was
    actually started in (`routers/chat.py`'s own documented rationale).
    """
    course = _get_block_or_404(db, root_id)
    if course.kind != "course":
        raise HTTPException(status_code=404, detail="not a curriculum root")

    session = db.scalars(
        select(ChatSession)
        .where(ChatSession.root_id == root_id)
        .order_by(ChatSession.created_at.desc())
    ).first()
    if session is None:
        session = ChatSession(root_id=root_id, locale=locale)
        db.add(session)
        db.commit()
    return ChatSessionCreated(session_id=session.id)


@router.get("/curricula/interview/{interview_id}/chat-session", response_model=ChatSessionCreated)
def get_or_create_interview_chat_session(
    interview_id: UUID, locale: str = Depends(locale_dep), db: Session = Depends(get_db)
) -> ChatSessionCreated:
    """GET-or-create the planning chat's session (Part 5) — one per
    interview, exactly the shape of `get_or_create_curriculum_chat_session`
    above, just keyed off `ChatSession.interview_id` instead of `root_id`:
    there is no course `Block` yet for this to bind to (the interview hasn't
    reached "confirm"), so none of `routers/chat.py`'s root_id-gated revise
    behaviors apply to a session created here.

    Most-recently-created wins when more than one session is bound to this
    interview, same reasoning as the curriculum route: a fresh `POST /chat`
    "Clear chat" starts a new one rather than deleting the old, and this
    endpoint must resume the newest. `locale` is `X-App-Locale` via
    `app.i18n.locale_dep`, same as the curriculum route — only used for a
    session this call itself creates.

    Route ordering: this path's literal `interview` segment is tried before
    `/curricula/{root_id}/chat-session` only matters if FastAPI walked routes
    in a conflicting order, but `root_id: UUID` on that route rejects the
    literal string "interview" anyway, so registration order here is
    unconstrained.
    """
    interview = _get_interview_or_404(db, interview_id)

    session = db.scalars(
        select(ChatSession)
        .where(ChatSession.interview_id == interview.id)
        .order_by(ChatSession.created_at.desc())
    ).first()
    if session is None:
        session = ChatSession(interview_id=interview.id, locale=locale)
        db.add(session)
        db.commit()
    return ChatSessionCreated(session_id=session.id)


@router.post(
    "/curricula/interview/{interview_id}/distill",
    response_model=PlanningBriefOut,
    dependencies=[Depends(require_llm_configured)],
)
def distill_interview_planning_brief(interview_id: UUID, db: Session = Depends(get_db)) -> dict:
    """Distill the planning chat into an EDITABLE brief. Deliberately does
    NOT store: the tutor reviews/edits first, then PUT planning-brief saves
    the approved text — approve-before-spend, the input-side mirror of the
    revise engine's approve-before-apply."""
    interview = _get_interview_or_404(db, interview_id)
    try:
        brief = interview_service.distill_planning_brief(db, interview)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"brief": brief}


@router.put("/curricula/interview/{interview_id}/planning-brief", status_code=204, response_model=None)
def put_interview_planning_brief(
    interview_id: UUID, payload: PlanningBriefIn, db: Session = Depends(get_db)
) -> None:
    interview = _get_interview_or_404(db, interview_id)
    interview.planning_brief = payload.brief.strip() or None
    db.commit()


@router.post("/curricula/{root_id}/draft", response_model=JobAccepted, status_code=202,
             dependencies=[Depends(require_llm_configured)])
def resume_curriculum_draft(
    root_id: UUID, background_tasks: BackgroundTasks, db: Session = Depends(get_db),
) -> JobAccepted:
    """RESUME: draft every lesson still `queued` under this curriculum.

    This is a REQUEST, and that is the entire architecture of the recovery story. A
    `BackgroundTask` can only be scheduled by a request — there is no worker
    process (the compose `worker` service is a stub that sleeps) and this stage
    deliberately did not add one. So: `jobs/sweep.py` puts interrupted `drafting`
    lessons back to `queued` at boot, nothing is auto-enqueued, and the tutor
    presses a button. It also covers the 429 case (a rate-limited lesson is
    `queued`, not `failed`), a lesson he added to the outline after the fact, and a
    failed lesson he wants retried — all the same code path, because they are all
    the same state.
    """
    _get_block_or_404(db, root_id)
    job = GenerationJob(
        kind="curriculum_draft", status="pending", params={"root_id": str(root_id)},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(run_curriculum_draft_job, job.id)
    return JobAccepted(job_id=job.id, status=job.status)


@router.post("/curricula/{root_id}/redraft", response_model=JobAccepted, status_code=202,
             dependencies=[Depends(require_llm_configured)])
def redraft_curriculum(
    root_id: UUID, background_tasks: BackgroundTasks, db: Session = Depends(get_db),
) -> JobAccepted:
    """RE-DRAFT UNDER THE CURRENT STRUCTURE: the ONLY route that rewrites lessons
    that already drafted.

    A blueprint edit must NEVER re-draft on its own (spec invariant #8) — the
    blueprint routes (`routers/blueprint.py`) never call this or anything like
    it. This is the explicit, tutor-pressed opt-in: every non-gap lesson under
    the root, `ready` or `failed` or `queued` alike, goes back to `queued`, and
    the ordinary fan-out (`run_curriculum_draft_job`) is scheduled exactly as
    `resume_curriculum_draft`/`deepen_lesson` above already do.

    THIS DIFFERS FROM RESUME IN EXACTLY ONE WAY, AND IT IS THE WHOLE POINT.
    `POST .../draft` (Resume, above) only picks up lessons already `queued` or
    `failed` — a `ready` lesson is left alone, because Resume exists to FINISH a
    curriculum, not rewrite one. This route flips `ready` lessons back to
    `queued` too, because the tutor changed the lesson BLUEPRINT (added/removed
    a section, reweighted one) and wants every lesson rebuilt under it. No new
    blueprint plumbing is needed here: `run_curriculum_draft_job`'s Phase A
    already resolves `blueprint_from_course_meta(course.meta)` FRESH on every
    run, so simply requeueing and rescheduling the same job is sufficient — the
    course's frozen blueprint is what gets drafted from, whatever it is now.

    A GAP module has no lessons at materialize time — "nothing to draft, no call
    is made" (`outline.py`) — but a tutor can add one manually later
    (`edit.add_lesson` does not check the module's tier). Such a lesson is
    excluded here: nothing under a gap module was ever meant to be drafted, and
    a redraft must not start drafting it for the first time as a side effect.
    """
    course = _get_block_or_404(db, root_id)
    if course.kind != "course":
        raise HTTPException(status_code=404, detail="not a curriculum root")

    modules = db.scalars(
        select(Block).where(Block.parent_id == course.id, Block.kind == "module")
    ).all()
    for module in modules:
        if (module.meta or {}).get("tier") == TIER_GAP:
            continue
        lessons = db.scalars(
            select(Block).where(Block.parent_id == module.id, Block.kind == "lesson")
        ).all()
        for lesson in lessons:
            # Whole-dict reassignment (invariant #9) — `Block.meta` is plain
            # `sa.JSON`, no `MutableDict`; `lesson.meta["k"] = v` would not
            # persist at all.
            lesson.meta = {**(lesson.meta or {}), "draft_status": "queued", "error": None}
    db.commit()

    job = GenerationJob(
        kind="curriculum_draft", status="pending", params={"root_id": str(root_id)},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(run_curriculum_draft_job, job.id)
    return JobAccepted(job_id=job.id, status=job.status)


@router.post("/curricula/{root_id}/modules", response_model=BlockTreeOut, status_code=201)
def add_curriculum_module(
    root_id: UUID, payload: ModuleCreate, db: Session = Depends(get_db),
) -> dict:
    try:
        module = edit_service.add_module(
            db, root_id, title=payload.title, objective=payload.objective,
            tier=payload.tier, after=payload.after,
        )
    except edit_service.EditError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return block_to_tree(module)


@router.post("/curricula/{root_id}/modules/generate", response_model=JobAccepted,
             status_code=202, dependencies=[Depends(require_llm_configured)])
def generate_curriculum_module(
    root_id: UUID, payload: ModuleGenerateRequest,
    background_tasks: BackgroundTasks, db: Session = Depends(get_db),
) -> JobAccepted:
    """AI ADD-MODULE: plan one module that fits this course (optionally about
    `topic`), persist it with its lessons `queued`, then chain the ordinary draft
    fan-out. 202 + a job id — the planning call alone runs 20-60s over the full
    library, which is exactly the timeout class the job table exists for.
    """
    course = _get_block_or_404(db, root_id)
    if course.kind != "course":
        raise HTTPException(status_code=404, detail="not a curriculum root")
    job = GenerationJob(
        kind="module_generate", status="pending",
        params={"root_id": str(root_id), "topic": payload.topic},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(run_module_generate_job, job.id)
    return JobAccepted(job_id=job.id, status=job.status)


@router.post("/curricula/{root_id}/revise", response_model=JobAccepted, status_code=202,
             dependencies=[Depends(require_llm_configured)])
def revise_curriculum(
    root_id: UUID, payload: ReviseRequest,
    background_tasks: BackgroundTasks, db: Session = Depends(get_db),
) -> JobAccepted:
    """PLAN or APPLY a curriculum revision — 202 + a `curriculum_revise` job.

    `mode="plan"` (default): the job runs the read-only planner and stores the
    plan on `job.progress["plan"]` (poll `GET /jobs/{id}`) — it MUTATES NOTHING.
    `mode="apply"`: the job applies the approved `plan` in one transaction and
    chains the ordinary draft fan-out over the new/changed lessons. The planner
    reads the whole library (20-60s) — exactly the timeout class the job table
    exists for.

    APPROVED == APPLIED, EXACT (controller, 2026-07-18): the plan is VALIDATED
    HERE, the moment apply is invoked, and the VALIDATED plan is what gets stored
    on the job params and applied verbatim — an op an id-validation would drop
    never reaches apply. `apply_revision` re-validates as defence-in-depth, a
    no-op now that the stored plan is already clean.
    """
    course = _get_block_or_404(db, root_id)
    if course.kind != "course":
        raise HTTPException(status_code=404, detail="not a curriculum root")
    if payload.mode == "apply" and not payload.plan:
        raise HTTPException(status_code=422, detail="apply requires a plan")

    params = {"root_id": str(root_id), "instruction": payload.instruction}
    if payload.mode == "apply":
        params["plan"] = validate_ops(db, root_id, payload.plan)
    job = GenerationJob(kind="curriculum_revise", status="pending", params=params)
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(run_curriculum_revise_job, job.id)
    return JobAccepted(job_id=job.id, status=job.status)


@router.post("/blocks/{module_id}/lessons", response_model=BlockTreeOut, status_code=201)
def add_module_lesson(
    module_id: UUID, payload: LessonCreate, db: Session = Depends(get_db),
) -> dict:
    """A new lesson, `queued`. The next Resume drafts it with the same cached
    library prefix as the rest — which is what makes "I want one more lesson on
    barre chords" a two-click operation rather than a regeneration."""
    try:
        lesson = edit_service.add_lesson(
            db, module_id, title=payload.title, objective=payload.objective,
            after=payload.after,
        )
    except edit_service.EditError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return block_to_tree(lesson)


@router.post("/blocks/{module_id}/lessons/from-chat", response_model=BlockTreeOut,
             status_code=201)
def add_lesson_from_chat_endpoint(
    module_id: UUID, payload: LessonFromChat, db: Session = Depends(get_db),
) -> dict:
    """THE CHAT→CURRICULUM BRIDGE: the answer the tutor is reading in chat lands
    as a real lesson under the module he picked. No LLM call — the approved
    content is stored verbatim, with the chat turn's citations mapped to the
    board's provenance chips. Deepen is the later "now write it out properly"
    upgrade path."""
    try:
        lesson = from_chat_service.add_lesson_from_chat(
            db, module_id,
            title=payload.title, content=payload.content,
            citations=payload.citations, chat_session_id=payload.chat_session_id,
        )
    except from_chat_service.FromChatError as e:
        raise HTTPException(
            status_code=404 if "not found" in str(e) else 422, detail=str(e),
        ) from e
    return block_to_tree(lesson)


@router.post("/blocks/{lesson_id}/deepen", response_model=JobAccepted, status_code=202,
             dependencies=[Depends(require_llm_configured)])
def deepen_lesson(
    lesson_id: UUID, background_tasks: BackgroundTasks, db: Session = Depends(get_db),
) -> JobAccepted:
    """DEEPEN: "this one is thin — write it again, longer."

    Deliberately NOT a second drafting pipeline. It puts the lesson back to `queued`
    with a `deepen` flag and schedules the ordinary draft fan-out over its
    curriculum — the same job, the same cached library prefix, the same per-lesson
    failure isolation. The only thing the flag changes is this lesson's word target
    (`jobs/curriculum_draft.DEEPEN_TARGET_RATIO`).

    So: one route, no new job kind, and a lesson that is already `queued` because it
    has never been drafted at all behaves identically — which is exactly what the
    tutor means when he presses the button on it.
    """
    try:
        root_id = edit_service.requeue_lesson(db, lesson_id, deepen=True)
    except edit_service.EditError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    job = GenerationJob(
        kind="curriculum_draft", status="pending", params={"root_id": str(root_id)},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(run_curriculum_draft_job, job.id)
    return JobAccepted(job_id=job.id, status=job.status)


@router.post("/blocks/{block_id}/reorder", response_model=BlockTreeOut)
def reorder_curriculum_block(
    block_id: UUID, payload: ReorderRequest, db: Session = Depends(get_db),
) -> dict:
    try:
        block = edit_service.reorder_block(db, block_id, direction=payload.direction)
    except edit_service.EditError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return block_to_tree(block)


@router.post("/blocks/{block_id}/refine", response_model=BlockTreeOut,
             dependencies=[Depends(require_llm_configured)])
def refine_curriculum_block(
    block_id: UUID, payload: RefineRequest, db: Session = Depends(get_db),
) -> dict:
    """EXTEND WITH CHAT. *"change this and give more detail about the Amp"* — and
    it actually follows the instruction.

    Synchronous, unlike everything else that calls the model here: this is ONE
    block, a few thousand tokens, and the tutor is sitting there watching. A job
    row and a poll for a 10-second call would be infrastructure for its own sake.

    The old body is stashed on `meta.prev_body` and `POST .../undo` puts it back.
    """
    block = _get_block_or_404(db, block_id)
    refine_block(db, block, payload.instruction)
    db.commit()
    return block_to_tree(block, _artifacts_for(db, block))


@router.post("/blocks/{block_id}/undo", response_model=BlockTreeOut)
def undo_block_refine(block_id: UUID, db: Session = Depends(get_db)) -> dict:
    block = _get_block_or_404(db, block_id)
    if not undo_refine(block):
        raise HTTPException(status_code=422, detail="nothing to undo on this block")
    db.commit()
    return block_to_tree(block, _artifacts_for(db, block))


@router.get("/blocks/{block_id}", response_model=BlockTreeOut)
def get_block(block_id: UUID, db: Session = Depends(get_db)) -> dict:
    block = _get_block_or_404(db, block_id)
    return block_to_tree(block, _artifacts_for(db, block))


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
    # `est_minutes: 0` means CLEAR. The drop-nulls rule above is what protects
    # NOT NULL columns, but it also made "remove this session's time estimate"
    # unexpressible — the frontend's clear silently no-op'd. Zero is not a
    # meaningful duration, so it is the sentinel: it lands as NULL.
    if updates.get("est_minutes") == 0:
        updates.pop("est_minutes")
        block.est_minutes = None
    for field, value in updates.items():
        setattr(block, field, value)
    db.commit()
    return block_to_tree(block)


@router.delete("/blocks/{block_id}", status_code=204, response_model=None)
def delete_block(block_id: UUID, db: Session = Depends(get_db)) -> None:
    """Delete a block and its subtree — and CLOSE THE HOLE IT LEAVES IN `order`.

    The delete itself was always fine (ORM `cascade="all, delete-orphan"` plus the
    DB's own `ON DELETE CASCADE`). What was missing was the renormalisation: this
    left siblings at [0, 1, 3], which sorted correctly and looked harmless — right
    up until Stage 6 made it possible to ADD a module, whose new `order` of
    `len(siblings)` = 3 then collided with the survivor already sitting at 3. The
    tie is broken by whatever Postgres feels like, so the tutor's new module lands
    somewhere in the middle of his course.
    """
    _get_block_or_404(db, block_id)
    edit_service.delete_block(db, block_id)


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
