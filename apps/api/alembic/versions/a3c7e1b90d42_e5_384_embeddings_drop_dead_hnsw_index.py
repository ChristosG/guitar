"""embeddings: vector(2560) -> vector(384), drop the dead HNSW index, stamp embed_model

Revision ID: a3c7e1b90d42
Revises: e2b7a4c19d05
Create Date: 2026-07-14

Plan 13, Stage 4.1. Three changes, one revision, because they are one decision:
the corpus stops being embedded by a 4B model on a shared GPU box and starts
being embedded by a 384-dim ONNX model on the local CPU.

1. `ALTER TABLE chunk ALTER COLUMN embedding TYPE vector(384)` — **NOT a TRUNCATE.**

   Not one row is deleted. `chunk.text`, `chunk.section_path` and `chunk.page_id`
   are not touched, which is the whole point: `scripts/reembed.py` rebuilds every
   vector FROM `chunk.text`, in place, and a truncate-and-reingest would instead
   have to rebuild the corpus from `Page.text` — which `paginate.py` only
   *opportunistically* populates, and which never carries the heading-aware
   `section_path` the chunker derives. Truncating here destroys any PDF whose
   vision-OCR never ran. Verified on a copy of the real DB: 408 chunks before,
   408 after.

   A `USING` clause is REQUIRED, and it is not a formality — a bare ALTER fails
   with `expected 384 dimensions, not 2560`. There is no honest cast: a 2560-dim
   Qwen vector and a 384-dim e5 vector are points in two spaces with no shared
   geometry, so nothing can be "converted". The column is therefore zero-filled
   and the real vectors are written by `scripts/reembed.py` immediately after.

   Deploy order is `alembic upgrade head` -> `python scripts/reembed.py`, and in
   between, search is degraded — pgvector returns NaN for the cosine distance to
   a zero vector, so an un-re-embedded chunk sorts LAST and (NaN failing every
   `>=`) is dropped by `retrieve.py`'s floor. That is the good failure: a
   half-finished re-embed yields fewer results, never wrong ones, and a resumed
   run heals it.

   Postgres will not ALTER a column an index depends on, which brings us to:

2. `DROP INDEX ix_chunk_embedding_hnsw` — **and it is not recreated.**

   That index has never been used by a single query. It was built (in
   `4b44fbf6e38f`) on the expression `embedding::halfvec(2560)`, because
   pgvector caps HNSW on `vector` at 2000 dims; `retrieve.search` orders by
   `Chunk.embedding.cosine_distance(qv)` on the FULL vector column, which does
   not match that expression, so the planner cannot use the index and has been
   doing an exact brute-force scan all along. (Confirmed by EXPLAIN when the
   index was built; the retrieve.py docstring has said so ever since.)

   Dead, and expensive to keep: `--autogenerate` reports it as a spurious
   `drop_index` on EVERY revision (Alembic does not reflect expression indexes),
   which is why NINE migrations in this directory carry a hand-edit and a
   comment explaining why they deleted it. That tax ends here. At 408 chunks —
   and at ten times that — an exact scan over vector(384) is sub-millisecond;
   when the corpus genuinely outgrows it, the index to build is the one that
   matches the ORDER BY, not this one.

3. `knowledge_source.embed_model` — see the column comment in `models/knowledge.py`.

`sa.Uuid(as_uuid=True)` also replaces `postgresql.UUID(as_uuid=True)` in the four
model files in this same commit. It is NOT in this migration because it needs no
DDL: on Postgres both render `UUID`, byte for byte. It is listed here so the next
person who diffs models-vs-migrations does not go looking for the missing one.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a3c7e1b90d42"
down_revision: Union[str, Sequence[str], None] = "e2b7a4c19d05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Order matters: Postgres refuses to ALTER a column an index depends on.
    op.execute("DROP INDEX IF EXISTS ix_chunk_embedding_hnsw")
    op.execute(
        "ALTER TABLE chunk ALTER COLUMN embedding TYPE vector(384) "
        "USING array_fill(0::real, ARRAY[384])::vector(384)"
    )
    op.add_column(
        "knowledge_source",
        sa.Column("embed_model", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    """Widens the column back and restores the index — and, exactly like the
    upgrade, it ZEROES every vector, because the 384-dim numbers cannot be
    projected back into the 2560-dim space they never came from.

    So this downgrade restores the SCHEMA, not the index. The only real way back
    to a working Qwen index is to re-embed against the old model (or restore the
    backup). Stated out loud, because an embedding migration that reports
    "downgraded OK" and leaves search silently returning nothing is precisely the
    kind of quiet lie this codebase tries not to tell.
    """
    op.drop_column("knowledge_source", "embed_model")
    op.execute(
        "ALTER TABLE chunk ALTER COLUMN embedding TYPE vector(2560) "
        "USING array_fill(0::real, ARRAY[2560])::vector(2560)"
    )
    op.execute(
        "CREATE INDEX ix_chunk_embedding_hnsw ON chunk "
        "USING hnsw ((embedding::halfvec(2560)) halfvec_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
