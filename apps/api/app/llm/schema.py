"""JSON-Schema sanitizer: Pydantic/`guided_json` schemas -> what Claude accepts.

THE PREMISE THIS FILE EXISTS TO CORRECT. Two of Plan 13's designs were written
on the assumption that Claude's structured outputs enforce `minItems`/`maxItems`/
`minLength`/`minimum`. **They do not.** Structured outputs do not support
numeric constraints, string constraints, complex array constraints, recursive
schemas, or `additionalProperties != false`. The SDK *strips* the unsupported
keywords and validates client-side — i.e. you get an exception AFTER you have
paid for the tokens, which is the worst of both worlds.

So this module strips them FIRST, deliberately and visibly, and the constraints
they used to express move to the two places that can actually enforce them:

  1. THE PROMPT states them ("exactly 6 integers, one per string").
  2. PYTHON VALIDATES them after the fact — `app.artifacts.specs.validate_spec`
     (Pydantic) and `app.curriculum.depth.enforce_shape`.

That has a consequence worth saying out loud: **the one-shot repair retry in
`app/artifacts/generate.py` is now LOAD-BEARING, not defensive.** Before, vLLM's
guided decoding made a schema-invalid artifact nearly impossible and the retry
was a belt-and-braces. Now `ChordDiagramSpec.frets` really can come back with 4
entries instead of 6, because nothing at the API layer forbids it any more. The
retry is what turns that from a failed job into a corrected one.

What survives untouched: `$defs`, `$ref`, `anyOf`, `allOf`, `enum`, `const`,
`type`, `properties`, `items`, `description`. Verified against all 7 artifact
specs: none is recursive, so `$ref` is safe.

WHY EVERY PROPERTY BECOMES REQUIRED. Structured outputs (like OpenAI's strict
mode) expect every declared property to appear in `required`. Pydantic omits any
field that has a default. Rather than guess which dialect Anthropic enforces, we
take the strictly-safe option: promote every property to `required`. A field that
was optional-with-default is still satisfiable — a nullable field can be filled
with `null` (Pydantic's `str | None` already emits `anyOf: [string, null]`), and
a defaulted non-nullable field (`barres: list = []`) is satisfied by `[]`. The
cost is that the model must consider every field; the benefit is that we never
depend on an undocumented leniency.
"""
from __future__ import annotations

from typing import Any

# Keywords Claude's structured outputs do not support. Present in a schema they
# are (per the SDK) stripped and enforced client-side after billing — so we
# remove them here, where it is visible, rather than discovering it at runtime.
_UNSUPPORTED: frozenset[str] = frozenset(
    {
        # numeric constraints
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        # string constraints
        "minLength", "maxLength", "pattern",
        # array constraints
        "minItems", "maxItems", "uniqueItems",
        # not a constraint, but Pydantic emits these and they are pure noise in
        # the prompt's token budget (a `title` per property, on every property)
        "default", "title", "examples",
    }
)

# `format` values Anthropic documents as safe. Anything else is dropped rather
# than risked — none of this app's schemas use `format` today, so the allowlist
# costs nothing and stops a future `format: "uri"` from 400ing a curriculum job.
_SAFE_FORMATS: frozenset[str] = frozenset({"date", "date-time", "time", "duration"})

# Keys whose VALUES are schemas (recurse into them).
_SCHEMA_VALUED = ("items", "additionalProperties", "not")
# Keys whose values are DICTS OF schemas (recurse into each value).
_SCHEMA_MAP_VALUED = ("properties", "$defs", "definitions", "patternProperties")
# Keys whose values are LISTS OF schemas (recurse into each element).
_SCHEMA_LIST_VALUED = ("anyOf", "allOf", "oneOf", "prefixItems")


def to_anthropic_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of `schema` that Claude's structured outputs accept.

    Pure: no network, no SDK import, no mutation of the input. That is what lets
    `tests/test_llm_schema.py` assert the contract against all 10 real schemas
    (7 artifact specs + plan/module/lesson) without a key or a live call.
    """
    return _clean(schema)


def _clean(node: Any) -> Any:
    if isinstance(node, list):
        return [_clean(v) for v in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED:
            continue
        if key == "format":
            if value in _SAFE_FORMATS:
                out[key] = value
            continue
        if key in _SCHEMA_MAP_VALUED and isinstance(value, dict):
            out[key] = {k: _clean(v) for k, v in value.items()}
        elif key in _SCHEMA_LIST_VALUED and isinstance(value, list):
            out[key] = [_clean(v) for v in value]
        elif key in _SCHEMA_VALUED:
            out[key] = _clean(value)
        else:
            out[key] = _clean(value)

    # Every object node must forbid extra keys, and must declare every property
    # it defines as required. See the module docstring for why we force the
    # latter rather than trusting an undocumented leniency.
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
        props = out.get("properties")
        if isinstance(props, dict):
            out["required"] = list(props.keys())
        elif "required" not in out:
            out["required"] = []
    return out


def unsupported_keywords(schema: dict[str, Any]) -> list[str]:
    """Every unsupported keyword still present, as dotted paths. Used by the
    test to prove the sanitizer is exhaustive *recursively* — a `$defs` branch
    it forgot to walk would otherwise sail through, because the top level looks
    clean.

    MUST be schema-aware, not a blind key scan. Inside a `properties` (or
    `$defs`) map the keys are PROPERTY NAMES, not JSON-Schema keywords — and
    `TabSpec` really does have a property named `title`, which is also the name
    of a keyword we strip. A blind scan flags it as a leftover; a blind
    *stripper* would delete the tab's title field outright and nothing would
    fail until a tutor noticed his tabs had lost their names. Hence the
    `in_name_map` flag: the same string means two different things depending on
    where it sits.
    """
    found: list[str] = []

    def walk(node: Any, path: str, in_name_map: bool = False) -> None:
        if isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
            return
        if not isinstance(node, dict):
            return

        for key, value in node.items():
            if not in_name_map:
                if key in _UNSUPPORTED:
                    found.append(f"{path}.{key}")
                if key == "format" and value not in _SAFE_FORMATS:
                    found.append(f"{path}.format={value}")
            walk(value, f"{path}.{key}", in_name_map=key in _SCHEMA_MAP_VALUED)

        if not in_name_map and (
            node.get("type") == "object" or "properties" in node
        ) and node.get("additionalProperties") is not False:
            found.append(f"{path}: additionalProperties is not false")

    walk(schema, "$")
    return found
