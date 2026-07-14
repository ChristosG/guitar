"""`app.llm.schema.to_anthropic_schema` — against the REAL schemas, not toys.

The bug this file exists to prevent is specific and quiet: a sanitizer that
walks `properties` but forgets `$defs` looks completely correct at the top
level. `ChordDiagramSpec` puts `BarreSpec` in `$defs`, `ScaleDiagramSpec` puts
`PositionSpec` there, and `AmpSettingsSpec` puts `DialSpec` there — each with
its own `minimum`/`maximum` and its own missing `additionalProperties`. Ship
that and the first chord diagram Claude generates 400s, in production, on a
path with no test.

So every assertion here runs over the actual `SPECS[kind].model_json_schema()`
output, recursively.
"""
import pytest

from app.artifacts.specs import SPECS
from app.llm.schema import to_anthropic_schema, unsupported_keywords

ARTIFACT_SCHEMAS = {kind: model.model_json_schema() for kind, model in SPECS.items()}


def _all_nodes(node, path="$"):
    """Every dict in the tree, so an assertion can't miss a nested branch."""
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _all_nodes(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _all_nodes(v, f"{path}[{i}]")


# ---------------------------------------------------------------------------
# The premise. If these first two ever fail, the sanitizer has become dead code
# and should be deleted — not "fixed".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(ARTIFACT_SCHEMAS))
def test_raw_pydantic_schemas_really_are_unusable_by_claude(kind):
    """Non-vacuity guard: prove the RAW schema is dirty before proving the
    sanitized one is clean. Otherwise `test_no_unsupported_keyword_survives`
    would still pass if `to_anthropic_schema` were the identity function."""
    assert unsupported_keywords(ARTIFACT_SCHEMAS[kind]), (
        f"{kind}: raw Pydantic schema is already Claude-clean — if this is "
        f"genuinely true now, the sanitizer is unnecessary for it"
    )


@pytest.mark.parametrize("kind", sorted(ARTIFACT_SCHEMAS))
def test_no_unsupported_keyword_survives_anywhere_including_defs(kind):
    cleaned = to_anthropic_schema(ARTIFACT_SCHEMAS[kind])
    leftovers = unsupported_keywords(cleaned)
    assert leftovers == [], f"{kind}: {leftovers}"


@pytest.mark.parametrize("kind", sorted(ARTIFACT_SCHEMAS))
def test_every_object_node_forbids_extra_properties(kind):
    cleaned = to_anthropic_schema(ARTIFACT_SCHEMAS[kind])
    for path, node in _all_nodes(cleaned):
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False, f"{kind} at {path}"


@pytest.mark.parametrize("kind", sorted(ARTIFACT_SCHEMAS))
def test_every_property_is_required(kind):
    """Structured outputs expect every declared property in `required`; Pydantic
    omits any field with a default. See `app/llm/schema.py`'s docstring."""
    cleaned = to_anthropic_schema(ARTIFACT_SCHEMAS[kind])
    for path, node in _all_nodes(cleaned):
        props = node.get("properties")
        if isinstance(props, dict):
            assert set(node.get("required", [])) == set(props), f"{kind} at {path}"


# ---------------------------------------------------------------------------
# What must SURVIVE. A sanitizer that strips too much is just as broken as one
# that strips too little — `$ref` is how BarreSpec is reachable at all.
# ---------------------------------------------------------------------------

def test_defs_and_refs_survive():
    cleaned = to_anthropic_schema(ARTIFACT_SCHEMAS["chord_diagram"])
    assert "BarreSpec" in cleaned["$defs"]
    assert cleaned["properties"]["barres"]["items"]["$ref"] == "#/$defs/BarreSpec"


def test_nullable_optionals_survive_as_anyof():
    """`str | None` must stay a union with null — that is what lets a property
    be both REQUIRED (above) and genuinely optional in content."""
    cleaned = to_anthropic_schema(ARTIFACT_SCHEMAS["tone_recipe"])
    artist = cleaned["properties"]["artist"]
    types = {b.get("type") for b in artist["anyOf"]}
    assert types == {"string", "null"}
    assert "artist" in cleaned["required"]


def test_a_property_NAMED_title_is_not_mistaken_for_the_title_KEYWORD():
    """The trap that a blind key-scan walks straight into.

    Pydantic emits `"title": "Frets"` as annotation noise on every node, so we
    strip the `title` keyword. But `TabSpec` has a property genuinely NAMED
    `title` — and inside a `properties` map, the keys are property names, not
    keywords. A sanitizer that doesn't know the difference deletes the tab's
    title field, and nothing fails until a tutor notices his tabs are unnamed.
    """
    raw = ARTIFACT_SCHEMAS["tab"]
    assert "title" in raw["properties"], "precondition: TabSpec really has a `title` field"

    cleaned = to_anthropic_schema(raw)
    assert "title" in cleaned["properties"], "the tab's `title` FIELD was eaten"
    assert "title" in cleaned["required"]
    # ...while the annotation noise on the sibling field IS gone.
    assert "title" not in cleaned["properties"]["alphaTex"]


def test_enum_and_description_survive():
    cleaned = to_anthropic_schema(
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["a", "b"], "description": "keep me"},
            },
            "required": ["kind"],
        }
    )
    assert cleaned["properties"]["kind"]["enum"] == ["a", "b"]
    assert cleaned["properties"]["kind"]["description"] == "keep me"


def test_the_input_schema_is_not_mutated():
    """`SPECS[kind].model_json_schema()` is rebuilt per call today, but Pydantic
    caches internally and `guided_json` is called from a ThreadPoolExecutor —
    an in-place sanitizer would be a cross-thread corruption waiting to happen."""
    raw = ARTIFACT_SCHEMAS["chord_diagram"]
    before = str(raw)
    to_anthropic_schema(raw)
    assert str(raw) == before


# ---------------------------------------------------------------------------
# The constraint that is now enforced ONLY by Pydantic. This is the test that
# documents the consequence of the whole design.
# ---------------------------------------------------------------------------

def test_the_six_string_constraint_is_gone_from_the_schema_and_pydantic_must_catch_it():
    """`frets` is `minItems: 6, maxItems: 6` — a six-string guitar. Claude cannot
    be told that any more, so a 4-fret chord is now SCHEMA-VALID on the wire.

    `validate_spec` is what stops it reaching the tutor, and the one-shot repair
    retry in `artifacts/generate.py` is what turns it into a corrected artifact
    rather than a failed job. If this test ever fails because the schema DOES
    carry minItems again, the repair retry can be downgraded back to defensive."""
    from pydantic import ValidationError

    cleaned = to_anthropic_schema(ARTIFACT_SCHEMAS["chord_diagram"])
    frets = cleaned["properties"]["frets"]
    assert "minItems" not in frets and "maxItems" not in frets

    with pytest.raises(ValidationError):
        SPECS["chord_diagram"].model_validate(
            {"name": "Am", "frets": [0, 1, 2, 3], "fingers": [0, 1, 2, 3, 4, 0]}
        )
