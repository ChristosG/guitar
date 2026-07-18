"""Full delete + re-add for the structured `exercises`/`qa` sections.

Follow-up to Plan C (`docs/superpowers/plans/2026-07-18-unit-c-prompts-blueprint.md`):
the tutor wants FULL control, not just enable/disable. `validate_blueprint` used to
require EXACTLY ONE `exercises`-kind and EXACTLY ONE `qa`-kind section to be present
(possibly disabled, but never removed). That rule is gone. What replaces it:

  * AT MOST ONE section of `kind == "exercises"`, AT MOST ONE of `kind == "qa"` —
    no duplicate structured kinds.
  * IF a structured section IS present, its `key` must still be the canonical one
    (`"exercises"` / `"qa_prompts"`) that `depth._KIND_BUILDERS` dispatches on —
    renaming one is still forbidden, because the model-facing schema is built by
    KIND, not by key, and a renamed key would silently stop matching anything a
    consumer keys on by name (`_section_minutes`, `persist_lesson`'s labels, etc).
  * At least one ENABLED section overall — an all-disabled/empty blueprint drafts
    nothing and is rejected outright.

`default_blueprint()` itself is UNCHANGED (all 8 sections, byte-identical) — see
`test_blueprint_schema_golden.py` / `test_blueprint_regression.py`.
"""
import pytest

from app.curriculum.blueprint import (
    BlueprintInvalid,
    build_lesson_schema,
    default_blueprint,
    structured_section_template,
    validate_blueprint,
)


def _drop(bp: dict, key: str) -> dict:
    bp["sections"] = [s for s in bp["sections"] if s["key"] != key]
    return bp


def _section(bp: dict, key: str) -> dict:
    return next(s for s in bp["sections"] if s["key"] == key)


# --- removal is now legal -----------------------------------------------------

def test_validate_accepts_blueprint_with_exercises_removed():
    out = validate_blueprint(_drop(default_blueprint(), "exercises"))
    assert "exercises" not in [s["key"] for s in out["sections"]]
    assert any(s["enabled"] for s in out["sections"])


def test_validate_accepts_blueprint_with_qa_removed():
    out = validate_blueprint(_drop(default_blueprint(), "qa_prompts"))
    assert "qa_prompts" not in [s["key"] for s in out["sections"]]


def test_validate_accepts_blueprint_with_both_structured_sections_removed():
    bp = _drop(_drop(default_blueprint(), "exercises"), "qa_prompts")
    out = validate_blueprint(bp)
    keys = [s["key"] for s in out["sections"]]
    assert "exercises" not in keys
    assert "qa_prompts" not in keys
    assert any(s["enabled"] for s in out["sections"])
    assert len(out["sections"]) == 6


# --- but never duplicated, and never renamed while present --------------------

def test_validate_rejects_two_exercises_sections():
    # Different KEY (so `dup_key` cannot be what fires), same KIND — the rule
    # under test is "at most one section of kind == exercises", not key uniqueness.
    bp = default_blueprint()
    dup = dict(_section(bp, "exercises"))
    dup["key"] = "extra_exercises"
    bp["sections"].append(dup)
    with pytest.raises(BlueprintInvalid) as exc:
        validate_blueprint(bp)
    assert exc.value.code == "structured_section_duplicate"


def test_validate_rejects_two_qa_sections():
    bp = default_blueprint()
    dup = dict(_section(bp, "qa_prompts"))
    dup["key"] = "extra_qa"
    bp["sections"].append(dup)
    with pytest.raises(BlueprintInvalid) as exc:
        validate_blueprint(bp)
    assert exc.value.code == "structured_section_duplicate"


def test_validate_rejects_exercises_kind_section_with_wrong_key():
    bp = default_blueprint()
    _section(bp, "exercises")["key"] = "drills"
    with pytest.raises(BlueprintInvalid) as exc:
        validate_blueprint(bp)
    assert exc.value.code == "structured_section_renamed"


def test_validate_rejects_qa_kind_section_with_wrong_key():
    bp = default_blueprint()
    _section(bp, "qa_prompts")["key"] = "discussion"
    with pytest.raises(BlueprintInvalid) as exc:
        validate_blueprint(bp)
    assert exc.value.code == "structured_section_renamed"


# --- at least one enabled section overall --------------------------------------

def test_validate_rejects_all_disabled_blueprint():
    bp = default_blueprint()
    for s in bp["sections"]:
        s["enabled"] = False
    with pytest.raises(BlueprintInvalid) as exc:
        validate_blueprint(bp)
    assert exc.value.code == "no_enabled_sections"


# --- build_lesson_schema on a blueprint missing a structured section ----------

def test_build_lesson_schema_without_exercises_has_no_exercises_key():
    bp = validate_blueprint(_drop(default_blueprint(), "exercises"))
    schema = build_lesson_schema(bp)
    assert "exercises" not in schema["properties"]
    assert "exercises" not in schema["required"]
    assert schema["required"] == [
        "title", "summary", "warm_up", "theory", "demonstration",
        "common_mistakes", "recap", "homework", "qa_prompts",
    ]


def test_build_lesson_schema_without_qa_has_no_qa_key():
    bp = validate_blueprint(_drop(default_blueprint(), "qa_prompts"))
    schema = build_lesson_schema(bp)
    assert "qa_prompts" not in schema["properties"]
    assert "qa_prompts" not in schema["required"]


def test_build_lesson_schema_without_either_structured_section():
    bp = validate_blueprint(
        _drop(_drop(default_blueprint(), "exercises"), "qa_prompts")
    )
    schema = build_lesson_schema(bp)
    assert "exercises" not in schema["properties"]
    assert "qa_prompts" not in schema["properties"]
    assert schema["required"] == [
        "title", "summary", "warm_up", "theory", "demonstration",
        "common_mistakes", "recap", "homework",
    ]


# --- the re-add template --------------------------------------------------------

def test_structured_section_template_exercises_matches_default():
    default_section = _section(default_blueprint(), "exercises")
    assert structured_section_template("exercises") == default_section


def test_structured_section_template_qa_matches_default():
    default_section = _section(default_blueprint(), "qa_prompts")
    assert structured_section_template("qa") == default_section


def test_structured_section_template_is_a_fresh_copy_each_call():
    a = structured_section_template("exercises")
    b = structured_section_template("exercises")
    a["weight"] = 999
    assert b["weight"] != 999


def test_structured_section_template_rejects_unknown_kind():
    with pytest.raises(ValueError):
        structured_section_template("prose")
