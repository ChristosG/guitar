"""page.ocr_attempts — the cap that makes an `empty` page retryable (Stage 7.2/7.4)

Before this, `ocr_source` picked up only pending/failed/ocr_running pages, so a
page the vision model returned "" for was PERMANENTLY dead: no path in the app
would ever look at it again, and one transient hiccup silently cost the tutor a
page of his book. `empty` is now a pickup status — which is only affordable
because of this column: a genuinely blank scan (a real book has several) would
otherwise be re-billed to a paid vision model on every single retry of the book,
forever. Three pickups, then the page rests.

Backfill: existing rows get 0, not NULL — but `ocr.py`'s pickup filter still has
an explicit `ocr_attempts IS NULL` branch, because `NULL < 3` is NULL in SQL and
a single un-backfilled row would otherwise silently drop out of every OCR run.

Revision ID: d4f1a90c7b28
Revises: 9c1e4a5d7b30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4f1a90c7b28"
down_revision: Union[str, Sequence[str], None] = "9c1e4a5d7b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "page",
        sa.Column("ocr_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("page", "ocr_attempts")
