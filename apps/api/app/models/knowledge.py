import uuid
from sqlalchemy import String, Integer, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from app.db import Base
from app.models.base import PkMixin, TimestampMixin

PAGE_STATUSES = {"pending", "ocr_running", "ready", "failed", "empty"}

# `KnowledgeSource.status`. "partial" is the fifth value (Plan 13, Stage 7.2) and
# it exists because a 74/77 book used to roll up to a green "Ready": the rollup
# rule was `ready iff char_count > 0`, so three unreadable pages vanished behind a
# checkmark with no retry path, forever — while `job.error` already knew and said
# "3 page(s) unreadable". Anything comparing against "ready" to mean "usable"
# must now accept "partial" too (`SOURCE_USABLE_STATUSES`): a partial book is
# fully readable and fully citable — it is just honest about the pages it lost.
SOURCE_STATUSES = {"ingesting", "ready", "partial", "empty", "failed"}
SOURCE_USABLE_STATUSES = frozenset({"ready", "partial"})

# multilingual-e5-small's width. HARDCODED — deliberately NOT `settings.embed_dim`.
#
# A column width is not a setting. It is a fact about the bytes on disk, and the
# only thing that can change it is an ALTER TABLE. When the model read the width
# from config, a one-line env change (EMBED_DIM=1024) silently redefined the ORM's
# idea of a column that still physically held 2560-dim vectors, and every insert
# then failed at the DATABASE with a dimension mismatch — the furthest possible
# point from the mistake. Changing this constant is a migration, and it now looks
# like one.
EMBED_DIM = 384


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
    # Which embedding model produced this source's chunk vectors. NULL means
    # "ingested before anyone tracked this" — i.e. a Qwen-era 2560-dim row, which
    # after Plan 13 Stage 4 cannot physically exist in a vector(384) column, so a
    # NULL here now only ever means "re-embedded by an early `scripts/reembed.py`
    # run that predated the stamp". The column exists for the NEXT model swap: it
    # is the only way to tell, without re-embedding everything, which rows still
    # hold vectors from a foreign embedding space (whose cosine distances against
    # a new query vector are not wrong so much as MEANINGLESS — two unrelated
    # models' spaces have no shared geometry, and nothing in the result set looks
    # any different).
    embed_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # SHA-256 of the uploaded bytes (pdf uploads only; NULL for url/text rows
    # and for anything uploaded before the column existed). The duplicate-upload
    # guard's key: two ids for the same book manufacture a false "divergence"
    # in the canon, because consensus is keyed on distinct source ids.
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


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

    (source_id, page_no) is unique: paginate_source replaces a source's Pages
    on every call (re-paginate is idempotent, not additive — see
    app/brain/paginate.py), and this constraint enforces that invariant at
    the DB level too, not just in application code.
    """
    __tablename__ = "page"
    __table_args__ = (
        UniqueConstraint("source_id", "page_no", name="uq_page_source_page_no"),
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source.id", ondelete="CASCADE"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)          # 1-based, as printed
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    ocr_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How many times `ocr_source` has PICKED THIS PAGE UP (not how many vision()
    # calls it made — each pickup gets its own in-run retry). The cap it feeds
    # (`ocr.MAX_PAGE_ATTEMPTS`) is what makes it safe to re-pick-up `empty` pages
    # at all: before Stage 7.2 an `empty` page was never retried BY ANY PATH, so
    # one transient model hiccup returning "" made a page permanently dead — but
    # retrying it unconditionally would re-bill a genuinely blank page on every
    # single retry of the book, forever. Three attempts, then it rests.
    ocr_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # WHERE this page's text came from. NULL for rows written before this
    # column existed (an additive migration must not invent history).
    #
    #   "text_layer"        — a real embedded font: publisher text, taken free.
    #   "qwen"              — the local VL model transcribed the scan.
    #   "claude"            — Claude transcribed the scan (subscription or API).
    #   "text_layer+claude" — BOTH, and the page says which is which: publisher
    #                         text, plus a model's description of the picture on
    #                         it inside a [FIGURE]…[/FIGURE] region (an
    #                         `image_region` page — see `brain/ocr.py`'s contract).
    #                         "claude" alone would claim a transcription that
    #                         never happened; "text_layer" alone would hide that a
    #                         model put content on the page at all.
    #   "unknown"           — a page was read but the provider name could not be
    #                         resolved (`_current_text_source` refuses to fail a
    #                         12-hour run over provenance). Honest, not a guess.
    #   "failed"            — every attempt failed; `ocr_error` says why.
    #
    # Any other value is a provider this app has not heard of, recorded verbatim
    # and clamped to String(20) — `ocr._TEXT_SOURCE_BY_PROVIDER` maps what it
    # knows and passes the rest through rather than guessing. Nothing parses this.
    #
    # Shown per page in the Reader. Without it, `brain/paginate.py`'s routing is
    # a silent behaviour change the tutor cannot inspect or overrule.
    text_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # WHY this page was sent to vision — the detector's reason, in its own words,
    # so a wrong call is diagnosable rather than merely wrong.
    #   "no_text_layer" | "inherited_ocr" | "image_region" | "ocr_garbage"
    ocr_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)


class Chunk(Base, PkMixin, TimestampMixin):
    __tablename__ = "chunk"
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    section_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("page.id", ondelete="CASCADE"), nullable=True, index=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
