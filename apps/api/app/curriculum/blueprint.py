"""The lesson BLUEPRINT: the 8-section lesson skeleton, lifted out of code and into
DATA so a tutor can reshape it per curriculum without touching the engine.

`default_blueprint()` is the code default — the single source for every course that
carries no blueprint of its own (i.e. every course that exists today). It is built
from the SAME constants `app.curriculum.depth` already owns — the section
`description` strings (`depth._DESC_*`), the weights (`depth.SECTION_WEIGHTS`) and
the el/en labels (`depth.SECTION_LABELS`) — so that `build_lesson_schema(default_blueprint())`
reproduces `depth.LESSON_DRAFT_SCHEMA` byte-for-byte BY CONSTRUCTION, not by careful
re-typing. That invariant is the whole point of this unit, and
`tests/test_blueprint_schema_golden.py` guards it. (`depth` is a pure module — no
retrieval/LLM imports — which is why this module imports only from it, not from
`app.curriculum.draft`; see `depth.SECTION_LABELS`'s own comment for why.)

A blueprint is plain JSON (see the plan's canonical shape):

    {"version": 1, "sections": [
        {"key": "warm_up", "label": {"el": "...", "en": "..."},
         "description": "...", "weight": 0.07, "kind": "prose",
         "audience": "teacher", "enabled": true}, ...]}

`kind` is one of `prose | exercises | qa`. Only `prose` sections may be renamed.
The two STRUCTURED sections (`exercises`, `qa`) keep their fixed `items[]` schema
built by `depth._KIND_BUILDERS` — they may be reweighted, re-audienced, reordered,
disabled, or REMOVED ENTIRELY (2026-07-19 follow-up: full tutor control, not just
enable/disable). What is still forbidden: two sections of the same structured kind
(`validate_blueprint`'s `structured_section_duplicate`), and a structured section
present under any key but its canonical one (`structured_section_renamed`) — the
model-facing schema and every downstream consumer (`_section_minutes`,
`persist_lesson`'s labels) key structured sections by kind, dispatching through a
fixed key, not a free-form one. A blueprint must always keep at least one ENABLED
section (`no_enabled_sections`). `structured_section_template(kind)` returns the
canonical section dict a re-add restores.
"""
from __future__ import annotations

import copy

from app.curriculum import depth
from app.curriculum.depth import SECTION_LABELS as _LABELS

BLUEPRINT_VERSION = 1

_KIND_PROSE = "prose"
_KIND_STRUCTURED = ("exercises", "qa")
_ALL_KINDS = (_KIND_PROSE, *_KIND_STRUCTURED)
_AUDIENCES = ("teacher", "student", "both")

# The two structured kinds, keyed by kind -> the one key each is allowed to carry
# WHEN PRESENT. Neither is required to exist any more (2026-07-19: full delete is
# legal); `validate_blueprint` only rejects a DUPLICATE of a kind or a present
# section under the wrong key.
_STRUCTURED_KEYS = {"exercises": "exercises", "qa": "qa_prompts"}

import re as _re

_KEY_RE = _re.compile(r"^[a-z][a-z0-9_]*$")


class BlueprintInvalid(Exception):
    """A blueprint failed validation. `code` is a stable machine string the API
    surfaces as `422 {"detail": {"code": ...}}` and the UI maps to one message."""

    def __init__(self, code: str, **detail):
        super().__init__(code)
        self.code = code
        self.detail = {"code": code, **detail}


def _label(key: str) -> dict:
    """This section's el/en label, sourced from `depth.SECTION_LABELS` — the same
    strings that land on `Block.title` — so the two can never disagree."""
    return {"el": _LABELS["el"][key], "en": _LABELS["en"][key]}


# THE code default. Every string/weight/label here REFERENCES the constant that is
# its single source of truth — nothing is transcribed. Order, kind, weight, audience
# and label are exactly `depth.py` today; see the module docstring.
_DEFAULT_SECTIONS: list[dict] = [
    {"key": "warm_up", "label": _label("warm_up"),
     "description": depth._DESC_WARM_UP,
     "weight": depth.SECTION_WEIGHTS["warm_up"], "kind": "prose",
     "audience": "teacher", "enabled": True},
    {"key": "theory", "label": _label("theory"),
     "description": depth._DESC_THEORY,
     "weight": depth.SECTION_WEIGHTS["theory"], "kind": "prose",
     "audience": "teacher", "enabled": True},
    {"key": "demonstration", "label": _label("demonstration"),
     "description": depth._DESC_DEMONSTRATION,
     "weight": depth.SECTION_WEIGHTS["demonstration"], "kind": "prose",
     "audience": "teacher", "enabled": True},
    {"key": "exercises", "label": _label("exercises"),
     "description": depth._DESC_EXERCISES_BODY,
     "weight": depth.SECTION_WEIGHTS["exercises"], "kind": "exercises",
     "audience": "student", "enabled": True},
    {"key": "common_mistakes", "label": _label("common_mistakes"),
     "description": depth._DESC_COMMON_MISTAKES,
     "weight": depth.SECTION_WEIGHTS["common_mistakes"], "kind": "prose",
     "audience": "teacher", "enabled": True},
    {"key": "recap", "label": _label("recap"),
     "description": depth._DESC_RECAP,
     "weight": depth.SECTION_WEIGHTS["recap"], "kind": "prose",
     "audience": "student", "enabled": True},
    {"key": "homework", "label": _label("homework"),
     "description": depth._DESC_HOMEWORK,
     "weight": depth.SECTION_WEIGHTS["homework"], "kind": "prose",
     "audience": "student", "enabled": True},
    {"key": "qa_prompts", "label": _label("qa_prompts"),
     "description": depth._DESC_QA_BODY,
     "weight": depth.SECTION_WEIGHTS["qa_prompts"], "kind": "qa",
     "audience": "teacher", "enabled": True},
]


def default_blueprint() -> dict:
    """The canonical code default, DEEP-COPIED on every call so callers never share a
    mutable — a course that freezes this into its `meta` must own its own object."""
    return {"version": BLUEPRINT_VERSION, "sections": copy.deepcopy(_DEFAULT_SECTIONS)}


def blueprint_from_course_meta(meta: dict | None) -> dict:
    """The blueprint a course DRAFTS FROM: its own frozen `meta["blueprint"]` if it
    has one, else the CODE default — never the settings table (spec invariant #3).

    This is the draft-path resolver, and its fallback being `default_blueprint()`
    rather than `blueprint_store.resolve_default_blueprint(db)` is the entire reason a
    settings-default edit cannot mutate an existing or blueprintless course. Every
    course that exists today has no `meta["blueprint"]`, so it drafts byte-identically
    to before (invariant #2). A missing, non-dict, or section-less value is treated as
    absent — an honest fall-through to the default, never a half-blueprint.
    """
    bp = (meta or {}).get("blueprint")
    return bp if isinstance(bp, dict) and bp.get("sections") else default_blueprint()


# ---------------------------------------------------------------------------
# Reading a blueprint
# ---------------------------------------------------------------------------

def enabled_sections(bp: dict) -> list[dict]:
    """The sections that actually make it into a lesson, in order. A disabled
    section is kept in the blueprint (so it can be re-enabled) but drafted nowhere."""
    return [s for s in bp["sections"] if s.get("enabled", True)]


def section_keys(bp: dict) -> tuple[str, ...]:
    """Enabled section keys, in order — the replacement for `depth.SECTIONS`."""
    return tuple(s["key"] for s in enabled_sections(bp))


def section_weights(bp: dict) -> dict[str, float]:
    """`{key: weight}` for the enabled sections — the replacement for
    `depth.SECTION_WEIGHTS` in `measure` and `_section_minutes`."""
    return {s["key"]: float(s["weight"]) for s in enabled_sections(bp)}


def section_labels(bp: dict, lang: str) -> dict[str, str]:
    """`{key: label}` in `lang`, for ALL sections. i18n rule: never default to
    English — fall back to `el`, then to the key, but never silently to `en`."""
    fb = "el"
    return {
        s["key"]: (s["label"].get(lang) or s["label"].get(fb) or s["key"])
        for s in bp["sections"]
    }


def build_lesson_schema(bp: dict) -> dict:
    """The guided-json schema for `bp` — the replacement for the module-level
    `depth.LESSON_DRAFT_SCHEMA`. For the default blueprint it reproduces that schema
    byte-for-byte (the golden test)."""
    props: dict = {
        "title": copy.deepcopy(depth._TITLE_PROP),
        "summary": copy.deepcopy(depth._SUMMARY_PROP),
    }
    keys: list[str] = []
    for s in enabled_sections(bp):
        key = s["key"]
        keys.append(key)
        if s["kind"] == _KIND_PROSE:
            props[key] = depth._prose_section(s["description"])
        else:
            props[key] = depth._KIND_BUILDERS[s["kind"]](s["description"])
    return {
        "type": "object",
        "properties": props,
        "required": ["title", "summary", *keys],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_blueprint(raw: object) -> dict:
    """Validate and NORMALIZE an incoming blueprint; return a fresh deep-copied dict.

    Raises `BlueprintInvalid(code, **detail)` on the first violation. Codes:
    `bad_version`, `no_sections`, `bad_key`, `dup_key`, `bad_label`,
    `bad_description`, `bad_weight`, `bad_kind`, `bad_audience`, `bad_enabled`,
    `structured_section_duplicate`, `structured_section_renamed`,
    `no_enabled_sections`.
    """
    if not isinstance(raw, dict):
        raise BlueprintInvalid("no_sections")
    if raw.get("version") != BLUEPRINT_VERSION:
        raise BlueprintInvalid("bad_version", version=raw.get("version"))

    sections = raw.get("sections")
    if not isinstance(sections, list) or not sections:
        raise BlueprintInvalid("no_sections")

    normalized: list[dict] = []
    seen: set[str] = set()
    for s in sections:
        if not isinstance(s, dict):
            raise BlueprintInvalid("bad_key", key=None)

        key = s.get("key")
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise BlueprintInvalid("bad_key", key=key)
        if key in seen:
            raise BlueprintInvalid("dup_key", key=key)
        seen.add(key)

        label = s.get("label")
        if (
            not isinstance(label, dict)
            or not isinstance(label.get("el"), str) or not label["el"].strip()
            or not isinstance(label.get("en"), str) or not label["en"].strip()
        ):
            raise BlueprintInvalid("bad_label", key=key)

        description = s.get("description")
        if not isinstance(description, str) or not description.strip():
            raise BlueprintInvalid("bad_description", key=key)

        weight = s.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise BlueprintInvalid("bad_weight", key=key, weight=weight)
        weight = float(weight)
        if not (0.0 <= weight <= 1.0):
            raise BlueprintInvalid("bad_weight", key=key, weight=weight)

        kind = s.get("kind")
        if kind not in _ALL_KINDS:
            raise BlueprintInvalid("bad_kind", key=key, kind=kind)

        audience = s.get("audience")
        if audience not in _AUDIENCES:
            raise BlueprintInvalid("bad_audience", key=key, audience=audience)

        enabled = s.get("enabled", True)
        if not isinstance(enabled, bool):
            raise BlueprintInvalid("bad_enabled", key=key, enabled=enabled)

        normalized.append({
            "key": key,
            "label": {"el": label["el"], "en": label["en"]},
            "description": description,
            "weight": weight,
            "kind": kind,
            "audience": audience,
            "enabled": enabled,
        })

    # Revised structural rule (2026-07-19: full delete + re-add is now legal).
    # A structured kind may be ABSENT entirely, but never DUPLICATED, and a section
    # of that kind — if present — must carry the canonical key `_KIND_BUILDERS`
    # dispatches on (renaming would silently desync it from every by-name consumer:
    # `_section_minutes`, `persist_lesson`'s labels).
    for kind, expected_key in _STRUCTURED_KEYS.items():
        matches = [s for s in normalized if s["kind"] == kind]
        if len(matches) > 1:
            raise BlueprintInvalid(
                "structured_section_duplicate", kind=kind, count=len(matches),
            )
        if matches and matches[0]["key"] != expected_key:
            raise BlueprintInvalid(
                "structured_section_renamed",
                kind=kind, key=matches[0]["key"], expected=expected_key,
            )

    # A blueprint that drafts NOTHING is not a blueprint — at least one section
    # must remain enabled (an all-disabled or fully-emptied-of-enabled-rows edit
    # is rejected outright, same posture as `no_sections`).
    if not any(s["enabled"] for s in normalized):
        raise BlueprintInvalid("no_enabled_sections", count=len(normalized))

    return {"version": BLUEPRINT_VERSION, "sections": normalized}


def structured_section_template(kind: str) -> dict:
    """The canonical default section dict for a STRUCTURED `kind` (`"exercises"` or
    `"qa"`) — what the frontend's "Add Exercises" / "Add Q&A" re-add restores after
    a delete. DEEP-COPIED from `_DEFAULT_SECTIONS`, the SAME source
    `default_blueprint()` reads, so its label/description/weight/audience can never
    drift from the code default — source, don't retype (the rule this whole module
    is built around).
    """
    if kind not in _KIND_STRUCTURED:
        raise ValueError(f"not a structured kind: {kind!r}")
    for s in _DEFAULT_SECTIONS:
        if s["kind"] == kind:
            return copy.deepcopy(s)
    raise AssertionError(f"no default section of kind {kind!r}")  # unreachable
