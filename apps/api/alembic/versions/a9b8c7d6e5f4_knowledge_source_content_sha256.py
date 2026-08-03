"""knowledge_source content sha256

Revision ID: a9b8c7d6e5f4
Revises: c7d8e9f0a1b2
Create Date: 2026-08-03 05:55:00.000000

The duplicate-upload guard's column (see `routers/knowledge.py::upload_source`).
A book uploaded twice becomes two `source_id`s, two compiles, and — because
canon consensus/divergence is keyed on DISTINCT source ids — a manufactured
"divergence" between two readings of the same pages, on the product's headline
surface. The hash is what lets upload say "you already have this book" instead.

Nullable, and existing rows stay NULL (they never match): the guard protects
every upload from now on; back-filling old rows would mean re-reading every
stored PDF at migration time on the tutor's machine, for a case (pre-existing
duplicates) the tutor can see and fix himself in the Library list.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9b8c7d6e5f4'
down_revision: Union[str, Sequence[str], None] = 'c7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('knowledge_source', sa.Column('content_sha256', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_knowledge_source_content_sha256'), 'knowledge_source', ['content_sha256'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_knowledge_source_content_sha256'), table_name='knowledge_source')
    op.drop_column('knowledge_source', 'content_sha256')
