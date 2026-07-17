"""page.text_source + page.ocr_reason

Revision ID: a1b2c3d4e5f6
Revises: d4f1a90c7b28
Create Date: 2026-07-17

ADDITIVE. Both columns are nullable with no server default: NULL means "this row
predates provenance tracking", which is true and is different from "unknown".
`alembic upgrade head` runs in the api container's CMD at boot, so this migrates
the tutor's live library in place without touching a single existing value.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "d4f1a90c7b28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("page", sa.Column("text_source", sa.String(length=20), nullable=True))
    op.add_column("page", sa.Column("ocr_reason", sa.String(length=30), nullable=True))


def downgrade() -> None:
    op.drop_column("page", "ocr_reason")
    op.drop_column("page", "text_source")
