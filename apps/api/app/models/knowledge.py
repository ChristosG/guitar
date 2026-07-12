import uuid
from sqlalchemy import String, Integer, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from app.db import Base
from app.config import settings
from app.models.base import PkMixin, TimestampMixin

PAGE_STATUSES = {"pending", "ocr_running", "ready", "failed", "empty"}


class KnowledgeSource(Base, PkMixin, TimestampMixin):
    __tablename__ = "knowledge_source"
    type: Mapped[str] = mapped_column(String(20))          # pdf/url/text/note/image
    title: Mapped[str] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(20), default="ingesting")
    language: Mapped[str | None] = mapped_column(String(5), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(30), nullable=True)   # tone/beginner/theory
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Controller decision (Plan 9 Task 1): persist the original URL for
    # kind="url" sources. A later task implements "retry a failed ingest",
    # which for a URL source must re-fetch this same URL — without this
    # column the row has no durable record of where it came from.
    url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    collection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("collection.id", ondelete="SET NULL"), nullable=True, index=True)


class Collection(Base, PkMixin, TimestampMixin):
    """A one-level folder. A source lives in exactly one, or none ("Unfiled").

    Deliberately NOT a tree and NOT tags (spec D7): a filing cabinet is the
    right mental model for a non-technical tutor, and "which tags do I use?"
    is exactly the open-ended box that makes the app feel like a chore.
    """
    __tablename__ = "collection"
    name: Mapped[str] = mapped_column(String(120), unique=True)


class Page(Base, PkMixin, TimestampMixin):
    """One physical page of a source.

    Exists so a citation is VERIFIABLE, not merely claimed: a Chunk knows its
    Page, and a Page carries the scan it came from — so "p.47" can always be
    SHOWN. Also the unit of OCR retry and of re-embedding.

    Per spec D2, non-paginated sources (url/text/note) get exactly ONE Page row
    with image_path=NULL. Paying one cheap redundant row here deletes a
    `source.type` branch from the reader, the chunker, the citation renderer,
    and the retry logic.
    """
    __tablename__ = "page"
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source.id", ondelete="CASCADE"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)          # 1-based, as printed
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    ocr_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Chunk(Base, PkMixin, TimestampMixin):
    __tablename__ = "chunk"
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    section_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("page.id", ondelete="CASCADE"), nullable=True, index=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embed_dim))
