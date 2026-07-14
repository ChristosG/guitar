import uuid
from sqlalchemy import JSON, String, Integer, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base
from app.models.base import PkMixin, TimestampMixin

class Block(Base, PkMixin, TimestampMixin):
    __tablename__ = "block"
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("block.id", ondelete="CASCADE"), nullable=True, index=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(30), default="topic")   # soft, relabelable
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    est_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str] = mapped_column(String(5), default="el")
    is_template: Mapped[bool] = mapped_column(default=False)
    # Curriculum plane (Plan 3): target_profile describes who a *template* block
    # is aimed at (e.g. {"level": "beginner", "age": 10}); student_id is set
    # when a block is (or descends from) a student-specific assigned instance,
    # nullable otherwise; plane distinguishes curriculum content blocks from
    # session/delivery blocks (e.g. a LessonLog's session_block_id target).
    target_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    student_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), nullable=True, index=True)
    plane: Mapped[str] = mapped_column(String(10), default="content")
    # Everything ABOUT this block that isn't the block: provenance/citations, the
    # grounding tier, the draft lifecycle (`draft_status`), the measured word
    # count, `prev_body` for Undo. It is called `meta` and NEVER `metadata` —
    # `metadata` is reserved on SQLAlchemy's Declarative base and the failure is
    # an import-time crash, not a mapping warning.
    #
    # target_profile used to carry all of this, which is why it meant two
    # unrelated things at once ("who is this template for" AND "where did this
    # content come from") and why `block_to_tree` serialized neither. Provenance
    # was data-migrated out of it in `9c1e4a5d7b30`; target_profile means one
    # thing again.
    #
    # PLAIN sa.JSON, NO MutableDict — so IN-PLACE MUTATION IS NOT PERSISTED.
    # `block.meta["draft_status"] = "ready"` appears to work in dev (the identity
    # map hands you the same dict back) and silently no-ops in production. Every
    # write must reassign the whole dict:
    #     block.meta = {**(block.meta or {}), "draft_status": "ready"}
    # The entire live-progress model rests on this.
    meta: Mapped[dict | None] = mapped_column("meta", JSON, nullable=True)
    children = relationship("Block", cascade="all, delete-orphan")
