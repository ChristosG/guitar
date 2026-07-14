"""chat_session: title + locale (chat history — Plan 13 Stage 5.6)

The rows were always there; nothing ever came back to read them. These two
columns are what make a `chat_session` resumable AS a listed conversation:
a `title` to show in the sidebar and a `locale` recording which language the
conversation was actually held in.

`title` is nullable ON PURPOSE — NULL means "no user message yet", which is
exactly the state every session the old UI created on mount was stuck in, and
`GET /chat` filters those out by joining on `message` rather than by reading
this column. Backfilling a title for existing rows is therefore pointless
work: an existing session either has messages (its title gets set the next
time... never — it's historical) or it has none (invisible either way). We
backfill anyway, from the first user message, because the tutor's real
transcripts are already in this table and appearing in the new sidebar with
no name at all would read as a bug.

`locale` gets a server_default so the backfill is implicit: every pre-existing
row was held in whatever the UI's default locale was, which is 'el'.

Revision ID: e2b7a4c19d05
Revises: c1a7d5f0e3b4
Create Date: 2026-07-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2b7a4c19d05"
down_revision: Union[str, Sequence[str], None] = "c1a7d5f0e3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_session", sa.Column("title", sa.String(length=200), nullable=True))
    op.add_column(
        "chat_session",
        sa.Column("locale", sa.String(length=5), nullable=False, server_default="el"),
    )

    # Backfill: the first user message of each session, whitespace left as-is
    # (this is a display string, and Postgres has no cheap equivalent of
    # `_title_from_message`'s whitespace collapse — the app-side helper is the
    # one that runs for every session created from here on).
    op.execute(
        """
        UPDATE chat_session cs
        SET title = LEFT(first_user.content, 60)
        FROM (
            SELECT DISTINCT ON (session_id) session_id, content
            FROM message
            WHERE role = 'user' AND content IS NOT NULL
            ORDER BY session_id, created_at, id
        ) AS first_user
        WHERE first_user.session_id = cs.id AND cs.title IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("chat_session", "locale")
    op.drop_column("chat_session", "title")
