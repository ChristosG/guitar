from datetime import date
from sqlalchemy import Date, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base
from app.models.base import PkMixin, TimestampMixin

class Student(Base, PkMixin, TimestampMixin):
    __tablename__ = "student"
    name: Mapped[str] = mapped_column(String(200))
    birthdate: Mapped[date | None] = mapped_column(Date, nullable=True)
    level: Mapped[str | None] = mapped_column(String(50), nullable=True)
    instrument: Mapped[str | None] = mapped_column(String(50), nullable=True)
    preferred_language: Mapped[str] = mapped_column(String(5), default="el")
    status: Mapped[str] = mapped_column(String(20), default="active")
    # What this student actually wants ("play Wonderwall at his sister's
    # wedding"). Free text, in the tutor's words, and it reaches the LESSON DRAFT
    # prompt via `app.students.context.build_student_brief` — unlike `level`,
    # which until Stage 6 only ever reached the outline.
    goals: Mapped[str | None] = mapped_column(Text, nullable=True)
