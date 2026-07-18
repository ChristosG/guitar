"""Resolving the SETTINGS-default blueprint: the tutor's edited `blueprint_default`
row if he has one, else the code default.

This is the ONLY reader of the `blueprint_default` table, and it exists so the
distinction spec invariant #3 turns on has exactly ONE home. There are two resolvers
in this unit and they must never be confused:

  * `resolve_default_blueprint(db)` (HERE) — the SEED resolver. Reads the table.
    Used only to seed a NEW course at `materialize_outline` time, to pre-fill the
    wizard "structure" step, and by the Settings editor. Editing the row changes
    what FUTURE courses are seeded with — nothing that already exists.

  * `blueprint.blueprint_from_course_meta(meta)` — the DRAFT-PATH resolver. Never
    reads the table; a course with no frozen `meta["blueprint"]` falls back to the
    CODE default. That is why editing the settings default cannot mutate an existing
    or blueprintless course.
"""
from __future__ import annotations

import copy

from app.curriculum.blueprint import default_blueprint, validate_blueprint
from app.models.blueprint_default import BlueprintDefault

_SINGLETON_ID = 1


def resolve_default_blueprint(db) -> dict:
    """The tutor's saved settings default, or the code default if he never edited it.

    Returns a FRESH object every call, like `default_blueprint()` — the row's JSON is
    deep-copied so a caller that freezes this onto a course (materialize) cannot alias
    the ORM-managed value, and mutating the result cannot corrupt the stored row.
    """
    row = db.get(BlueprintDefault, _SINGLETON_ID) if db is not None else None
    return copy.deepcopy(row.blueprint) if row is not None else default_blueprint()


def has_default_override(db) -> bool:
    """Whether the tutor has saved a settings default (a row exists). The Settings
    UI's `is_override` flag — "you've customised this", offer Restore. Kept here so
    `blueprint_store` stays the single reader of the `blueprint_default` table."""
    return db is not None and db.get(BlueprintDefault, _SINGLETON_ID) is not None


def save_default_blueprint(db, bp: dict) -> None:
    """Validate + normalize `bp`, then upsert the single row. Commits.

    Validation runs BEFORE the write (raising `BlueprintInvalid`), so a rejected
    blueprint leaves any existing default untouched. `blueprint` is a plain `JSON`
    scalar column — whole-value reassignment is the correct write, there is no
    `MutableDict` to trip over here.
    """
    bp = validate_blueprint(bp)
    row = db.get(BlueprintDefault, _SINGLETON_ID)
    if row is None:
        db.add(BlueprintDefault(id=_SINGLETON_ID, blueprint=bp))
    else:
        row.blueprint = bp
    db.commit()


def reset_default_blueprint(db) -> bool:
    """Delete the row, restoring the git-backed code default (invariant: Reset =
    delete, no history table). Returns whether a row was actually removed —
    idempotent, so resetting an already-default settings is `False`, not an error.
    """
    row = db.get(BlueprintDefault, _SINGLETON_ID)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
