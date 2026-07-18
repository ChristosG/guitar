"""Plan C, Task 3 — per-course blueprint persistence + the settings-default table.

Two resolution functions, and the difference between them IS spec invariant #3:

  * `blueprint_from_course_meta(meta)` — the DRAFT-PATH resolver. A course whose
    `meta` carries no `"blueprint"` (i.e. every course that exists today) falls back
    to the CODE default, never to the settings table. This is what makes invariant #2
    (byte-identical regression) hold for legacy courses, and invariant #3 (editing the
    settings default must not touch a blueprintless course) hold for future edits.

  * `resolve_default_blueprint(db)` — the SEED resolver. The `blueprint_default` row
    if the tutor edited it, else the code default. Read only to seed a NEW course at
    materialize time and by the Settings editor.

The whole point of the split: editing `blueprint_default` changes only FUTURE courses.
"""
from app.curriculum import blueprint_store as store
from app.curriculum.blueprint import (
    blueprint_from_course_meta,
    default_blueprint,
    section_keys,
    validate_blueprint,
)
from app.curriculum.corpus import LibraryContext
from app.curriculum.outline import materialize_outline
from app.curriculum.shape import plan_shape
from app.models.block import Block

SHAPE = plan_shape(8, 1, 50)  # 8 lessons / 2 modules x 4 — only its meta matters here
_LIB = LibraryContext(text="", token_count=0, fits=True)


def _outline() -> dict:
    return {
        "title": "Tone Fundamentals",
        "modules": [
            {
                "title": "Module 1", "objective": "o", "tier": "library",
                "coverage_note": "p.19",
                "lessons": [
                    {"title": "L1.1", "objective": "o", "est_minutes": 50},
                ],
            },
        ],
    }


def _custom_blueprint() -> dict:
    """A settings default a tutor might save: an extra prose section, and the first
    section reweighted. The two structured sections survive untouched (invariant #4),
    so `validate_blueprint` accepts it."""
    bp = default_blueprint()
    bp["sections"][0]["weight"] = 0.11
    bp["sections"].insert(1, {
        "key": "improv",
        "label": {"el": "Αυτοσχεδιασμός", "en": "Improvisation"},
        "description": "Improvise four bars over the lesson's key before theory.",
        "weight": 0.05, "kind": "prose", "audience": "student", "enabled": True,
    })
    return bp


# --- invariant #2: a blueprintless course resolves to the CODE default -------

def test_course_without_blueprint_falls_back_to_code_default():
    assert blueprint_from_course_meta({"brief": "x"}) == default_blueprint()
    assert blueprint_from_course_meta(None) == default_blueprint()
    assert blueprint_from_course_meta({}) == default_blueprint()
    # A malformed/empty blueprint is treated as absent, not honoured.
    assert blueprint_from_course_meta({"blueprint": {}}) == default_blueprint()
    assert blueprint_from_course_meta({"blueprint": "nonsense"}) == default_blueprint()


def test_course_with_a_blueprint_returns_that_frozen_blueprint():
    frozen = validate_blueprint(_custom_blueprint())
    assert blueprint_from_course_meta({"blueprint": frozen}) == frozen


# --- invariant #3: editing the settings default never touches existing courses

def test_editing_settings_default_does_not_touch_a_blueprintless_course(db):
    store.save_default_blueprint(db, _custom_blueprint())
    # A course row with NO meta["blueprint"] STILL resolves to the CODE default —
    # the settings table is not in this resolver's world at all.
    assert blueprint_from_course_meta({"brief": "x"}) == default_blueprint()
    assert blueprint_from_course_meta(None) == default_blueprint()


# --- the seed resolver: table row if present, else code default --------------

def test_resolve_default_reads_table_then_code(db):
    assert store.resolve_default_blueprint(db) == default_blueprint()

    custom = _custom_blueprint()
    store.save_default_blueprint(db, custom)
    assert store.resolve_default_blueprint(db) == validate_blueprint(custom)

    assert store.reset_default_blueprint(db) is True
    assert store.resolve_default_blueprint(db) == default_blueprint()
    # Reset is idempotent: nothing to delete is False, not an error.
    assert store.reset_default_blueprint(db) is False


def test_save_validates_and_normalizes(db):
    import pytest

    from app.curriculum.blueprint import BlueprintInvalid

    bad = default_blueprint()
    bad["sections"][0]["weight"] = 5.0  # out of [0, 1]
    with pytest.raises(BlueprintInvalid) as exc:
        store.save_default_blueprint(db, bad)
    assert exc.value.code == "bad_weight"
    # A rejected save leaves the table empty — resolve is still the code default.
    assert store.resolve_default_blueprint(db) == default_blueprint()


def test_resolve_returns_a_copy_not_the_managed_row(db):
    """Mutating what `resolve_default_blueprint` hands back must not corrupt the
    stored row — it is a fresh object, like `default_blueprint()` itself."""
    store.save_default_blueprint(db, _custom_blueprint())
    got = store.resolve_default_blueprint(db)
    got["sections"][0]["weight"] = 0.99
    assert store.resolve_default_blueprint(db)["sections"][0]["weight"] != 0.99


# --- materialize seeds the RESOLVED settings default, and freezes it ---------

def test_materialize_seeds_the_resolved_settings_default(db):
    store.save_default_blueprint(db, _custom_blueprint())
    root_id = materialize_outline(
        db, _outline(), title="Tone Fundamentals", language="el",
        shape=SHAPE, library=_LIB, blueprint=None,
    )
    course = db.get(Block, root_id)
    assert course.meta["blueprint"] == validate_blueprint(_custom_blueprint())


def test_materialize_with_no_settings_row_seeds_the_code_default(db):
    root_id = materialize_outline(
        db, _outline(), title="Tone Fundamentals", language="el",
        shape=SHAPE, library=_LIB, blueprint=None,
    )
    course = db.get(Block, root_id)
    assert course.meta["blueprint"] == default_blueprint()


def test_materialize_freezes_an_explicit_blueprint_over_the_settings_default(db):
    # Even with a settings default saved, an explicitly-passed blueprint wins and is
    # frozen onto the course — this is the wizard "structure" step's path.
    store.save_default_blueprint(db, _custom_blueprint())
    explicit = default_blueprint()
    explicit["sections"][0]["enabled"] = False  # drop warm_up for THIS course only
    root_id = materialize_outline(
        db, _outline(), title="Tone Fundamentals", language="el",
        shape=SHAPE, library=_LIB, blueprint=explicit,
    )
    course = db.get(Block, root_id)
    assert course.meta["blueprint"] == explicit
    # invariant #6 round-trip: the frozen per-course blueprint is what the fan-out
    # would draft from, and warm_up is gone from its section keys.
    resolved = blueprint_from_course_meta(course.meta)
    assert "warm_up" not in section_keys(resolved)
