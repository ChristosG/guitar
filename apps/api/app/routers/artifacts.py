"""`/artifacts` routes: create-from-spec, LLM generation, retrieval, and
delete.

Auth: every route here sits behind the `gt_session` password gate
(`app/auth/middleware.py`) — a whole-API ASGI middleware, not a per-router
dependency, so there is nothing to declare in this file. One tutor, one
password; there is still no authorization model, because there is nobody to
authorize against anybody else.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifacts.generate import TITLE_MAX_LEN, derive_title, generate_artifact
from app.artifacts.specs import validate_spec
from app.db import get_db
from app.i18n import locale_dep
from app.llm.errors import GuidedJSONError, LLMError
from app.models.artifact import Artifact
from app.models.block import Block
from app.schemas.artifacts import ArtifactCreate, ArtifactGenerateRequest, ArtifactOut

router = APIRouter(tags=["artifacts"])


def _to_artifact_out(artifact: Artifact) -> ArtifactOut:
    return ArtifactOut.model_validate(artifact, from_attributes=True)


def _get_block_or_404(db: Session, block_id: UUID) -> Block:
    # Same lookup routers/curriculum.py's own `_get_block_or_404` performs
    # (not imported from there — small deliberate duplication over a
    # cross-router import, mirroring that module's own precedent for
    # segment.py's module-private `_get_or_create_delivery_root`).
    block = db.get(Block, block_id)
    if block is None:
        raise HTTPException(status_code=404, detail="block not found")
    return block


@router.post("/artifacts", response_model=ArtifactOut)
def create_artifact(payload: ArtifactCreate, db: Session = Depends(get_db)) -> ArtifactOut:
    """Create-from-spec: validate `payload.spec` against `SPECS[payload.
    kind]` and persist as-is (`source="ai"` default — a hand-authored/
    uploaded spec posted here is not distinguished from an LLM one by this
    route; Task 5's upload path, if any, would set `source="uploaded"`
    explicitly). `block_id`, when given, is checked BEFORE spec validation
    — both are cheap/independent preconditions, so the order doesn't matter
    for cost, but checking existence first means an attach-to-missing-block
    request never has to explain a spec error it'll never see used.
    """
    if payload.block_id is not None:
        _get_block_or_404(db, payload.block_id)

    try:
        spec = validate_spec(payload.kind, payload.spec)
    except ValueError as e:
        # Unknown kind (plain ValueError) or a structurally invalid spec
        # (pydantic.ValidationError, itself a ValueError subclass — see
        # app.artifacts.specs's own docstring) both map to 422 here.
        raise HTTPException(status_code=422, detail=str(e)) from e

    if payload.title is None:
        title = derive_title(payload.kind, spec, None)
    elif payload.title == "":
        # Satisfies the NOT NULL constraint but is still a useless title —
        # mirrors routers.curriculum.update_block's identical empty-title guard.
        raise HTTPException(status_code=422, detail="title cannot be empty")
    else:
        # Clamp the same way derive_title clamps a generated title — a
        # client-supplied title can exceed Artifact.title's column cap
        # (String(300)) just as easily as an LLM-emitted spec name can, and
        # would otherwise reach Postgres uncaught (StringDataRightTruncation,
        # not a ValueError, so the except ValueError above never sees it).
        title = payload.title[:TITLE_MAX_LEN]

    artifact = Artifact(
        kind=payload.kind, spec=spec, title=title, tags=payload.tags, block_id=payload.block_id,
    )
    db.add(artifact)
    db.commit()
    return _to_artifact_out(artifact)


@router.post("/artifacts/generate", response_model=ArtifactOut)
def generate_artifact_endpoint(
    payload: ArtifactGenerateRequest,
    db: Session = Depends(get_db),
    locale: str = Depends(locale_dep),
) -> ArtifactOut:
    """Plain `def` (sync), not `async def` — same reasoning as `routers.
    curriculum.generate_curriculum_endpoint`: `generate_artifact` makes a
    blocking guided-JSON LLM call (49-179s/call per Plan 3 Task 2's report;
    same underlying `QwenVLLM.guided_json`), and FastAPI runs a `def` path
    operation in an external threadpool, so this doesn't block the event
    loop / other concurrent requests.

    `block_id`, when given, is checked BEFORE calling `generate_artifact` —
    unlike `create_artifact` above, here the ordering IS load-bearing: it
    would be wasteful to run a 49-179s LLM call only to then 404 on the
    attach target.

    Error mapping mirrors `generate_curriculum_endpoint` for the two error
    classes it also has (`GuidedJSONError` -> 502 "bad upstream response,
    retry"; a transport-level timeout/connection failure -> 504 "no
    response in time, retry") plus one this endpoint alone needs: a
    `ValueError` — either an unknown `kind`, or a `pydantic.ValidationError`
    (itself a `ValueError`) surviving `generate_artifact`'s one repair retry
    — maps to 422, since artifact generation can genuinely still fail
    structurally after that retry, unlike curriculum generation which has
    no repair step at all.
    """
    if payload.block_id is not None:
        _get_block_or_404(db, payload.block_id)

    try:
        artifact = generate_artifact(
            db, kind=payload.kind, prompt=payload.prompt,
            block_id=payload.block_id, ground=payload.ground,
            # The spec's prose (a gear card's `why`, a tone recipe's steps) is
            # written in the tutor's UI language; alphaTex/chord/gear names are
            # not translated — see `app.i18n.language_directive`.
            locale=locale,
        )
    except GuidedJSONError as e:
        raise HTTPException(
            status_code=502,
            detail="Artifact generation failed (model returned invalid/truncated output). Try again.",
        ) from e
    except LLMError as e:
        # `timeout`/`upstream`/`rate_limit` from the provider seam — the
        # openai/httpx transport tuple this replaced died with the vLLM era.
        raise HTTPException(
            status_code=504 if e.kind == "timeout" else 502,
            detail="Artifact generation failed upstream. Try again.",
        ) from e
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=f"could not generate a valid {payload.kind} spec: {e}",
        ) from e
    return _to_artifact_out(artifact)


@router.get("/artifacts", response_model=list[ArtifactOut])
def list_artifacts(
    block_id: UUID | None = None, kind: str | None = None, db: Session = Depends(get_db)
) -> list[ArtifactOut]:
    stmt = select(Artifact).order_by(Artifact.created_at.desc())
    if block_id is not None:
        stmt = stmt.where(Artifact.block_id == block_id)
    if kind is not None:
        stmt = stmt.where(Artifact.kind == kind)
    artifacts = db.scalars(stmt).all()
    return [_to_artifact_out(a) for a in artifacts]


@router.get("/artifacts/{artifact_id}", response_model=ArtifactOut)
def get_artifact(artifact_id: UUID, db: Session = Depends(get_db)) -> ArtifactOut:
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return _to_artifact_out(artifact)


@router.delete("/artifacts/{artifact_id}", status_code=204, response_model=None)
def delete_artifact(artifact_id: UUID, db: Session = Depends(get_db)) -> None:
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    db.delete(artifact)
    db.commit()
