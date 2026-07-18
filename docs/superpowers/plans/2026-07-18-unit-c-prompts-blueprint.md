# Plan C — Prompt Visibility (C1) + The Lesson Blueprint (C2)

**Date:** 2026-07-18
**Spec:** `docs/superpowers/specs/2026-07-18-curriculum-authoring-control-design.md` — Unit C, plus its Global Constraints.
**Repo:** `/mnt/nvme2TB/guitar_tutor` (FastAPI `apps/api` + Next.js `apps/web`).

---

## Goal

Turn the hardcoded 8-section lesson skeleton into **per-curriculum DATA** (a "blueprint"), make it tutor-editable at both the settings-default level and per-curriculum (wizard), and surface the curriculum-shaping prompts as an editable "Curriculum" group — **without changing a single byte of what today's engine sends**. The default blueprint reproduces today's `LESSON_DRAFT_SCHEMA` exactly, and a course carrying no blueprint drafts identically to before.

## Architecture

The whole change is a **lift-into-data** of four module-level constants in `apps/api/app/curriculum/depth.py` (`SECTION_WEIGHTS` :64, `SECTIONS` :74, `LESSON_DRAFT_SCHEMA` :137-223) and `apps/api/app/curriculum/draft.py` (`SECTION_LABELS` :425-446), plus the machinery that reads them (`measure()` :271, `persist_lesson()` :482, `_section_minutes()` :451, the citation iterators :253-292).

Data flow (unchanged shape, blueprint threaded exactly like `course_brief`):

```
interview "structure" step ─┐
settings blueprint_default ─┼─► materialize_outline(..., blueprint=…) ─► course.meta["blueprint"]
code default_blueprint() ───┘                                                    │
                                                                                 ▼
run_curriculum_draft_job Phase A: blueprint_from_course_meta(course.meta)  ──► plan["blueprint"]
                                                                                 │  (alongside plan["course_brief"])
                                                                                 ▼
_draft_one ─► draft_lesson(..., blueprint) ─► guided_json(build_lesson_schema(blueprint))
                                            ─► measure(lesson, blueprint, …)
                                            ─► persist_lesson(..., blueprint)  (labels/keys/order/audience)
```

Two resolution functions, and the difference between them is invariant #3:

- **`default_blueprint()`** — pure code default, built once from `depth.py`'s constants. This is the **draft-path fallback** for any course whose `meta` has no `"blueprint"` (i.e. every course that exists today). It never reads the DB.
- **`resolve_default_blueprint(db)`** — the `blueprint_default` table row if the tutor edited the settings default, else `default_blueprint()`. Used **only** to seed a *new* course at `materialize_outline` time and by the Settings editor. Editing this **never** touches an existing course, because existing courses either carry their own frozen `meta["blueprint"]` or fall back to the code default — never to this table.

C1 needs **no new override system** (invariant #10): every curriculum/lesson prompt entry already owns a whole-text `replace` `Slice` (verified: `curriculum.outline`, `curriculum.extend`, `curriculum.refine`, `lesson.draft`, `lesson.deepen`, `lesson.retrieved`, `lesson.repair`, `lesson.tier_library`, `lesson.gap`, `lesson.tier_web` all have `slices=(Slice(... kind="replace"),)`), and `overrides.validate` already refuses an edit that drops a `{var}`. C1 is: a `curriculum_group` flag on `PromptEntry`, a grouped view in `prompt-list.tsx`, and a step-3 deep-link button.

## Tech Stack

- **Backend:** Python 3, FastAPI, SQLAlchemy (`Block.meta` is plain `sa.JSON`, no `MutableDict` — reassign the whole dict on every write), Alembic (additive only), `pytest`.
- **Frontend:** Next.js (App Router), React, `next-intl`, Playwright.
- **LLM:** provider-agnostic via `get_provider()` (`app/llm/factory.py`); CPU-only; no GPU.

## Global Constraints (bind every task — these are the point)

1. **Golden identity.** `build_lesson_schema(default_blueprint())` **must deep-equal** a frozen copy of today's `LESSON_DRAFT_SCHEMA` — key order, nested `required` lists, `additionalProperties: False`, and every `description` string byte-for-byte. A golden test asserts this against a captured constant.
2. **Byte-identical regression.** A course whose `meta` has **no `"blueprint"`** drafts exactly as today: the draft path resolves `blueprint_from_course_meta(meta)` → `default_blueprint()`, and every downstream value (`measure`, section keys/order, `SECTION_LABELS`, `_section_minutes`) is identical to the current module-constant behaviour.
3. **Settings default never mutates existing courses.** New courses freeze `resolve_default_blueprint(db)` into their own `meta["blueprint"]` at materialize time; existing courses fall back to the **code** default (`default_blueprint()`), not the settings table. Editing `blueprint_default` changes only future courses.
4. **`exercises` and `qa` are structural.** `kind ∈ {prose, exercises, qa}`. Only `prose` sections may be added/removed/renamed. The two structured sections (`kind: exercises`, `kind: qa`) keep their fixed `items[]` schema, keep their keys, and may only be **reweighted / re-audienced / reordered / disabled** — never renamed, never kind-changed, never both removed. Each section carries `audience ∈ {teacher, student, both}`.
5. **Everything reads the blueprint.** `measure(lesson, blueprint, *, teaching_minutes)`, the section keys/order, `SECTION_LABELS`, `_section_minutes`, and the citation iterators are all **functions of the blueprint**, not module constants. The old module constants remain **only** as the seed of `default_blueprint()`.
6. **Threaded like `course_brief`.** `blueprint` rides the draft fan-out `plan` dict (`jobs/curriculum_draft.py` :341-353), resolved once in Phase A where a session is legitimately held, handed to workers as plain data.
7. **Wizard step is OPTIONAL + SKIPPABLE**, pre-filled with `resolve_default_blueprint(db)`. Its result lives in `interview.answers["structure"]` and is written to `course.meta["blueprint"]` at materialize. Skip → the settings default is used.
8. **Re-draft is OPT-IN.** A blueprint edit **never** re-drafts. Only an explicit button (reusing the resume requeue) rewrites existing lessons.
9. **CPU-only, provider-agnostic, additive migration.** All LLM via `get_provider()`; `Block.meta` whole-dict reassignment; migration chains from the current head.
10. **No new override system for C1.** Editable static prose writes through the existing `prompt_override` / `Slice` mechanism; `{var}` slots render as read-only chips (existing span rendering).

## Resolved design calls (controller, 2026-07-18)

The planner flagged five open questions; all are settled — implementers follow these:

1. **Wizard step placement — KEEP** `who→duration→scope→structure→sources→outline→confirm` (structure between scope and sources). It doesn't disturb the `sources→outline` auto-regenerate handoff, and the blueprint shapes lesson drafting, not the outline.
2. **C1 granularity — NONE new.** The existing whole-text `replace` `Slice` + `{var}`-as-read-only-chip rendering IS the vehicle (invariant #10). No per-static-run override system.
3. **`audience` — forward-looking plumbing ONLY.** Store it in the blueprint and on each segment's `meta`; build NO print/handout UI in this unit. It has no consumer yet by design.
4. **`blueprint_default` history — NONE.** Reset = delete the row; the git-backed code default is the restore target. No history table.
5. **Golden/label fidelity — LOAD-BEARING, source don't transcribe.** Do NOT retype the description or label strings from this plan's summary table (it is illustrative). Extract the 8 prose/body descriptions AND the el/en section labels into NAMED CONSTANTS in `depth.py`/`draft.py` (single source of truth), and have `blueprint._DEFAULT_SECTIONS` REFERENCE those constants — so `default_blueprint()` is byte-identical to today *by construction*, not by careful typing. The schema golden test (Task 1) and the persist-label regression (Task 2 — which MUST capture the CURRENT `SECTION_LABELS` values before the refactor and assert against them) are the backstops. A reworded string that silently changes what the model is sent is the one defect this unit must never ship.

## Migration chain

Current Alembic head is **`c4d9e1f7a230`** (`c4d9e1f7a230_concept_canon.py` — verified: it is nobody's `down_revision`). The one new migration in this plan chains `down_revision = "c4d9e1f7a230"`.

## Blueprint data shape (canonical)

```json
{
  "version": 1,
  "sections": [
    {"key": "warm_up", "label": {"el": "Ζέσταμα", "en": "Warm-up"},
     "description": "5 minutes of playing to open the session. Concrete: ...",
     "weight": 0.07, "kind": "prose", "audience": "teacher", "enabled": true}
  ]
}
```

Default section order + kind + weight + audience + label + description (this is exactly `depth.py` today; the strings below are load-bearing for the golden test):

| key | kind | weight | audience | label el / en |
|---|---|---|---|---|
| warm_up | prose | 0.07 | teacher | Ζέσταμα / Warm-up |
| theory | prose | 0.25 | teacher | Θεωρία / Theory |
| demonstration | prose | 0.18 | teacher | Επίδειξη / Demonstration |
| exercises | exercises | 0.22 | student | Ασκήσεις / Exercises |
| common_mistakes | prose | 0.10 | teacher | Συνήθη λάθη / Common mistakes |
| recap | prose | 0.06 | student | Ανακεφαλαίωση / Recap |
| homework | prose | 0.06 | student | Εργασία για το σπίτι / Homework |
| qa_prompts | qa | 0.06 | teacher | Ερωτήσεις & συζήτηση / Q&A and discussion |

`prose` descriptions are the exact strings passed to `_prose_section(...)` at `depth.py:145-191`. The `exercises`/`qa` section `description` maps to the **`body`** field description ("Prose introducing and sequencing the exercises." / "How to open the 10-minute discussion block."); their nested `items[]` schema (including item-level descriptions) stays as code constants in the structured builders, keyed by `kind`.

---

# TASK 1 — Blueprint data + `build_lesson_schema` + golden test

Lift the section skeleton into data. Pure Python, no DB, no threading yet. This task's whole job is invariant #1.

### Files
- **Modify:** `apps/api/app/curriculum/depth.py`
- **Create:** `apps/api/app/curriculum/blueprint.py`
- **Create test:** `apps/api/tests/test_blueprint_schema_golden.py`

### Interfaces
Produces:
- `app.curriculum.blueprint.default_blueprint() -> dict` — the canonical default (deep-copied on every call, never a shared mutable).
- `app.curriculum.blueprint.build_lesson_schema(blueprint: dict) -> dict` — the guided-json schema.
- `app.curriculum.blueprint.enabled_sections(blueprint: dict) -> list[dict]` — enabled sections in order.
- `app.curriculum.blueprint.section_keys(blueprint: dict) -> tuple[str, ...]`.
- `app.curriculum.blueprint.section_weights(blueprint: dict) -> dict[str, float]`.
- `app.curriculum.blueprint.section_labels(blueprint: dict, lang: str) -> dict[str, str]`.
- `app.curriculum.blueprint.validate_blueprint(raw: object) -> dict` — raises `BlueprintInvalid(code, **detail)`.

Consumes: `depth._prose_section`, `depth._CITATIONS`, and the two structured section builders (moved/kept in `depth.py`).

### Steps

1. **Write the failing golden test.** Capture today's schema BEFORE touching `depth.py` by importing the current constant and freezing a literal copy in the test file. Create `apps/api/tests/test_blueprint_schema_golden.py`:

```python
import copy
from app.curriculum.blueprint import default_blueprint, build_lesson_schema

# FROZEN COPY of app/curriculum/depth.py::LESSON_DRAFT_SCHEMA as of 2026-07-18.
# If depth.py's schema legitimately changes, this literal changes in the SAME commit
# and the reviewer sees exactly what moved. Paste the current dict here verbatim.
GOLDEN_LESSON_SCHEMA = { ... }   # <-- paste the exact dict from depth.py:137-223

def test_build_lesson_schema_of_default_equals_todays_schema():
    assert build_lesson_schema(default_blueprint()) == GOLDEN_LESSON_SCHEMA

def test_default_blueprint_is_deep_copied_each_call():
    a = default_blueprint(); b = default_blueprint()
    a["sections"][0]["weight"] = 999
    assert b["sections"][0]["weight"] != 999

def test_required_lists_are_exact():
    schema = build_lesson_schema(default_blueprint())
    assert schema["required"] == ["title", "summary", "warm_up", "theory",
        "demonstration", "exercises", "common_mistakes", "recap", "homework", "qa_prompts"]
    assert schema["exercises"["properties"] if False else schema["properties"]["exercises"]["required"] == ["body", "items", "citations"]
```
   (Fix the last assertion to `schema["properties"]["exercises"]["required"]` when pasting — shown deliberately verbose so the intent is unmissable: nested `required` order must match.)

   To produce `GOLDEN_LESSON_SCHEMA` exactly, run and paste:
```
cd apps/api && python -c "import json; from app.curriculum.depth import LESSON_DRAFT_SCHEMA as S; print(json.dumps(S, ensure_ascii=False, indent=4))"
```
   Convert that JSON to a Python literal (`false`→`False`) in the test.

2. **Run — expect ImportError** (`blueprint.py` does not exist):
```
cd apps/api && python -m pytest tests/test_blueprint_schema_golden.py -q
```
   Expected: `ModuleNotFoundError: No module named 'app.curriculum.blueprint'`.

3. **Implement `depth.py` builders.** Keep `_prose_section`/`_CITATIONS` as-is. Add two structured builders so the schema can be assembled per-kind (extract today's inline `exercises`/`qa_prompts` object literals verbatim into functions):

```python
def _exercises_section(body_description: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": body_description},
            "items": {
                "type": "array",
                "description": "Each exercise the student actually plays.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "instructions": {"type": "string"},
                        "est_minutes": {"type": "integer"},
                    },
                    "required": ["title", "instructions", "est_minutes"],
                    "additionalProperties": False,
                },
            },
            "citations": _CITATIONS,
        },
        "required": ["body", "items", "citations"],
        "additionalProperties": False,
    }

def _qa_section(body_description: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": body_description},
            "items": {
                "type": "array",
                "description": (
                    "Questions to put to the student, EACH WITH AN ANSWER KEY — "
                    "the tutor is holding this page while the student answers."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "answer_key": {"type": "string"},
                    },
                    "required": ["question", "answer_key"],
                    "additionalProperties": False,
                },
            },
            "citations": _CITATIONS,
        },
        "required": ["body", "items", "citations"],
        "additionalProperties": False,
    }
```
   Keep the module-level `LESSON_DRAFT_SCHEMA`, `SECTION_WEIGHTS`, `SECTIONS`, `SECTION_LABELS` for now (Task 2 removes the reads; here nothing else must break). Rebuild `LESSON_DRAFT_SCHEMA`'s `exercises`/`qa_prompts` entries to call the two new functions so there is one source for their shape (still byte-identical). Export `_KIND_BUILDERS = {"exercises": _exercises_section, "qa": _qa_section}` and the `_TITLE_PROP`/`_SUMMARY_PROP` literals (`{"type": "string"}` and the summary object with its description).

4. **Implement `blueprint.py`:**

```python
from __future__ import annotations
import copy
from app.curriculum import depth

class BlueprintInvalid(Exception):
    def __init__(self, code: str, **detail):
        super().__init__(code); self.code = code; self.detail = {"code": code, **detail}

BLUEPRINT_VERSION = 1
_KIND_STRUCTURED = ("exercises", "qa")

# THE code default. Descriptions/labels/weights/audience are exactly depth.py today.
_DEFAULT_SECTIONS = [
    {"key": "warm_up", "label": {"el": "Ζέσταμα", "en": "Warm-up"},
     "description": <the exact _prose_section arg for warm_up>,
     "weight": 0.07, "kind": "prose", "audience": "teacher", "enabled": True},
    # theory 0.25 teacher, demonstration 0.18 teacher …
    {"key": "exercises", "label": {"el": "Ασκήσεις", "en": "Exercises"},
     "description": "Prose introducing and sequencing the exercises.",
     "weight": 0.22, "kind": "exercises", "audience": "student", "enabled": True},
    # common_mistakes 0.10 teacher, recap 0.06 student, homework 0.06 student,
    {"key": "qa_prompts", "label": {"el": "Ερωτήσεις & συζήτηση", "en": "Q&A and discussion"},
     "description": "How to open the 10-minute discussion block.",
     "weight": 0.06, "kind": "qa", "audience": "teacher", "enabled": True},
]

def default_blueprint() -> dict:
    return {"version": BLUEPRINT_VERSION, "sections": copy.deepcopy(_DEFAULT_SECTIONS)}

def enabled_sections(bp: dict) -> list[dict]:
    return [s for s in bp["sections"] if s.get("enabled", True)]

def section_keys(bp: dict) -> tuple[str, ...]:
    return tuple(s["key"] for s in enabled_sections(bp))

def section_weights(bp: dict) -> dict[str, float]:
    return {s["key"]: float(s["weight"]) for s in enabled_sections(bp)}

def section_labels(bp: dict, lang: str) -> dict[str, str]:
    fb = "el"  # i18n rule: never default to English
    return {s["key"]: s["label"].get(lang) or s["label"].get(fb) or s["key"]
            for s in bp["sections"]}

def build_lesson_schema(bp: dict) -> dict:
    props = {"title": copy.deepcopy(depth._TITLE_PROP),
             "summary": copy.deepcopy(depth._SUMMARY_PROP)}
    for s in enabled_sections(bp):
        if s["kind"] == "prose":
            props[s["key"]] = depth._prose_section(s["description"])
        else:
            props[s["key"]] = depth._KIND_BUILDERS[s["kind"]](s["description"])
    return {"type": "object", "properties": props,
            "required": ["title", "summary", *[s["key"] for s in enabled_sections(bp)]],
            "additionalProperties": False}
```
   `validate_blueprint(raw)` enforces: `version == 1`; `sections` a non-empty list; each section has a slug `key` (`^[a-z][a-z0-9_]*$`), unique across sections; `label` a dict with non-empty `el` and `en` strings; `description` a non-empty str; `weight` a float in `[0, 1]`; `kind ∈ {prose, exercises, qa}`; `audience ∈ {teacher, student, both}`; `enabled` a bool. Structural rules (invariant #4): there must be **exactly one** `kind == "exercises"` section keyed `"exercises"` and **exactly one** `kind == "qa"` section keyed `"qa_prompts"`, both present (may be `enabled: false` but not removed/renamed). On any violation raise `BlueprintInvalid(code, ...)` with codes `bad_version`, `no_sections`, `bad_key`, `dup_key`, `bad_label`, `bad_description`, `bad_weight`, `bad_kind`, `bad_audience`, `structured_section_missing`, `structured_section_renamed`. Return a normalized deep-copied dict.

5. **Run — expect green:**
```
cd apps/api && python -m pytest tests/test_blueprint_schema_golden.py -q
```
   Expected: `3 passed`. If the golden diff fails, `json.dumps` both sides and diff — the failure is almost always a `description` string or a nested `required` order.

6. **Commit:** `feat(curriculum): lift the lesson section skeleton into a blueprint (build_lesson_schema == today's schema)`

---

# TASK 2 — `measure` / labels / keys / citation-iterators become functions of the blueprint (byte-identical regression)

Make the drafting/measuring code read a blueprint argument, defaulting to `default_blueprint()`. No storage, no threading through jobs yet — the fan-out still passes the default. Invariant #2 + #5.

### Files
- **Modify:** `apps/api/app/curriculum/depth.py` (`measure`, `section_words`)
- **Modify:** `apps/api/app/curriculum/draft.py` (`_iter_citation_lists`, `invalid_citations`, `strip_invalid_citations`, `draft_lesson`, `build_lesson_messages`, `persist_lesson`, `_section_minutes`, `SECTION_LABELS` usage)
- **Modify:** `apps/api/app/jobs/curriculum_draft.py` (`_draft_one` passes `default_blueprint()` for now)
- **Create test:** `apps/api/tests/test_blueprint_regression.py`

### Interfaces
Produces (new signatures):
- `depth.measure(lesson: dict, blueprint: dict, *, teaching_minutes: int) -> Measurement`
- `draft.draft_lesson(db, *, ctx, library, language, blueprint, student_brief=None, course_brief=None, source_ids=None, prompts=None) -> tuple[dict, Measurement]`
- `draft.build_lesson_messages(*, ctx, library, language, blueprint, student_brief, course_brief, retrieved=None, deepen=None, previous=None, source=None) -> list[dict]`
- `draft.persist_lesson(db, lesson_block, lesson, m, library, blueprint, *, qa_minutes, teaching_minutes) -> None`
- `draft.invalid_citations(lesson, library, blueprint) -> list[...]`, `draft.strip_invalid_citations(lesson, library, blueprint) -> dict`

Consumes: `app.curriculum.blueprint`.

### Steps

1. **Write the failing regression test.** It asserts the default-blueprint path is identical to the frozen behaviour:

```python
from app.curriculum.blueprint import default_blueprint, build_lesson_schema
from app.curriculum.depth import measure, LESSON_DRAFT_SCHEMA

def _lesson():  # a fully-populated sample lesson dict (all 8 sections)
    ...

def test_schema_from_default_equals_module_constant():
    assert build_lesson_schema(default_blueprint()) == LESSON_DRAFT_SCHEMA

def test_measure_with_default_blueprint_matches_legacy_numbers():
    bp = default_blueprint(); lesson = _lesson()
    m = measure(lesson, bp, teaching_minutes=40)
    # legacy expected numbers computed by hand / captured before the refactor:
    assert m.total_words == <captured>
    assert m.per_section.keys() == {"warm_up","theory","demonstration","exercises",
        "common_mistakes","recap","homework","qa_prompts"}
    assert m.thin_sections == <captured>

def test_persist_lesson_labels_come_from_blueprint(db_session):
    # persist a lesson under default_blueprint(); assert segment titles are the
    # exact SECTION_LABELS Greek/English strings and meta["section"] keys/order match.
    ...
```
   Capture `<captured>` by running `measure` on `_lesson()` under the CURRENT code path first (temporary print), before changing signatures.

2. **Run — expect failure** (signature mismatch: `measure()` takes no `blueprint`):
```
cd apps/api && python -m pytest tests/test_blueprint_regression.py -q
```
   Expected: `TypeError: measure() takes ...` / missing-arg errors.

3. **Implement.** In `depth.py`:
   - `measure(lesson, blueprint, *, teaching_minutes)`: iterate `blueprint.section_keys(blueprint)` for `per_section`; read weights from `blueprint.section_weights(blueprint)`; keep `_SECTION_THIN_RATIO` and the `summary` word add unchanged. Import `blueprint` lazily inside the function to avoid a cycle (`from app.curriculum import blueprint as bp`).
   - `section_words` unchanged (already section-shape agnostic).

   In `draft.py`:
   - `_iter_citation_lists(lesson, blueprint)` iterates `bp.section_keys(blueprint)` instead of `SECTIONS`.
   - `invalid_citations`/`strip_invalid_citations` take `blueprint` and pass it down.
   - `build_lesson_schema` is called in `draft_lesson`: `schema = build_lesson_schema(blueprint)`; replace the three `provider.guided_json(..., LESSON_DRAFT_SCHEMA, ...)` calls with `..., schema, ...`.
   - `draft_lesson` threads `blueprint` into `measure(lesson, blueprint, teaching_minutes=…)`, `invalid_citations(..., blueprint)`, and both `build_lesson_messages(..., blueprint=blueprint, ...)` calls.
   - `build_lesson_messages` gains a `blueprint` param (kept for symmetry + future per-section prose; it does not change the tail text today — the descriptions reach the model through the schema).
   - `persist_lesson(..., blueprint, *, qa_minutes, teaching_minutes)`: replace `for name in SECTIONS` with `for name in bp.section_keys(blueprint)`; replace `SECTION_LABELS[lang][name]` with `bp.section_labels(blueprint, lang)[name]`; write `meta={"section": name, "audience": <section audience>, "citations": cites}`.
   - `_section_minutes(name, teaching_minutes, qa_minutes, blueprint)`: qa special-case becomes `if <section kind is qa>: return max(1, qa_minutes)`; else `round(weight * teaching_minutes)` reading `bp.section_weights(blueprint)[name]`.
   - Keep the module-level `SECTION_LABELS`/`SECTIONS`/`SECTION_WEIGHTS` constants **only** if still imported elsewhere; grep and delete dead imports. (`registry.py` imports `SECTIONS`? No — it imports `LESSON_TAIL` etc. Confirm with grep; `depth.SECTIONS` is imported by `draft.py:44`.)

   In `jobs/curriculum_draft.py` `_draft_one`: import `default_blueprint`, and pass `blueprint=default_blueprint()` to `draft_lesson(...)` and `persist_lesson(...)`. (Task 3 replaces this with `plan["blueprint"]`.)

4. **Run — expect green:**
```
cd apps/api && python -m pytest tests/test_blueprint_regression.py tests/test_blueprint_schema_golden.py -q
cd apps/api && python -m pytest tests/ -q -k "draft or lesson or curriculum or prompt"
```
   Expected: all pass. The existing prompt-registry byte-identity tests (`test_prompts_byte_identity.py`) must stay green — `LESSON_TAIL` is untouched.

5. **Commit:** `refactor(curriculum): measure/labels/keys/citations read a blueprint (default == today, byte-identical)`

---

# TASK 3 — Storage (`blueprint_default` table + migration) + threading through materialize & the draft fan-out + backward-compat regression

Persist a per-curriculum blueprint on `course.meta["blueprint"]`, resolve it in the fan-out, and add the settings-default table. Invariants #2, #3, #6.

### Files
- **Create:** `apps/api/app/models/blueprint_default.py`
- **Create:** `apps/api/alembic/versions/<newrev>_blueprint_default.py`
- **Create:** `apps/api/app/curriculum/blueprint_store.py` (resolution: table row vs code default)
- **Modify:** `apps/api/app/curriculum/blueprint.py` (`blueprint_from_course_meta`)
- **Modify:** `apps/api/app/curriculum/outline.py` (`materialize_outline` gains `blueprint=`)
- **Modify:** `apps/api/app/jobs/curriculum_draft.py` (Phase A resolves blueprint into `plan`)
- **Modify:** `apps/api/app/curriculum/interview.py` (`_answer_confirm` passes the blueprint)
- **Create test:** `apps/api/tests/test_blueprint_storage.py`

### Interfaces
Produces:
- `models.blueprint_default.BlueprintDefault` — single-row table, `id` singleton PK, `blueprint: JSON`.
- `blueprint_store.resolve_default_blueprint(db) -> dict` (table row if present, else `default_blueprint()`).
- `blueprint_store.save_default_blueprint(db, bp: dict) -> None`, `blueprint_store.reset_default_blueprint(db) -> bool`.
- `blueprint.blueprint_from_course_meta(meta: dict | None) -> dict` — `meta["blueprint"]` if present else `default_blueprint()` (**code** default — invariant #3).
- `materialize_outline(..., blueprint: dict | None = None)` — writes `course.meta["blueprint"] = blueprint or resolve_default_blueprint(db)`.

### Steps

1. **Write the failing storage/regression test:**

```python
def test_course_without_blueprint_falls_back_to_code_default():
    from app.curriculum.blueprint import blueprint_from_course_meta, default_blueprint
    assert blueprint_from_course_meta({"brief": "x"}) == default_blueprint()
    assert blueprint_from_course_meta(None) == default_blueprint()

def test_editing_settings_default_does_not_touch_a_blueprintless_course(db_session):
    from app.curriculum import blueprint_store as store
    custom = default_blueprint(); custom["sections"].append(<a new prose section>)
    store.save_default_blueprint(db_session, custom)
    # a course row with NO meta["blueprint"] STILL resolves to the CODE default:
    assert blueprint_from_course_meta({"brief": "x"}) == default_blueprint()

def test_materialize_seeds_the_resolved_settings_default(db_session):
    # save a custom settings default, materialize a tiny outline with blueprint=None,
    # assert course.meta["blueprint"] == that custom default (frozen).
    ...

def test_resolve_default_reads_table_then_code(db_session):
    from app.curriculum import blueprint_store as store
    assert store.resolve_default_blueprint(db_session) == default_blueprint()
    store.save_default_blueprint(db_session, <custom>)
    assert store.resolve_default_blueprint(db_session) == <custom normalized>
    assert store.reset_default_blueprint(db_session) is True
    assert store.resolve_default_blueprint(db_session) == default_blueprint()
```

2. **Run — expect failure** (`blueprint_default` table / module missing):
```
cd apps/api && python -m pytest tests/test_blueprint_storage.py -q
```

3. **Implement the model** (`models/blueprint_default.py`) — mirror `prompt_override`'s "empty means code default" property, single row:

```python
from sqlalchemy import JSON, Integer, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base
from app.models.base import TimestampMixin

class BlueprintDefault(Base, TimestampMixin):
    """The tutor's edited SETTINGS default blueprint — the default for NEW curricula.
    ONE row (id=1). Absent row => the code default (`blueprint.default_blueprint()`),
    which is in git and is what Reset restores — same design as prompt_override:
    this table holds only the DELTA."""
    __tablename__ = "blueprint_default"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    blueprint: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (CheckConstraint("id = 1", name="blueprint_default_singleton"),)
```

4. **Write the migration** `alembic/versions/<newrev>_blueprint_default.py`, `down_revision = "c4d9e1f7a230"`, additive only:

```python
revision = "b1c2d3e4f5a6"          # any fresh 12-hex id
down_revision = "c4d9e1f7a230"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "blueprint_default",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("blueprint", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("id = 1", name="blueprint_default_singleton"),
    )

def downgrade() -> None:
    op.drop_table("blueprint_default")
```
   (Match `TimestampMixin`'s actual column names/defaults — check `app/models/base.py` and copy its `created_at`/`updated_at` definitions verbatim so `alembic check` is clean.)

5. **Implement `blueprint_store.py`** and `blueprint.blueprint_from_course_meta`:

```python
# blueprint_store.py
from app.curriculum.blueprint import default_blueprint, validate_blueprint
from app.models.blueprint_default import BlueprintDefault

def resolve_default_blueprint(db) -> dict:
    row = db.get(BlueprintDefault, 1) if db is not None else None
    return row.blueprint if row is not None else default_blueprint()

def save_default_blueprint(db, bp: dict) -> None:
    bp = validate_blueprint(bp)
    row = db.get(BlueprintDefault, 1)
    if row is None:
        db.add(BlueprintDefault(id=1, blueprint=bp))
    else:
        row.blueprint = bp          # plain JSON: whole-value reassignment is fine (scalar)
    db.commit()

def reset_default_blueprint(db) -> bool:
    row = db.get(BlueprintDefault, 1)
    if row is None: return False
    db.delete(row); db.commit(); return True
```
```python
# blueprint.py addition
def blueprint_from_course_meta(meta: dict | None) -> dict:
    bp = (meta or {}).get("blueprint")
    return bp if isinstance(bp, dict) and bp.get("sections") else default_blueprint()
```

6. **Thread into `materialize_outline`** (`outline.py:333`): add param `blueprint: dict | None = None`; inside the `course = Block(...)` `meta={...}` dict add `"blueprint": blueprint or resolve_default_blueprint(db)`. Import `resolve_default_blueprint` at top. (Whole-dict `meta=` assignment already — the new key rides along.)

7. **Thread into the draft fan-out** (`jobs/curriculum_draft.py`): in `run_curriculum_draft_job` Phase A, after `meta = course.meta or {}`, add `blueprint = blueprint_from_course_meta(meta)`; add `"blueprint": blueprint` to the `plan` dict (:341). In `_draft_one`, replace the Task-2 `default_blueprint()` with `plan["blueprint"]` in both `draft_lesson(...)` and `persist_lesson(...)`.

8. **Thread into interview confirm** (`interview.py` `_answer_confirm` :518): resolve the blueprint from answers-or-settings and pass it:
```python
structure = (interview.answers.get("structure") or {})
chosen_bp = structure.get("blueprint")           # set by the (optional) structure step
blueprint = chosen_bp if isinstance(chosen_bp, dict) else resolve_default_blueprint(db)
root_id = materialize_outline(db, interview.outline, ..., blueprint=blueprint, ...)
```
   (The non-interview creation path `generate_curriculum_endpoint` in `routers/curriculum.py` also calls `materialize_outline` — leave `blueprint=None` there so it seeds the settings default.)

9. **Run — expect green:**
```
cd apps/api && alembic upgrade head && alembic check
cd apps/api && python -m pytest tests/test_blueprint_storage.py tests/test_blueprint_regression.py -q
cd apps/api && python -m pytest tests/ -q -k "curriculum or draft or interview or outline"
```

10. **Commit:** `feat(curriculum): persist per-course blueprint + settings default table; thread through the draft fan-out`

---

# TASK 4 — Settings blueprint API (GET/PUT/DELETE the default)

Expose `blueprint_default` over HTTP so the Settings UI (Task 6) and the wizard prefill (Task 5) can read/write it. Follows the `routers/prompts.py` `code`-error convention.

### Files
- **Create:** `apps/api/app/routers/blueprint.py`
- **Modify:** `apps/api/app/main.py` (register the router)
- **Create test:** `apps/api/tests/test_blueprint_api.py`

### Interfaces
Produces:
- `GET /blueprint/default -> {blueprint, is_override}` — the resolved default + whether a row exists.
- `GET /blueprint/code-default -> {blueprint}` — always the code default (for the UI's "restore" preview and diffing).
- `PUT /blueprint/default {blueprint} -> {blueprint, is_override:true}` — validate + save.
- `DELETE /blueprint/default -> {blueprint, is_override:false}` — reset to code default.

Validation errors return `422 {"detail": {"code": ...}}` from `BlueprintInvalid.detail`, exactly like `put_slice`.

### Steps
1. **Write the failing test** (`test_blueprint_api.py`): GET returns code default + `is_override:false`; PUT a custom → GET returns it + `is_override:true`; PUT an invalid blueprint (e.g. renamed `exercises` key) → 422 with `{"code": "structured_section_renamed"}`; DELETE → GET back to code default. Use the app `TestClient`.
2. **Run — expect 404** (route unregistered).
3. **Implement `routers/blueprint.py`** using `blueprint_store` + `validate_blueprint`, catching `BlueprintInvalid` → `HTTPException(422, detail=e.detail)`. No extra auth (single session gate, per `routers/prompts.py` docstring). Register in `main.py` beside the prompts router.
4. **Run — expect green:** `python -m pytest tests/test_blueprint_api.py -q`.
5. **Commit:** `feat(api): /blueprint/default read/edit/reset endpoints`

---

# TASK 5 — Wizard "Lesson structure" step (optional, skippable, pre-filled)

Insert a new interview step between `scope` and `sources`. Backend state machine + frontend step + dialog wiring + spec. Invariant #7.

### Files
- **Modify:** `apps/api/app/curriculum/interview.py` (`STEP_ORDER`, `describe_step`, `_answer_structure`, `answer_interview` dispatch)
- **Modify:** `apps/api/app/models/interview.py` (`STEPS` tuple)
- **Create:** `apps/web/src/components/curriculum/interview-structure-step.tsx`
- **Modify:** `apps/web/src/components/curriculum/interview-dialog.tsx` (`STEP_ORDER`, step dispatch)
- **Modify:** `apps/web/src/lib/api.ts` (interview types: `findings.blueprint`)
- **Create test:** `apps/api/tests/test_interview_structure_step.py`

### Interfaces
- New step key `"structure"`. `STEP_ORDER = ["who","duration","scope","structure","sources","outline","confirm"]` (both API and web).
- `describe_step` for `"structure"` returns `{question, options: null, findings: {"blueprint": resolve_default_blueprint(db)}}` — the pre-fill.
- `_answer_structure(db, interview, answer)`: accepts `{"skip": true}` (store nothing / `{"skip": true}` and advance) **or** `{"blueprint": {...}}` (validate via `validate_blueprint`; on `BlueprintInvalid` re-ask with a short message; on success store `interview.answers["structure"] = {"blueprint": <normalized>}` and advance). Placement between `scope` and `sources` keeps the existing `sources → outline` auto-regenerate handoff in the dialog untouched.

### Steps
1. **Write the failing backend test:** start an interview, drive who→duration→scope, assert `describe_step` now yields `step == "structure"` with `findings.blueprint == resolve_default_blueprint(db)`; `{"skip": true}` advances to `sources` and leaves `answers` without a custom blueprint; a `{"blueprint": <custom>}` advances to `sources` and stores it; an invalid blueprint re-asks and leaves `step == "structure"`. Also assert confirm materializes `course.meta["blueprint"]` == the custom blueprint when structure supplied one, else the settings default.
2. **Run — expect failure** (`structure` not in `STEP_ORDER`; `describe_step` has no branch → falls to the terminal default).
3. **Implement backend:** add `"structure"` to `interview.STEP_ORDER` (after `"scope"`) and to `models/interview.py::STEPS`; add the `describe_step` branch and `_answer_structure`; wire the dispatch in `answer_interview` (`elif step == "structure": result = _answer_structure(db, interview, answer)`). Import `resolve_default_blueprint` and `validate_blueprint`.
4. **Implement the frontend step** `interview-structure-step.tsx` — a thin wrapper that renders the shared `BlueprintEditor` (Task 6) seeded from `findings.blueprint`, plus two buttons: **"Use the standard structure" (Skip)** → `onSubmit({ skip: true })`, and **"Use this structure" (Continue)** → `onSubmit({ blueprint })`. `data-testid`s: `interview-structure-step`, `interview-structure-skip`, `interview-answer-submit` (reuse the shared submit testid used by other steps). Copy the props convention from `interview-duration-step.tsx` (`submitting`, `error`, `onSubmit`).
5. **Wire the dialog:** add `"structure"` to `STEP_ORDER` (:42) and a `{state.step === "structure" && <InterviewStructureStep .../>}` block in the dispatch (:299-363), passing `blueprint={findings?.blueprint}`. Add `blueprint?: BlueprintShape` to the interview `findings` type in `lib/api.ts`.
6. **Run — expect green:**
```
cd apps/api && python -m pytest tests/test_interview_structure_step.py -q
cd apps/web && npx tsc --noEmit
```
7. **Commit:** `feat(curriculum): optional skippable "Lesson structure" wizard step`

---

# TASK 6 — Settings blueprint editor UI (the shared `BlueprintEditor`)

The reusable editor component, mounted in Settings against `blueprint_default`, and reused by Task 5's wizard step. Confirm-gated restore.

### Files
- **Create:** `apps/web/src/components/settings/blueprint-editor.tsx` (the shared editor)
- **Create:** `apps/web/src/components/settings/blueprint-default-card.tsx` (Settings wrapper: load/save/restore against `/blueprint/default`)
- **Modify:** `apps/web/src/app/[locale]/(cockpit)/settings/page.tsx` (mount the card)
- **Modify:** `apps/web/src/lib/api.ts` (`getBlueprintDefault`, `saveBlueprintDefault`, `resetBlueprintDefault`, `getBlueprintCodeDefault`, `BlueprintShape` types)
- **Modify:** `apps/web/src/messages/{el,en}.json` (`blueprint.*` strings + error codes)
- **Create test:** `apps/web/tests/blueprint-settings.spec.ts`

### Interfaces
`BlueprintEditor({ value, onChange })` — controlled. Renders one row per section: label (el/en) inputs (prose only editable; structured `exercises`/`qa_prompts` labels read-only), `description` textarea, `weight` number, `audience` select (`teacher|student|both`), `enabled` toggle, reorder up/down, and **Remove** (prose only). An **Add section** button appends a new `prose` section with a blank slug/label. The two structured sections render with a lock affordance and no Remove/rename (invariant #4). No weight-sum enforcement (weights are relative thin-detection shares).

### Steps
1. **Write the failing Playwright spec** (`blueprint-settings.spec.ts`, mock `/blueprint/*`): the card loads the code default (8 sections); editing a prose `description` + Save issues `PUT /blueprint/default` with the mutated blueprint; **Add section** then Save sends 9 sections; the `exercises` row shows no Remove button and its key input is read-only; **Restore** opens the confirm modal (`ui/confirm.tsx`) and on accept issues `DELETE /blueprint/default`; an invalid save (server 422 `structured_section_renamed`) renders exactly one Greek sentence via `t.has(...)`.
2. **Run — expect failure** (component/card absent).
3. **Implement** `blueprint-editor.tsx` (pure controlled UI) + `blueprint-default-card.tsx` (fetch on mount, optimistic Save, confirm-gated Restore — mirror `prompt-list.tsx`'s `SliceEditor` Save/Reset/verdict pattern and `useConfirm`). Add the `lib/api.ts` client fns + types. Mount the card in `settings/page.tsx` near `PromptList`. Add `blueprint.*` message keys (title, help, section field labels, `add`, `remove`, `restore`, `restoreConfirmTitle/Body`, `audience.*`, and `errors.<code>` for each `BlueprintInvalid` code) to both locale files.
4. **Run — expect green:**
```
cd apps/web && npx tsc --noEmit && npx playwright test blueprint-settings.spec.ts
```
5. **Commit:** `feat(settings): tutor-editable default lesson blueprint (add/remove/reweight prose sections; structured locked)`

---

# TASK 7 — C1: Curriculum prompt group + assembled view + wizard step-3 button

Mark the curriculum-crucial prompts, render them as a "Curriculum" group in Settings, and deep-link to it from the scope step. No new override system — the existing `Slice`/span machinery already provides the assembled view with read-only `{var}` chips and editable static prose.

### Files
- **Modify:** `apps/api/app/prompts/registry.py` (`curriculum_group: bool` on `PromptEntry`; set True on the C1 subset)
- **Modify:** `apps/api/app/routers/prompts.py` (`curriculum_group` on `PromptSummary`/`_summary`)
- **Modify:** `apps/web/src/lib/api.ts` (`curriculum_group` on `PromptSummary`)
- **Modify:** `apps/web/src/components/settings/prompt-list.tsx` (a "Curriculum" group at the top, filtered by `curriculum_group`)
- **Modify:** `apps/web/src/components/curriculum/interview-scope-step.tsx` (step-3 deep-link button)
- **Modify:** `apps/api/tests/test_prompts_registry.py` (assert the curriculum subset is exactly the intended set)
- **Modify/Create:** `apps/web/tests/prompts-curriculum-group.spec.ts`

### Interfaces
- `PromptEntry.curriculum_group: bool = False`. Set `True` on exactly: `curriculum.system`, `curriculum.library`, `curriculum.outline`, `curriculum.extend`, `lesson.draft`, `lesson.tier_library`, `lesson.tier_web`, `lesson.gap`, `lesson.deepen`, `lesson.repair` (the spec's C1 list).
- `PromptSummary.curriculum_group: bool` surfaced by `GET /prompts`.
- Step-3 button: on `interview-scope-step.tsx`, a secondary button "Δες / άλλαξε τα prompts που θα φτιάξουν αυτό το πρόγραμμα" linking to `/{locale}/settings?promptGroup=curriculum`. `prompt-list.tsx` reads `?promptGroup=curriculum` (via `useSearchParams`) to auto-open the Curriculum group and scroll to it.

### Steps
1. **Write the failing tests.** Backend `test_prompts_registry.py`: assert `{e.id for e in REGISTRY.values() if e.curriculum_group}` equals the intended set exactly (guards against drift — a new curriculum prompt added without the flag fails here). Frontend `prompts-curriculum-group.spec.ts` (mock `/prompts` + `/prompts/{id}`): a "Curriculum" group appears listing exactly the subset; opening `curriculum.outline` shows the rendered prompt with a read-only span chip and an editable textarea (existing `slice-*` testids); editing + Save issues `PUT /prompts/slices/curriculum.outline` (mutation proof); navigating with `?promptGroup=curriculum` opens the group.
2. **Run — expect failure** (`curriculum_group` attribute unknown).
3. **Implement backend:** add the field (default `False`) to `PromptEntry`, set it on the ten entries, surface it in `_summary`/`PromptSummary`. This is additive — the completeness/`is`-identity tests are untouched.
4. **Implement frontend:** in `prompt-list.tsx`, before the flow groups, render a synthetic **Curriculum** `Collapsible` whose items are `(prompts ?? []).filter(p => p.curriculum_group)` in registration order (dropping provider-mismatched ones exactly as the flow loop does); reuse `PromptRow` unchanged. Read `?promptGroup=curriculum` to default it open + `scrollIntoView`. Add the deep-link button to `interview-scope-step.tsx` (a `next/link` or router push; it lives beside Continue and does not submit the form).
5. **Run — expect green:**
```
cd apps/api && python -m pytest tests/test_prompts_registry.py -q
cd apps/web && npx tsc --noEmit && npx playwright test prompts-curriculum-group.spec.ts
```
6. **Commit:** `feat(settings): Curriculum prompt group + wizard step-3 deep-link`

---

# TASK 8 — Opt-in "Re-draft under the current structure" button

A blueprint edit must **never** re-draft (invariant #8). This adds the ONLY path that rewrites existing lessons under a changed blueprint: an explicit, count-aware button reusing the resume requeue.

### Files
- **Modify:** `apps/api/app/routers/curriculum.py` (new `POST /curricula/{root_id}/redraft`)
- **Create:** `apps/api/app/curriculum/redraft.py` (requeue helper) — or inline if trivial
- **Modify:** `apps/web/src/components/curriculum/tree-board.tsx` (or the detail screen from Unit A) — the button + confirm
- **Modify:** `apps/web/src/lib/api.ts` (`redraftCurriculum`)
- **Create test:** `apps/api/tests/test_redraft_endpoint.py`

### Interfaces
- `POST /curricula/{root_id}/redraft -> 202 JobAccepted`. Behaviour: for every **non-gap** lesson under the root, set `meta = {**meta, "draft_status": "queued", "error": None}` (whole-dict reassignment), then schedule `run_curriculum_draft_job` (the existing fan-out, which reads `course.meta["blueprint"]`). This mirrors `resume_curriculum_draft` (:369) and `deepen_lesson` (:476) but flips **ready** lessons back to `queued` too — that is the point, and it is why it is a separate, explicitly-named route, not `/draft`.
- The button is count-aware: it shows "Re-draft N lessons under the current structure?" in a confirm modal (`ui/confirm.tsx`) with `destructive: true`, and only fires on accept.

### Steps
1. **Write the failing endpoint test:** a course with 3 `ready` lessons; `POST /redraft` returns 202, all three lessons flip to `queued`, and a `run_curriculum_draft_job` is scheduled. Assert it does **not** fire on any blueprint save (there is no coupling — the blueprint routes never call requeue).
2. **Run — expect 404** (route absent).
3. **Implement** the requeue (guard: `_get_block_or_404`, `course.kind == "course"`; iterate module→lesson blocks, whole-dict `meta` reassignment, `db.commit()`), create the `GenerationJob(kind="curriculum_draft", params={"root_id": ...})`, `background_tasks.add_task(run_curriculum_draft_job, job.id)`. Add the confirm-gated button on the curriculum detail screen; wire `redraftCurriculum(rootId)` in `lib/api.ts`.
4. **Run — expect green:** `python -m pytest tests/test_redraft_endpoint.py -q`.
5. **Commit:** `feat(curriculum): opt-in "re-draft under current structure" (never auto-fires on a blueprint edit)`

---

# TASK 9 — Refresh `interview.spec.ts` mocks/testids (green the wizard)

The interview e2e specs are currently RED from **stale mock fixtures** (production is fine): the mocked `sources`-step response returns `findings.shape` as a **string**, but `interview-sources-step.tsx:66-71` reads `shape.lessons_total/modules/lessons_per_module.join("+")/target_words_per_lesson/teaching_minutes/qa_minutes` — a string has no `.lessons_per_module`, so the step throws. Task 5 also inserts a new `structure` step the current mock/`startToStep` do not walk through. This task makes the specs green for both.

### Files
- **Modify:** `apps/web/tests/interview.spec.ts`

### Steps
1. **Reproduce red:** `cd apps/web && npx playwright test interview.spec.ts` — expect failures at the sources step (`shape.lessons_per_module` undefined) and, after Task 5, an extra unhandled step.
2. **Fix the `shape` fixture:** in `mockInterviewApi`, the `step === "scope"` → `sources` response must return `findings.shape` as the **object** the real `describe_step` returns:
```js
findings: { shape: { lessons_total: 3, modules: 1, lessons_per_module: [3],
  target_words_per_lesson: 2200, teaching_minutes: 40, qa_minutes: 10 } }
```
3. **Add the `structure` step** to the mock's step machine (between `scope` and `sources`): the `scope` answer now advances to `step = "structure"` returning `findings: { blueprint: <a default-shaped blueprint object> }`; a `structure` answer (`{skip:true}` or `{blueprint}`) advances to `sources`. Add the corresponding leg to the `startToStep` helper: after filling the scope brief and submitting, click `interview-structure-skip` (skip) before the sources step.
4. **Reconcile moved testids.** Commit `787f0d8` moved some rename/delete affordances into a `⋯` menu — run the spec and, for any interaction that no longer resolves (e.g. a rename/delete that is now behind a menu button), open the menu first (`getByTestId("...-menu")` → the item) rather than clicking a now-nonexistent direct testid. Grep the current components for the live testids (`grep -rn "data-testid" apps/web/src/components/curriculum/interview-outline-step.tsx` and the board card) and match the spec to them; do **not** invent testids — use what the components render today.
5. **Run — expect green:** `npx playwright test interview.spec.ts`. (`confirm.spec.ts` / `artifacts.spec.ts` are tracked separately — out of scope here.)
6. **Commit:** `test(web): refresh interview mocks (object shape + structure step) and moved testids`

---

## Ordered task list

1. Blueprint data + `build_lesson_schema` + golden test
2. `measure`/labels/keys/citations become blueprint functions (byte-identical regression)
3. Storage (`blueprint_default` table + migration) + threading through materialize & fan-out + backward-compat regression
4. Settings blueprint API (GET/PUT/DELETE default)
5. Wizard "Lesson structure" step (optional, skippable, pre-filled)
6. Settings blueprint editor UI (shared `BlueprintEditor`)
7. C1: Curriculum prompt group + assembled view + wizard step-3 button
8. Opt-in "Re-draft under the current structure" button
9. Refresh `interview.spec.ts` mocks/testids

## Notes for the executor
- Run each task's Python tests with `cd apps/api && python -m pytest`, frontend with `cd apps/web && npx tsc --noEmit && npx playwright test <spec>`.
- After Task 3, `alembic upgrade head` runs in the api boot CMD — never let the DB get ahead of the image; the migration is additive and reversible.
- Whole-dict `Block.meta` reassignment everywhere (invariant #9); never `meta["k"] = v`.
