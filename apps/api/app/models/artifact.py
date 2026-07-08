import uuid

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PkMixin, TimestampMixin


class Artifact(Base, PkMixin, TimestampMixin):
    """A generated-or-uploaded teaching visual: `{kind, spec}` (Plan 4).

    The LLM only ever emits `spec` (validated server-side per `kind` — see
    `app.artifacts.specs.validate_spec`); rendering from a validated spec is
    deterministic and lives client-side. `block_id` is nullable + `SET NULL`
    on delete (not `CASCADE`, unlike e.g. `Progress.block_id`): an artifact
    that loses its lesson attachment is still a valid standalone artifact
    (e.g. in a gallery), so deleting the Block it was attached to should
    detach it, not destroy it.
    """

    __tablename__ = "artifact"
    kind: Mapped[str] = mapped_column(String(30))
    spec: Mapped[dict] = mapped_column(JSON)
    title: Mapped[str] = mapped_column(String(300))
    # `default=list` (a callable), NOT `default=[]` — a literal mutable default
    # would be the same list object reused as the client-side default for
    # every row lacking an explicit value (the classic Python mutable-default
    # gotcha, here at the SQLAlchemy Column level rather than a function arg).
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(20), default="ai")  # "ai" | "uploaded"
    block_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("block.id", ondelete="SET NULL"), nullable=True, index=True)
