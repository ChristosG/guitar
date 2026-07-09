"""Pydantic request/response models for the Artifacts API
(`routers/artifacts.py`): create-from-spec, LLM generation, and CRUD.

Kept separate from the SQLAlchemy `app.models.artifact` model per this
codebase's established split (mirrors `schemas/curriculum.py` vs
`app.models.block`) — this module is only the HTTP boundary's shape; `kind`/
`spec` validation itself lives in `app.artifacts.specs.validate_spec`, not
here (a `dict` field accepts any JSON object at this layer).
"""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ArtifactCreate(BaseModel):
    """`POST /artifacts` body: create-from-spec. `spec` is validated against
    `SPECS[kind]` in the router (`app.artifacts.specs.validate_spec`), not
    here. `title` is optional — an omitted title is derived from the spec
    itself (e.g. a chord's `name`) by the router via `app.artifacts.
    generate.derive_title`, since `Artifact.title` is a NOT NULL column with
    no default.
    """
    kind: str
    spec: dict
    title: str | None = None
    tags: list[str] = []
    block_id: UUID | None = None


class ArtifactGenerateRequest(BaseModel):
    """`POST /artifacts/generate` body: `kind` + free-text `prompt` ->
    `generate_artifact`. `ground=True` retrieves Brain context (`app.brain.
    retrieve.search`) to ground the generation in real material — typically
    used for tone/gear kinds; see `generate_artifact`'s own docstring for
    why this isn't restricted to a kind allowlist.
    """
    kind: str
    prompt: str
    block_id: UUID | None = None
    ground: bool = False


class ArtifactOut(BaseModel):
    """`from_attributes=True` so it validates directly off the ORM
    `Artifact` object, mirroring `schemas/curriculum.py`'s `BlockTreeOut`/
    `schemas/knowledge.py`'s `SourceOut`.
    """
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    spec: dict
    title: str
    tags: list[str]
    source: str
    block_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
