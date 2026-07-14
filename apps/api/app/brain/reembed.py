"""Re-embed every chunk IN PLACE, from its own `chunk.text` (Plan 13, Stage 4.2).

Run this IMMEDIATELY after `alembic upgrade head` applies `a3c7e1b90d42`
(vector(2560) -> vector(384)). That migration ZERO-FILLS the column — a 2560-dim
Qwen vector cannot be projected into e5's 384-dim space, so there is nothing to
convert — and this is what puts real numbers back. Between the two commands
search is degraded but never wrong: a zero vector gives a NaN cosine distance,
which sorts last and fails `retrieve.py`'s floor.

    docker compose exec -T api python - < scripts/reembed.py   # or POST /knowledge/reindex

WHY IT READS `chunk.text` AND NOT `page.text` — this is the whole design, and the
alternative silently destroys data:

    `paginate.py` only OPPORTUNISTICALLY captures a PDF's text layer. Chunks, by
    contrast, are built by `ingest.py` from `extract_text`, which is heading-aware
    and is where `section_path` comes from. Rebuilding the corpus from `Page.text`
    would therefore (a) EMPTY any PDF whose vision-OCR never ran and whose pages
    carry no text layer, and (b) drop `section_path` corpus-wide, because Page rows
    have never held one.

    `chunk.text` is the only place the extracted, chunked, heading-attributed text
    actually lives, so it is the only honest input. Nothing is deleted, nothing is
    re-chunked, nothing is re-paginated: exactly one column is overwritten.

IDEMPOTENT AND RESUMABLE. The same text yields the same vector, so re-running is
a no-op; commits are batched, so a run killed halfway leaves a half-new,
half-zero index that the next run simply finishes. `only_stale=True` skips sources
already stamped with the current model — which is what makes that resumed run
cheap. The stamp is written only AFTER a source's every chunk is committed, so a
crash mid-source redoes that source rather than skipping a half-embedded one.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from app.llm.embed_factory import get_embedder
from app.models.knowledge import Chunk, KnowledgeSource

log = logging.getLogger(__name__)

# One embed call + one commit per this many chunks. Small enough that a killed run
# loses seconds of work; large enough that per-batch overhead disappears.
BATCH = 32


def reembed_all(db, *, only_stale: bool = False) -> dict:
    """Re-embed the corpus. Returns a small summary dict (also the JSON body of
    `POST /knowledge/reindex`)."""
    embedder = get_embedder()
    model_id = embedder.model_id
    log.info("reembed: %s (dim=%d), only_stale=%s", model_id, embedder.dim, only_stale)

    started = time.monotonic()
    sources = db.scalars(select(KnowledgeSource).order_by(KnowledgeSource.created_at)).all()
    chunks_done = 0
    sources_done = 0
    sources_skipped = 0

    for source in sources:
        if only_stale and source.embed_model == model_id:
            sources_skipped += 1
            continue

        chunk_ids = list(
            db.scalars(select(Chunk.id).where(Chunk.source_id == source.id).order_by(Chunk.id))
        )
        if not chunk_ids:
            # Stamp it anyway: an empty source IS consistent with the current model,
            # and leaving `embed_model` NULL would make `only_stale` re-scan it on
            # every future run, forever.
            source.embed_model = model_id
            db.commit()
            sources_done += 1
            continue

        for i in range(0, len(chunk_ids), BATCH):
            chunks = db.scalars(
                select(Chunk).where(Chunk.id.in_(chunk_ids[i : i + BATCH]))
            ).all()
            vectors = embedder.embed([c.text for c in chunks], is_query=False)
            if len(vectors) != len(chunks):
                raise RuntimeError(
                    f"embed() returned {len(vectors)} vectors for {len(chunks)} chunks"
                )
            for chunk, vector in zip(chunks, vectors):
                chunk.embedding = vector
            db.commit()
            chunks_done += len(chunks)

        source.embed_model = model_id
        db.commit()
        sources_done += 1
        log.info("ok    %-50.50s %4d chunks", source.title, len(chunk_ids))

    elapsed = time.monotonic() - started
    log.info(
        "reembed: %d chunks across %d sources in %.1fs (%d skipped)",
        chunks_done, sources_done, elapsed, sources_skipped,
    )
    return {
        "embed_model": model_id,
        "chunks": chunks_done,
        "sources": sources_done,
        "sources_skipped": sources_skipped,
        "seconds": round(elapsed, 1),
    }
