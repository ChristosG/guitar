"""`AppSetting` — the tutor's own runtime configuration: his Anthropic API key
and which model to spend it on.

A SINGLETON ROW, pinned to a fixed UUID. Not "the first row ordered by
created_at": two concurrent requests that both find an empty table would each
INSERT one, and from then on `GET /settings` and `get_provider()` could read
DIFFERENT rows — one holding the key the tutor just pasted, the other holding
nothing. A fixed primary key turns "create if missing" into an idempotent upsert
that the database itself serialises.

THE KEY IS NEVER STORED IN PLAINTEXT. `anthropic_key_ct` holds a Fernet
ciphertext derived from `settings.encryption_secret` (see
`app.settings_store`) — a separate secret from the session-signing
`app_secret`, so rotating the cookie key logs the tutor out without also
bricking the key he pasted. The column name ends in `_ct` so that a `SELECT *`
in psql, or a future ORM refactor, cannot mistake it for something readable.
"""
from __future__ import annotations

import uuid

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PkMixin, TimestampMixin

SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# The two models the Settings screen offers. Kept here (not imported from
# `llm/claude.py`) so the model layer does not depend on the provider layer;
# `settings_store` validates against this tuple before writing.
MODELS = ("claude-sonnet-5", "claude-haiku-4-5")
DEFAULT_MODEL = "claude-sonnet-5"


class AppSetting(Base, PkMixin, TimestampMixin):
    __tablename__ = "app_setting"

    anthropic_key_ct: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(40), default=DEFAULT_MODEL)
