"""`blueprint_default` — the tutor's edited SETTINGS-default lesson blueprint, the
default handed to every NEW curriculum.

THE DEFAULT IS NOT IN HERE, AND THAT IS THE WHOLE DESIGN — the same shape as
`prompt_override` (see `models/prompt.py`). The code default lives in
`app.curriculum.blueprint.default_blueprint()`, in git, shipped with the golden and
regression tests that pin it byte-for-byte. This table holds only the DELTA: a row
exists only once the tutor edits the settings default, and Reset (`DELETE`) removes
it, restoring the git-backed code default. Seeding the default in here would invert
the property that matters — the code would stop being the truth, and a migration
would become a blueprint change.

ONE ROW (`id = 1`), enforced by a CHECK constraint rather than a convention the
callers must remember: a blueprint singleton is exactly the shape of `app_setting`
(`models/setting.py`), and the constraint is what makes "the settings default" a
thing the database guarantees is single, not a thing the router hopes is.

This table is NEVER read on the draft path. A course either carries its own frozen
`meta["blueprint"]` or falls back to the CODE default — editing this row must never
change an existing course (spec invariant #3), which is only true because the draft
path (`blueprint.blueprint_from_course_meta`) does not consult this table at all.
"""
from __future__ import annotations

from sqlalchemy import CheckConstraint, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin


class BlueprintDefault(Base, TimestampMixin):
    __tablename__ = "blueprint_default"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    blueprint: Mapped[dict] = mapped_column(JSON, nullable=False)

    __table_args__ = (CheckConstraint("id = 1", name="blueprint_default_singleton"),)
