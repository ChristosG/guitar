import uuid
from datetime import date
from sqlalchemy import String, Date
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
