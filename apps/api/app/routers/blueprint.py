"""`/blueprint/default` — read, edit, and reset the SETTINGS-default lesson
blueprint: the 8-section skeleton every NEW curriculum is seeded with.

WHAT THIS ROUTER TOUCHES, AND WHAT IT DELIBERATELY DOES NOT. It reads and writes the
`blueprint_default` singleton (via `blueprint_store`) — the default handed to FUTURE
courses. It never re-drafts and never touches an existing course: a course freezes
its blueprint onto `meta["blueprint"]` at materialize time and drafts from that copy,
so editing this default changes only what comes next (spec invariant #3). The opt-in
"re-draft under the current structure" button is a SEPARATE route (Task 8), never a
side effect of a save here.

`is_override` is the whole reason GET returns more than the blueprint: the Settings UI
shows "you've customised this" and offers Restore only when a row exists, and diffs the
tutor's default against `/blueprint/code-default` (always the git-backed code default,
which Restore returns to — there is no history table, Reset = delete the row).

EVERY FAILURE IS A `code`, following `routers/prompts.py`. A `BlueprintInvalid`
becomes `422 {"detail": {"code": ...}}`, exactly like `put_slice` — the tutor is a
computer beginner and never sees JSON or a status code, only one Greek sentence the
web looks up under `blueprint.errors.<code>`. NO EXTRA AUTH: the whole cockpit sits
behind one session gate, and a second rule on this router would be a second thing to
get wrong on a single-user app (same note as `routers/prompts.py`).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.curriculum import blueprint_store as store
from app.curriculum.blueprint import BlueprintInvalid, default_blueprint, validate_blueprint
from app.db import get_db

router = APIRouter(prefix="/blueprint", tags=["blueprint"])


class BlueprintIn(BaseModel):
    blueprint: dict


class BlueprintOut(BaseModel):
    blueprint: dict
    is_override: bool


class CodeDefaultOut(BaseModel):
    blueprint: dict


@router.get("/default", response_model=BlueprintOut)
def get_default(db: Session = Depends(get_db)) -> BlueprintOut:
    """The RESOLVED settings default (the tutor's edit if he has one, else the code
    default) plus whether it is a customisation."""
    return BlueprintOut(
        blueprint=store.resolve_default_blueprint(db),
        is_override=store.has_default_override(db),
    )


@router.get("/code-default", response_model=CodeDefaultOut)
def get_code_default() -> CodeDefaultOut:
    """ALWAYS the git-backed code default — the Restore target and the diff baseline.
    No DB read: this is a fact about the code, not about this install."""
    return CodeDefaultOut(blueprint=default_blueprint())


@router.put("/default", response_model=BlueprintOut)
def put_default(payload: BlueprintIn, db: Session = Depends(get_db)) -> BlueprintOut:
    """Validate + save the tutor's settings default. Validation runs BEFORE the write
    (all-or-nothing), so a rejected blueprint leaves any existing default untouched —
    he does not lose a good structure by saving a bad one over it."""
    try:
        validate_blueprint(payload.blueprint)
    except BlueprintInvalid as e:
        raise HTTPException(status_code=422, detail=e.detail) from e

    store.save_default_blueprint(db, payload.blueprint)
    return BlueprintOut(
        blueprint=store.resolve_default_blueprint(db),
        is_override=True,
    )


@router.delete("/default", response_model=BlueprintOut)
def delete_default(db: Session = Depends(get_db)) -> BlueprintOut:
    """Reset to the code default by deleting the row. Idempotent: resetting an
    already-default settings is a 200, not a 404 — the wish is granted."""
    store.reset_default_blueprint(db)
    return BlueprintOut(
        blueprint=store.resolve_default_blueprint(db),
        is_override=False,
    )
