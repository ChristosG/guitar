"""`prompt_override` — the few prompt fragments the tutor has rewritten, and what
they used to say.

THE DEFAULT IS NOT IN HERE, AND THAT IS THE WHOLE DESIGN. Every prompt's real
text lives in code (`students/context.py::STUDENT_PITCH`, and the rest), where it
is diffable, reviewable, and shipped with the guard tests that were written
alongside it. This table holds only the DELTA: rows exist for slices the tutor has
actually changed, and an empty table means the app behaves exactly as its source
reads. Seeding the defaults in here instead would look tidier and would invert the
one property that matters — the code would stop being the truth, a migration would
become a prompt change, and `git log` would stop being able to answer "what were
we telling the model in March".

WHY A SEPARATE TABLE AND NOT COLUMNS ON `app_setting`: that is a fixed-column
singleton pinned to one UUID (see `models/setting.py`). Slices are an open set —
`student.pitch` today, an append slot per flow when one is earned — and an open
set is rows, not columns.

`slice_id` IS THE PRIMARY KEY, not a surrogate uuid with a unique index on top.
One slice has one override, and making that the PK means the database enforces it
rather than the router remembering to. `varchar(80)` because these are our own
dotted identifiers (`shared.student_brief.extra_guidance` is 37), not user input.

`prompt_override_history` KEEPS THE ROWS THIS ONE OVERWRITES. Allowing edits at
all is what makes it necessary: the previous text of an override exists nowhere
else — the code default is in git, but the paragraph he wrote last month and just
replaced is not. `replaced_at`, not `created_at`, because that is what the
timestamp means: the moment this text STOPPED being what the model got.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PkMixin, TimestampMixin

# Long enough for any dotted slice id we would ever write, short enough that a
# typo'd key cannot become a row. Mirrored by the migration, and enforced where it
# can actually be fixed: `test_every_registered_slice_id_fits_its_column` fails at
# REGISTRATION time, in CI, rather than leaving a runtime guard to turn a
# too-clever id into a 500 in front of the tutor. Slice ids are ours, not input —
# they cannot be wrong at request time, only at authoring time.
SLICE_ID_LEN = 80


class PromptOverride(Base, TimestampMixin):
    __tablename__ = "prompt_override"

    slice_id: Mapped[str] = mapped_column(String(SLICE_ID_LEN), primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class PromptOverrideHistory(Base, PkMixin):
    __tablename__ = "prompt_override_history"

    # NOT a foreign key to `prompt_override.slice_id`. History must OUTLIVE the
    # row it describes: the single most valuable snapshot is the one taken when an
    # override is deleted (Reset), and an FK with any ON DELETE rule either
    # forbids that delete or cascades away the exact text he might want back.
    slice_id: Mapped[str] = mapped_column(String(SLICE_ID_LEN), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    replaced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
