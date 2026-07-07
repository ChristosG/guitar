import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PkMixin, TimestampMixin


class Assignment(Base, PkMixin, TimestampMixin):
    """A template curriculum Block handed to a specific Student."""

    __tablename__ = "assignment"
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True)
    curriculum_block_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("block.id", ondelete="CASCADE"), index=True)


class Progress(Base, PkMixin, TimestampMixin):
    """A Student's mastery status against a single curriculum Block."""

    __tablename__ = "progress"
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True)
    block_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("block.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="not_started")   # soft, relabelable
    # not_started | introduced | practicing | mastered
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class LessonLog(Base, PkMixin, TimestampMixin):
    """A record of what actually happened in a taught session."""

    __tablename__ = "lesson_log"
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True)
    session_block_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("block.id", ondelete="CASCADE"), index=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    taught: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    homework: Mapped[str | None] = mapped_column(Text, nullable=True)
