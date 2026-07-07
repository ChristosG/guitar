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

from sqlalchemy import select

from app.llm.factory import get_provider
from app.models.knowledge import Chunk, KnowledgeSource


@dataclass
class Hit:
    chunk_id: UUID
    source_id: UUID
    source_title: str
    text: str
    section_path: str | None
    page: int | None
    score: float  # 1 - cosine_distance (1.0 == identical direction)


def search(
    db, query: str, *, k: int = 8, domain: str | None = None, language: str | None = None
) -> list[Hit]:
    """Top-k Chunks by cosine similarity to `query`, joined to their source.

    `domain`/`language`, when given, filter on the joined KnowledgeSource
    (e.g. only "tone"-domain sources, or only Greek-language sources) — the
    query itself is still embedded and matched exactly the same way.
    """
    qv = get_provider().embed([query], is_query=True)[0]
    distance = Chunk.embedding.cosine_distance(qv)

    stmt = (
        select(Chunk, KnowledgeSource, distance.label("distance"))
        .join(KnowledgeSource, Chunk.source_id == KnowledgeSource.id)
        .order_by(distance)
        .limit(k)
    )
    if domain is not None:
        stmt = stmt.where(KnowledgeSource.domain == domain)
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
            page=chunk.page,
            score=1.0 - dist,
        )
        for chunk, source, dist in rows
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
