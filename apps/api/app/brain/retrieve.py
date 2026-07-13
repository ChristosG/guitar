"""Retrieval: cross-lingual cosine search over Chunks, plus a grounded-answer builder.

Retrieval ordering — decision (do this, not an oversight): for this PoC, order
by **exact cosine distance on the full `vector(2560)` column**,
``Chunk.embedding.cosine_distance(qv)``, evaluated as a plain scan + sort. The
corpus here is small (a handful of sources, at most a few hundred chunks), so
an exact brute-force scan is fast *and* always returns the true nearest
neighbours — no ANN recall trade-off to reason about at this scale.

This deliberately does NOT use the Task 1 HNSW index (`ix_chunk_embedding_hnsw`,
built on the ``embedding::halfvec(2560)`` cast — see that migration's own
comment) — a plain ``cosine_distance`` on the full ``vector`` column does not
match that index's expression, so Postgres cannot use it (confirmed via
EXPLAIN during Task 1; see progress.md's "CRITICAL carry-forward" note). That
index is forward-provisioning for when the corpus grows large enough that an
exact scan becomes the bottleneck; when that day comes, switch the ORDER BY to
raw SQL matching the index's own expression exactly:
``ORDER BY embedding::halfvec(2560) <=> :q::halfvec(2560)``.

Cross-lingual: both documents (embedded at ingest time with ``is_query=False``)
and queries (embedded here with ``is_query=True``, which applies Qwen's
asymmetric retrieval instruction prefix — see ``app.llm.embeddings.
query_instruct``) live in the same embedding space regardless of source
language, so a query in one language can retrieve chunks written in another.
``answer()`` then generates its grounded response in the *requested* locale
regardless of which language the retrieved chunks are written in.
"""
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select

from app.llm.factory import get_provider
from app.models.knowledge import Chunk, KnowledgeSource, Page


@dataclass
class Hit:
    chunk_id: UUID
    source_id: UUID
    source_title: str
    text: str
    section_path: str | None
    page: int | None
    score: float  # 1 - cosine_distance (1.0 == identical direction)
    # Page.id (FK), resolved below by joining Chunk.page_id -> Page. Lets a
    # caller fetch the Page row (and therefore its scan, Page.image_path) to
    # make a citation VERIFIABLE rather than merely claimed — this is the
    # wiring the T1/T5 docstrings deferred to "later Plan 9 work"; it lands
    # here in T10 because the acceptance criterion (a grounded answer citing
    # a real, showable page) cannot be proven without it. Defaults to None so
    # existing call sites that construct a bare Hit (e.g.
    # test_build_grounded_messages_...) keep working unchanged.
    page_id: UUID | None = None


def search(
    db, query: str, *, k: int = 8, domain: str | None = None, language: str | None = None
) -> list[Hit]:
    """Top-k Chunks by cosine similarity to `query`, joined to their source.

    `domain`/`language`, when given, filter on the joined KnowledgeSource
    (e.g. only "tone"-domain sources, or only Greek-language sources) — the
    query itself is still embedded and matched exactly the same way.

    `domain` semantics (CRITICAL bug fix, Plan 12): `KnowledgeSource.domain`
    is a free-text, essentially UNPOPULATED field in practice — measured on
    the real deployed library, 12 of 16 real sources (95% of the real
    library's characters, including the tutor's entire 77-page book) have
    `domain IS NULL`. A plain `domain == X` filter treats NULL as "not X",
    which is wrong: NULL means UNCLASSIFIED, not "confirmed to be a
    different domain". Filtering it out silently threw away the tutor's
    entire real library whenever a curriculum interview ran with
    `domain="tone"`, leaving nothing but the tiny synthetic seed-filler and
    marking every module a false gap ("your library doesn't cover this")
    when it demonstrably did.

    So `domain=X` here matches `KnowledgeSource.domain == X OR
    KnowledgeSource.domain IS NULL` — a domain acts as a *preference for
    same-domain material while never excluding unclassified material*, not
    as an exclusion filter over everything not explicitly tagged. It still
    excludes a source EXPLICITLY tagged a *different* domain (e.g.
    domain="theory" when filtering domain="tone") — the filter isn't a
    no-op, it just no longer punishes sources nobody got around to tagging.
    """
    qv = get_provider().embed([query], is_query=True)[0]
    distance = Chunk.embedding.cosine_distance(qv)

    # outerjoin, not join: chunks from sources ingested before Plan 9 Task 1
    # (or any future ingest path that legitimately has no Page) still have
    # page_id=None — those must still be searchable, just with page=None.
    stmt = (
        select(Chunk, KnowledgeSource, Page, distance.label("distance"))
        .join(KnowledgeSource, Chunk.source_id == KnowledgeSource.id)
        .outerjoin(Page, Chunk.page_id == Page.id)
        .order_by(distance)
        .limit(k)
    )
    if domain is not None:
        stmt = stmt.where(
            or_(KnowledgeSource.domain == domain, KnowledgeSource.domain.is_(None))
        )
    if language is not None:
        stmt = stmt.where(KnowledgeSource.language == language)

    rows = db.execute(stmt).all()
    return [
        Hit(
            chunk_id=chunk.id,
            source_id=chunk.source_id,
            source_title=source.title,
            text=chunk.text,
            section_path=chunk.section_path,
            page=page.page_no if page is not None else None,
            page_id=chunk.page_id,
            score=1.0 - dist,
        )
        for chunk, source, page, dist in rows
    ]


def build_grounded_messages(query: str, hits: list[Hit], *, locale: str) -> list[dict]:
    """Pure function: the {system,user} chat messages for a grounded answer.

    Kept separate from `answer()` (which also calls the live model) so the
    prompt shape itself — the locale instruction, the citation instruction,
    the numbered context block — is unit-testable without a model or a DB.
    """
    system = (
        f"Answer strictly from the provided context. Answer in {locale} "
        "(el=Greek, en=English). Cite sources as [n]. If the context does not "
        f"contain the answer, say so, in {locale}."
    )
    context = "\n\n".join(f"[{i}] {hit.text}" for i, hit in enumerate(hits, start=1))
    user = f"{query}\n\nContext:\n{context}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


@dataclass
class Answer:
    text: str
    citations: list[Hit]


def answer(db, query: str, *, locale: str, k: int = 8) -> Answer:
    hits = search(db, query, k=k)
    messages = build_grounded_messages(query, hits, locale=locale)
    text = get_provider().chat(messages, enable_thinking=False)
    return Answer(text=text, citations=hits)
