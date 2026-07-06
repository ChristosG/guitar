import uuid
from sqlalchemy import String, Integer, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from app.db import Base
from app.config import settings
from app.models.base import PkMixin, TimestampMixin

class KnowledgeSource(Base, PkMixin, TimestampMixin):
    __tablename__ = "knowledge_source"
    type: Mapped[str] = mapped_column(String(20))          # pdf/url/text/note/image
    title: Mapped[str] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(20), default="ingesting")
    language: Mapped[str | None] = mapped_column(String(5), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(30), nullable=True)   # tone/beginner/theory
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

class Chunk(Base, PkMixin, TimestampMixin):
    __tablename__ = "chunk"
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    section_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embed_dim))
