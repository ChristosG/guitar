"""Pydantic response model for the async generation-job poll endpoint
(`routers/jobs.py`).

Kept separate from the SQLAlchemy `app.models.generation_job` model per this
codebase's established split (mirrors `schemas/artifacts.py` vs
`app.models.artifact`) — this module is only the HTTP boundary's shape.
"""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class JobOut(BaseModel):
    """`from_attributes=True` so it validates directly off the ORM
    `GenerationJob` object, mirroring `schemas/artifacts.py`'s `ArtifactOut`.
    """
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    status: str
    result_root_id: UUID | None = None
    error: str | None = None
    error_kind: str | None = None
    created_at: datetime
    updated_at: datetime
