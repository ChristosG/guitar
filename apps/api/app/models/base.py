"""The two mixins every table uses.

`sa.Uuid`, NOT `sqlalchemy.dialects.postgresql.UUID` (Plan 13, Stage 4). On
Postgres the two emit BYTE-IDENTICAL DDL — `id UUID NOT NULL` — so the swap
costs exactly zero migration. What it buys is that the model layer no longer
names a dialect: `sa.Uuid` renders `UUID` on Postgres and `CHAR(32)` on
SQLite, which turns a future single-file desktop bundle into a packaging
question rather than a schema rewrite. This was the largest Postgres-only
surface in the models, and removing it was free.

(The `vector` column in `models/knowledge.py` is a REAL Postgres dependency and
stays one. That is pgvector, not a naming choice.)
"""
import uuid
from datetime import datetime
from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

class PkMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(),
                                                 onupdate=func.now())
