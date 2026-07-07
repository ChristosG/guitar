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
    children = relationship("Block", cascade="all, delete-orphan")
