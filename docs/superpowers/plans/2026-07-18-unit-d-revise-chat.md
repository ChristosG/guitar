# Plan D — Revise Engine (D1) + Per-Curriculum Chat Drawer (D2)

**Date:** 2026-07-18
**Spec:** `docs/superpowers/specs/2026-07-18-curriculum-authoring-control-design.md` — Unit D (D1 revise backend + D2 per-curriculum chat), plus the Global Constraints.
**Repo:** `/mnt/nvme2TB/guitar_tutor` (FastAPI `apps/api` + Next.js `apps/web`).
**Builds after:** Unit A (the `curricula/[rootId]` detail route exists) and Unit C (the draft fan-out reads `course.meta["blueprint"]`; revised lessons draft under the course blueprint for free).

---

## Goal

Give the tutor a conversational way to revise a *finished* curriculum — "should I add a module on the Tube Screamer?" → a grounded answer → "yes, and one on the DS-1 too" → a refined **plan** → "give me the new curriculum" → approve → apply. The plan is computed by the model reading the actual tree + the tutor's own books; **it mutates nothing** until the tutor approves. Approval applies the ops in one transaction and re-uses the ordinary draft fan-out so the new/changed lessons fill in against the warm library cache and the progress bar moves.

The whole feature is **PLAN → PREVIEW → APPROVE → APPLY**. It reuses, without re-inventing: the grounding prefix (`build_curriculum_context` + `prefix_messages`), the positional insert edit services (`edit.add_lesson`/`add_module`), the draft fan-out (`run_curriculum_draft_job`), the `GenerationJob` async pattern, and the chat HITL machinery (`ApprovalCard`, `resolve_approval`, job polling).

## Architecture

```
                        ┌─────────────────────────────  D1 (backend)  ─────────────────────────────┐
tutor instruction ─►  revise.plan_revision(db, root_id, instruction)          [MUTATES NOTHING]
                        │  build_curriculum_context(source_ids) + prefix_messages   (same warm cache)
                        │  + compact_tree_text(db, course)   (ids+title+objective+summary, NO bodies)
                        │  → get_provider().guided_json(REVISION_PLAN_SCHEMA, role="plan")
                        │  → validate_ops(db, root_id, raw)   (every *_id resolved to a real block
                        │                                       under this root, else dropped + logged)
                        ▼
                     RevisionPlan {summary, ops:[ {op, reason, …} ]}   ← previewed to the tutor
                        │  approve
                        ▼
                     revise.apply_revision(db, root_id, plan)          [ONE transaction]
                        │  insert_lesson → edit._add_lesson (no-commit core, positional `after`)
                        │  insert_module → edit._add_module (no-commit core)
                        │  move_lesson   → edit._move_block  (NEW — renormalises BOTH parents)
                        │  modify_lesson → requeue queued + meta["revise_instruction"] (+ prev_body)
                        │  remove_lesson → db.delete + renormalise parent
                        │  new/changed lessons → draft_status="queued"   (whole-dict meta reassignment)
                        ▼  db.commit()  (once)
                     chain run_curriculum_draft_job(root_id)   ← reads course.meta["blueprint"] (Unit C)
                                                                  → progress bar fills the new lessons

  transport:  POST /curricula/{root}/revise  → 202 + GenerationJob(kind="curriculum_revise")
              run_curriculum_revise_job:  no "plan" in params → run planner, store plan in job.progress
                                          "plan" in params     → apply + chain draft

                        ┌─────────────────────────────  D2 (chat)  ────────────────────────────────┐
propose_curriculum_revision(root_id, instruction)   READ tool  → returns the plan (inline planner call)
apply_curriculum_revision(root_id, plan)            MUTATION tool, async_job, job_kind="curriculum_revise"
                        │  suspends → ApprovalRequest.tool_args = {root_id, plan}
                        │  RevisionPlanCard (ApprovalCard variant) renders per-op reasons + Approve/Reject
                        │  approve → resolve_approval async branch → run_curriculum_revise_job (apply)
                        ▼
              right-side DRAWER on curricula/[rootId]  (not a new route)
```

The chat session is **bound to one curriculum**: a new nullable `chat_session.root_id` column, set when the drawer opens the session. `chat.py` transiently injects a compact-tree + brief context block onto the last user turn (never persisted, never in the cached system+tools prefix) so the model reasons about *this* curriculum and passes the right `root_id`.

## Tech Stack

- **Backend:** Python 3, FastAPI, SQLAlchemy (`Block.meta` and `GenerationJob.params/progress` are plain `sa.JSON`, no `MutableDict` — reassign the whole dict on every write), Alembic (additive only), `pytest`.
- **Frontend:** Next.js (App Router), React, `next-intl`, Playwright.
- **LLM:** provider-agnostic via `get_provider().guided_json(messages, schema, role="plan")` (`app/llm/factory.py`); CPU-only; no GPU.

## Global Constraints (bind every task — these are the point)

1. **Plan ≠ mutation, always.** `revise.plan_revision(...)` and `propose_curriculum_revision(...)` MUST NOT write anything (no `db.add`, no `db.commit`, no status flip). Apply is a separate, explicit, approval-gated call. Keep this boundary crisp in every task: a test in D1a asserts `plan_revision` leaves the tree byte-identical.
2. **Every `*_id` validated before the plan is returned.** `validate_ops(db, root_id, raw)` resolves every `module_id`/`lesson_id`/`after_lesson_id`/`after_module_id`/`to_module_id` against the real blocks under this root, of the right `kind`. Invalid ops are **dropped and logged**, never applied. The apply engine re-validates (defence in depth — the chat path carries the plan back through the model).
3. **Apply is ONE transaction.** `apply_revision` executes all ops on one Session and commits **once** at the end. It reuses commit-free cores of `edit.add_lesson`/`add_module` (refactored in D1b — public wrappers keep committing, existing tests unchanged) and the new `edit._move_block`. Then it chains `run_curriculum_draft_job` (a *separate* job/row, exactly like `module_generate`).
4. **New/changed lessons re-enter the draft queue.** `draft_status="queued"` via **whole-dict `meta` reassignment** (`Block.meta` is plain `sa.JSON`). The chained `run_curriculum_draft_job` reads `course.meta["blueprint"]` (Unit C) so revised lessons draft under the course's own blueprint automatically. Weeks re-derive with no week column (teaching order = `module.order` then `lesson.order`; `lessons_total` follows the tree).
5. **Grounding reused, never re-invented.** The planner rides `build_curriculum_context(db, source_ids)` + `prefix_messages(...)`. `source_ids` comes from `course.meta["source_ids"]` **preserving `None` vs `[]`** exactly as `extend.py:322-324` does (`None` = whole library; `[]` = deliberately none). The planner reads a **compact tree** (ids + title + objective + summary; NO lesson bodies — token discipline), except a `modify_lesson` op MAY include that one lesson's body.
6. **Citations unchanged.** Any drafted content goes through the existing `page_index` citation validation in `persist_lesson` (`draft.py`), inside the chained draft job. This plan adds no citation code.
7. **Chat reuses the existing infra.** Transcript, streaming, `ApprovalCard` HITL, job polling — all reused. TWO new tools only. Adding tools re-mints the chat system+tools cache prefix **once** (known, acceptable — noted in D2a).
8. **CPU-only, provider-agnostic.** The planner is `get_provider().guided_json(..., role="plan")`. No hardcoded `claude_cli`. Reuse `GenerationJob` with `kind="curriculum_revise"` — **no new table**. One additive migration only: a nullable `chat_session.root_id` column (a column, not a table — justified in D2a).
9. **`REVISION_PLAN_SCHEMA` is a flat tagged object, not `oneOf`.** Guided-JSON portability across providers (Claude CLI now, API later) is not guaranteed for `oneOf`/discriminated unions (see `depth.py`/`extend.py` — every existing schema is a flat object). Each op is one object with an `op` enum discriminator and all per-op fields optional; Python enforces the per-op required set in `validate_ops`. This is a deliberate, documented choice.

## Resolved design calls (controller, 2026-07-18)

The planner flagged three risks; a fourth is a scope addition from Chris. All settled — implementers follow these:

1. **`modify_lesson` = full re-draft — KEPT.** Queue the lesson + `meta["revise_instruction"]` + store `meta["prev_body"]` for one-step undo. REQUIREMENT: the `RevisionPlanCard` must label a modify op as **"Rewrite lesson «X»"** (not "edit"), so the tutor approves a rewrite knowingly.
2. **Synchronous inline planner for chat `propose` — KEPT.** The ship target is a local/bundled CPU app (no Cloudflare 100s cap), so a ~20-60s planner tool call is fine. REQUIREMENT: the drawer shows a clear "Planning the revision…" progress state. Async job+poll is the documented fallback only if a proxy timeout ever bites.
3. **Approved == applied must be EXACT.** VALIDATE the plan when apply is invoked (chat tool OR REST endpoint) BEFORE the ApprovalCard suspends; store the *validated* plan on the ApprovalRequest and render THAT; apply it VERBATIM. Ops dropped by id-validation must never reach the card. Apply-time re-validation stays as defense-in-depth but is a no-op when the card already showed the validated plan.
4. **NEW (Chris) — the chat/revise can also edit an existing curriculum's BLUEPRINT.** Add an `update_blueprint` op (see the op table). Apply writes `course.meta["blueprint"]` (whole-dict reassignment) after `blueprint.validate_blueprint`. **Re-drafting existing lessons under the new blueprint stays the OPT-IN button** (Unit C's `POST /curricula/{root_id}/redraft`) — a blueprint change NEVER auto-re-drafts. The `RevisionPlanCard` labels it clearly ("Change lesson structure: …") and notes existing lessons keep their content until re-drafted. Blueprint is now editable in three places: wizard (new), settings default (future), chat (existing).

## Op vocabulary (canonical — the schema and the validator both key off this)

| `op` | required fields (beyond `op`, `reason`) | applied by |
|---|---|---|
| `insert_lesson` | `module_id`, `title`, `objective`; optional `after_lesson_id` | `edit._add_lesson` |
| `insert_module` | `title`, `objective`, `tier`, `lessons[]` (`{title,objective}`); optional `after_module_id` | `edit._add_module` + queued lessons |
| `modify_lesson` | `lesson_id`, `instruction` | requeue + `meta["revise_instruction"]` |
| `move_lesson` | `lesson_id`, `to_module_id`; optional `after_lesson_id` | `edit._move_block` (NEW) |
| `remove_lesson` | `lesson_id` | `db.delete` + `_renormalise` |
| `update_blueprint` | `blueprint` (full blueprint object) | `blueprint.validate_blueprint` → `course.meta["blueprint"]` (whole-dict); **NO auto-redraft** (opt-in `/redraft`) |

Every op carries a human-readable `reason` (the "why it belongs"). `tier ∈ TIER_ORDER` (`outline.TIER_ORDER`), clamped to the course's `gap_policy` via `outline.clamp_tier` at apply time (mirrors `extend.generate_module_json:247-250`).

## Migration chain

Unit D adds ONE migration: the nullable `chat_session.root_id` column. It chains from **the current Alembic head at build time**, which will be **Unit C's `blueprint_default` migration** (Unit C is built before D). Get the exact id with `cd apps/api && .venv/bin/alembic heads` and set `down_revision` to it. (Do NOT hardcode `c4d9e1f7a230` — that is the head *before* Unit C.)

## Decomposition

- **D1a** — `revise.py`: `REVISION_PLAN_SCHEMA`, `compact_tree_text`, `build_revise_messages`, `plan_revision`, `validate_ops`; unit tests. (No mutation anywhere.)
- **D1b** — apply engine: `edit._move_block` + commit-free cores of `add_lesson`/`add_module`; `revise.apply_revision`; `_draft_one`/`_claim` `revise_instruction` threading; unit tests.
- **D1c** — `run_curriculum_revise_job` runner + `POST /curricula/{root_id}/revise` endpoint (202, plan vs apply modes); tests.
- **D2a** — the two agent tools + registry/completeness guards + `resolve_approval` `curriculum_revise` wiring + `chat_session.root_id` column/migration + context injection; tests.
- **D2b** — the revise drawer + `RevisionPlanCard` on `curricula/[rootId]`; tests.

---

# TASK D1a — The planner: schema, compact tree, `plan_revision`, `validate_ops`

Pure planning. Reads the tree + library, asks the model for a plan, validates ids. **Writes nothing.**

### Files
- **Create:** `apps/api/app/curriculum/revise.py`
- **Create test:** `apps/api/tests/test_revise_planner.py`

### Interfaces
Produces:
- `app.curriculum.revise.REVISION_PLAN_SCHEMA: dict` — flat guided-json schema (constraint #9).
- `app.curriculum.revise.compact_tree_text(db, course: Block) -> str` — the tree as `M<n> [id] title — objective` / `  L<n> [id] title — objective — summary`, NO lesson bodies.
- `app.curriculum.revise.build_revise_messages(*, course_title, brief, language, tree_text, instruction, library, source=None) -> list[dict]` — pure; `prefix_messages(library, source)` + one volatile user message.
- `app.curriculum.revise.validate_ops(db, root_id: uuid.UUID, raw: dict) -> dict` — returns `{summary, ops}` with invalid ops dropped + logged.
- `app.curriculum.revise.plan_revision(db, root_id: uuid.UUID, *, instruction: str) -> dict` — the whole read-only planner. Raises `ReviseError` for a missing/non-course root.

Consumes: `corpus.build_curriculum_context` (`:263`), `corpus.prefix_messages` (`:399`), `outline.TIER_ORDER`, `i18n.language_directive`/`answer_in`, `prompts.overrides.resolve`, `llm.factory.get_provider`, `models.block.Block`.

### Steps

1. **Write the failing test.** `apps/api/tests/test_revise_planner.py` — mirror `test_agent_tools.py`'s DB-reachable skip guard + `Base.metadata.create_all` setup. Seed a course→module→lesson tree via ORM (mirror `test_curriculum_api.py::_build_template_course`). Stub the provider so no live model is hit — monkeypatch `app.curriculum.revise.get_provider` to return a fake whose `guided_json(messages, schema, role)` returns a canned raw plan, and stub `build_curriculum_context` to a tiny `LibraryContext`.

```python
import copy, uuid
import pytest
from sqlalchemy import text
from app.db import Base, SessionLocal, engine
from app.models.block import Block
import app.curriculum.revise as revise

try:
    with engine.connect() as _c: _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)

def setup_module(_): Base.metadata.create_all(engine)

class _FakeProvider:
    def __init__(self, raw): self.raw = raw
    def guided_json(self, messages, schema, role="plan"): return copy.deepcopy(self.raw)

def _seed(db):
    course = Block(kind="course", title="Tone 101", is_template=True, language="el",
                   meta={"brief": "for gigging players", "source_ids": None})
    db.add(course); db.flush()
    m = Block(kind="module", title="Overdrive", parent_id=course.id, order=0,
              language="el", meta={"objective": "od pedals", "tier": "library"})
    db.add(m); db.flush()
    l = Block(kind="lesson", title="Tube Screamer", parent_id=m.id, order=0,
              language="el", body="the TS-808 mid hump", meta={"objective": "ts basics"})
    db.add(l); db.commit()
    return course, m, l

def test_plan_revision_returns_validated_ops_and_mutates_nothing(monkeypatch):
    db = SessionLocal()
    course, m, l = _seed(db)
    before = {b.id: (b.title, b.order, b.parent_id) for b in db.query(Block).all()}
    raw = {"summary": "add a DS-1 lesson",
           "ops": [
               {"op": "insert_lesson", "module_id": str(m.id), "after_lesson_id": str(l.id),
                "title": "DS-1", "objective": "distortion", "reason": "gap after TS"},
               {"op": "insert_lesson", "module_id": str(uuid.uuid4()),  # bogus module
                "title": "ghost", "objective": "x", "reason": "should be dropped"},
           ]}
    monkeypatch.setattr(revise, "get_provider", lambda: _FakeProvider(raw))
    from app.curriculum.corpus import LibraryContext
    monkeypatch.setattr(revise, "build_curriculum_context",
                        lambda db, sids: LibraryContext(text="", token_count=0, fits=True))
    plan = revise.plan_revision(db, course.id, instruction="add a DS-1 lesson")
    assert plan["summary"]
    assert len(plan["ops"]) == 1                      # bogus module op dropped
    assert plan["ops"][0]["module_id"] == str(m.id)
    after = {b.id: (b.title, b.order, b.parent_id) for b in db.query(Block).all()}
    assert after == before                            # constraint #1: nothing written
    db.close()

def test_compact_tree_has_ids_titles_objectives_no_bodies():
    db = SessionLocal(); course, m, l = _seed(db)
    txt = revise.compact_tree_text(db, course)
    assert str(m.id) in txt and str(l.id) in txt and "Tube Screamer" in txt
    assert "the TS-808 mid hump" not in txt           # body excluded (token discipline)
    db.close()
```

2. **Run — expect failure** (`ModuleNotFoundError: app.curriculum.revise`):

```bash
cd apps/api && .venv/bin/python -m pytest tests/test_revise_planner.py -q
```

3. **Implement `revise.py`.** Model the schema flat (constraint #9); build the compact tree; the pure message builder; the validator; the planner.

```python
"""REVISE — read a whole curriculum + the tutor's library, PLAN structural
changes, mutate nothing. Apply is a separate, approval-gated step (apply_revision).

The planner mirrors extend.generate_module_json: same warm prefix
(prefix_messages over build_curriculum_context), same role="plan" guided_json,
same None-vs-[] source_ids rule (extend.py:322-324). The one difference is the
task in the volatile tail: not "design one module" but "propose a list of ops on
THIS tree". The tree is serialised COMPACTLY — ids + titles + objectives +
one-line summaries, never lesson bodies — because the model needs to locate an
insertion point, not re-read 45,000 words it already wrote.

NOTHING HERE WRITES. plan_revision is a pure read. validate_ops resolves every id
against the live tree and DROPS what does not resolve, so a hallucinated module
id can never reach apply. apply_revision (edit.py / apply task) is the only writer.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.curriculum.corpus import build_curriculum_context, prefix_messages
from app.curriculum.outline import TIER_ORDER
from app.i18n import answer_in, language_directive
from app.llm.factory import get_provider
from app.models.block import Block
from app.prompts.overrides import resolve

log = logging.getLogger(__name__)


class ReviseError(ValueError):
    """A revise request the tree cannot accept (missing / not a course). The
    router and job runner turn it into a 404 / failed-job, never a 500."""


# Constraint #9: FLAT tagged object, not oneOf. `op` discriminates; every
# per-op field is optional at the schema level and enforced in validate_ops.
_OP_ENUM = ["insert_lesson", "insert_module", "modify_lesson", "move_lesson", "remove_lesson"]

REVISION_PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "one sentence: what this revision does and why"},
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": _OP_ENUM},
                    "reason": {"type": "string",
                               "description": "why this op belongs, in one sentence, for the tutor"},
                    "module_id": {"type": "string", "description": "insert_lesson: the target module id"},
                    "after_lesson_id": {"type": "string",
                                        "description": "insert_lesson/move_lesson: place AFTER this "
                                                       "lesson id, or omit to append"},
                    "after_module_id": {"type": "string",
                                        "description": "insert_module: place AFTER this module id, or omit"},
                    "to_module_id": {"type": "string", "description": "move_lesson: the destination module id"},
                    "lesson_id": {"type": "string",
                                  "description": "modify_lesson/move_lesson/remove_lesson: the target lesson id"},
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "instruction": {"type": "string",
                                    "description": "modify_lesson: what to change about the lesson"},
                    "tier": {"type": "string", "enum": list(TIER_ORDER),
                             "description": "insert_module: where its material comes from — say honestly"},
                    "lessons": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"title": {"type": "string"}, "objective": {"type": "string"}},
                            "required": ["title", "objective"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["op", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "ops"],
    "additionalProperties": False,
}

# Per-op required fields enforced in Python (constraint #9). Keys that must
# resolve to a live block of a given kind under this root.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "insert_lesson": ("module_id", "title", "objective"),
    "insert_module": ("title", "objective", "tier", "lessons"),
    "modify_lesson": ("lesson_id", "instruction"),
    "move_lesson": ("lesson_id", "to_module_id"),
    "remove_lesson": ("lesson_id",),
}


def compact_tree_text(db, course: Block) -> str:
    """The tree as the model needs it to locate an insertion point: every module
    and lesson with its id, title, objective and (for a lesson) its one-line
    summary — its `body`, which persist_lesson set from the draft's `summary`
    (draft.py:557). NEVER the full section bodies (those are child `segment`
    blocks and 45k words; the model does not need them to say "add a lesson after
    this one")."""
    modules = db.scalars(
        select(Block).where(Block.parent_id == course.id, Block.kind == "module").order_by(Block.order)
    ).all()
    lines: list[str] = []
    for mi, m in enumerate(modules, start=1):
        mobj = (m.meta or {}).get("objective") or m.body or ""
        lines.append(f"M{mi} [{m.id}] {m.title} — {mobj}")
        lessons = db.scalars(
            select(Block).where(Block.parent_id == m.id, Block.kind == "lesson").order_by(Block.order)
        ).all()
        for li, l in enumerate(lessons, start=1):
            lobj = (l.meta or {}).get("objective") or ""
            summary = (l.body or "").strip().replace("\n", " ")
            summary = f" — {summary}" if summary else ""
            lines.append(f"  L{li} [{l.id}] {l.title} — {lobj}{summary}")
    return "\n".join(lines) or "(the course has no modules yet)"


REVISE_SLICE_ID = "curriculum.revise"
REVISE_TAIL = (
    "YOUR TASK: propose STRUCTURAL revisions to an existing course, as a list of "
    "operations. Do NOT rewrite lessons here — insert, move, modify (by "
    "instruction), or remove them, and explain WHY each change belongs. Ground "
    "every judgement in the course as it stands and in the tutor's library above; "
    "never invent a topic his books do not support without saying so.\n"
    "\nCOURSE: {course_title}{course_brief_block}\n"
    "\nTHE COURSE AS IT STANDS (teaching order; [id] is what you reference):\n{tree}\n"
    "\nTHE TUTOR ASKS:\n{instruction}\n"
    "\nReference modules and lessons ONLY by an [id] shown above. Every op needs a "
    "one-sentence `reason`. Tier any new module honestly.\n"
    "\n{language_directive}\n\n{answer_in}"
)
REVISE_BRIEF_BLOCK = "\n\nWHAT THE TUTOR WANTS FROM THIS COURSE, IN HIS OWN WORDS:\n{brief}"


def build_revise_messages(*, course_title, brief, language, tree_text, instruction,
                          library, source=None) -> list[dict]:
    """Pure — same contract as extend.build_module_messages: the shared cached
    prefix, then every request-specific fact strictly after it."""
    messages = prefix_messages(library, source)
    content = resolve(source, REVISE_SLICE_ID, REVISE_TAIL).format(
        course_title=course_title,
        course_brief_block=(REVISE_BRIEF_BLOCK.format(brief=brief) if brief else ""),
        tree=tree_text,
        instruction=instruction.strip(),
        language_directive=language_directive(language, source),
        answer_in=answer_in(language, source),
    )
    messages.append({"role": "user", "content": content})
    return messages


def _tree_ids(db, root_id: uuid.UUID) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID]]:
    """Live module ids and lesson ids under this root, as {str: UUID} maps —
    membership is checked in one pass, no per-op query."""
    modules = db.scalars(
        select(Block).where(Block.parent_id == root_id, Block.kind == "module")
    ).all()
    module_ids = {str(m.id): m.id for m in modules}
    lesson_ids: dict[str, uuid.UUID] = {}
    for m in modules:
        for l in db.scalars(select(Block).where(Block.parent_id == m.id, Block.kind == "lesson")).all():
            lesson_ids[str(l.id)] = l.id
    return module_ids, lesson_ids


def _lesson_module(db, lesson_id: uuid.UUID) -> uuid.UUID | None:
    l = db.get(Block, lesson_id)
    return l.parent_id if l is not None and l.kind == "lesson" else None


def validate_ops(db, root_id: uuid.UUID, raw: dict) -> dict:
    """Drop every op whose required fields are missing or whose ids do not resolve
    to a real block of the right kind UNDER THIS ROOT (constraint #2). Logs each
    drop. Returns {summary, ops} — ops trimmed, ids kept as the model's strings
    (apply re-resolves them)."""
    module_ids, lesson_ids = _tree_ids(db, root_id)
    kept: list[dict] = []
    for op in raw.get("ops") or []:
        name = op.get("op")
        if name not in _REQUIRED:
            log.warning("revise: dropping op with unknown/absent 'op': %r", name)
            continue
        missing = [f for f in _REQUIRED[name] if not op.get(f)]
        if missing:
            log.warning("revise: dropping %s op — missing %s", name, missing)
            continue
        # id resolution per op
        ok = True
        if name == "insert_lesson":
            ok = op["module_id"] in module_ids and (
                op.get("after_lesson_id") in (None, "") or
                lesson_ids.get(op["after_lesson_id"]) is not None
                and _lesson_module(db, lesson_ids[op["after_lesson_id"]]) == module_ids[op["module_id"]])
        elif name == "insert_module":
            ok = op.get("after_module_id") in (None, "") or op["after_module_id"] in module_ids
        elif name in ("modify_lesson", "remove_lesson"):
            ok = op["lesson_id"] in lesson_ids
        elif name == "move_lesson":
            ok = (op["lesson_id"] in lesson_ids and op["to_module_id"] in module_ids and
                  (op.get("after_lesson_id") in (None, "") or
                   (lesson_ids.get(op["after_lesson_id"]) is not None
                    and _lesson_module(db, lesson_ids[op["after_lesson_id"]]) == module_ids[op["to_module_id"]])))
        if not ok:
            log.warning("revise: dropping %s op — an id does not resolve under root %s", name, root_id)
            continue
        kept.append(op)
    return {"summary": raw.get("summary") or "", "ops": kept}


def plan_revision(db, root_id: uuid.UUID, *, instruction: str) -> dict:
    """The whole read-only planner. Mutates nothing."""
    course = db.get(Block, root_id)
    if course is None:
        raise ReviseError(f"curriculum not found: {root_id}")
    if course.kind != "course":
        raise ReviseError(f"block {root_id} is a {course.kind!r}, not a course")

    meta = course.meta or {}
    raw_sources = meta.get("source_ids")
    source_ids = None if raw_sources is None else [uuid.UUID(s) for s in raw_sources]
    library = build_curriculum_context(db, source_ids)

    messages = build_revise_messages(
        course_title=course.title, brief=meta.get("brief"), language=course.language,
        tree_text=compact_tree_text(db, course), instruction=instruction,
        library=library, source=db,
    )
    raw = get_provider().guided_json(messages, REVISION_PLAN_SCHEMA, role="plan")
    return validate_ops(db, root_id, raw)
```

4. **Run — expect green:**

```bash
cd apps/api && .venv/bin/python -m pytest tests/test_revise_planner.py -q
```
Expected: `2 passed`.

5. **Commit** `feat(revise): read-only revision planner + REVISION_PLAN_SCHEMA + id validation`.

---

# TASK D1b — The apply engine: `edit._move_block`, commit-free cores, `apply_revision`, draft threading

The only writer. One transaction, then chain the draft fan-out.

### Files
- **Modify:** `apps/api/app/curriculum/edit.py` (add `_move_block`/`move_block`; extract commit-free cores)
- **Modify:** `apps/api/app/curriculum/revise.py` (add `apply_revision`)
- **Modify:** `apps/api/app/jobs/curriculum_draft.py` (`_claim`/`_draft_one`: thread `revise_instruction`)
- **Create test:** `apps/api/tests/test_revise_apply.py`
- **Modify test:** `apps/api/tests/test_curriculum_edit.py` (add `move_block` cases; existing cases stay green)

### Interfaces
Produces:
- `edit.move_block(db, block_id, new_parent_id, *, after=None) -> Block` — public, commits; renormalises BOTH old and new parent.
- `edit._add_module(db, root_id, *, title, objective, tier, after) -> Block` / `edit._add_lesson(db, module_id, *, title, objective, after) -> Block` / `edit._move_block(...)` — **commit-free cores** (flush + in-memory renormalise, no `db.commit()`).
- `revise.apply_revision(db, root_id: uuid.UUID, plan: dict) -> dict` — `{applied: int, root_id: str}`; ONE commit; queues new/changed lessons.

Consumes: `edit._get`, `edit._renormalise`, `outline.clamp_tier`, `outline.POLICY_GENERAL`, `outline.TIER_GAP`, `outline.gap_body`.

### Steps

1. **Write the failing test** `apps/api/tests/test_revise_apply.py`. Seed a two-module tree (M1: L1,L2; M2: L3). Assert each op path. Key cases:

```python
def test_insert_lesson_after_positions_and_queues(db_and_tree):
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    plan = {"summary": "x", "ops": [
        {"op": "insert_lesson", "module_id": str(m1.id), "after_lesson_id": str(l1.id),
         "title": "NEW", "objective": "o", "reason": "r"}]}
    out = revise.apply_revision(db, course.id, plan)
    assert out["applied"] == 1
    lessons = _children(db, m1.id, "lesson")
    assert [x.title for x in lessons] == ["L1", "NEW", "L2"]          # positional
    assert [x.order for x in lessons] == [0, 1, 2]                     # renormalised
    new = lessons[1]
    assert (new.meta or {})["draft_status"] == "queued"               # constraint #4

def test_move_lesson_across_modules_renormalises_both_parents(db_and_tree):
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    plan = {"summary": "x", "ops": [
        {"op": "move_lesson", "lesson_id": str(l2.id), "to_module_id": str(m2.id),
         "after_lesson_id": str(l3.id), "reason": "r"}]}
    revise.apply_revision(db, course.id, plan)
    assert [x.title for x in _children(db, m1.id, "lesson")] == ["L1"]
    assert [x.order for x in _children(db, m1.id, "lesson")] == [0]    # old parent closed
    m2_l = _children(db, m2.id, "lesson")
    assert [x.title for x in m2_l] == ["L3", "L2"]
    assert [x.order for x in m2_l] == [0, 1]                           # new parent contiguous
    moved = next(x for x in m2_l if x.title == "L2")
    assert moved.parent_id == m2.id and (moved.meta or {})["draft_status"] == "queued"

def test_remove_lesson_deletes_and_renormalises(db_and_tree):
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    revise.apply_revision(db, course.id, {"summary": "x", "ops": [
        {"op": "remove_lesson", "lesson_id": str(l1.id), "reason": "r"}]})
    assert [x.title for x in _children(db, m1.id, "lesson")] == ["L2"]
    assert [x.order for x in _children(db, m1.id, "lesson")] == [0]

def test_modify_lesson_requeues_with_instruction_and_stashes_prev_body(db_and_tree):
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    l1.body = "old body"; db.commit()
    revise.apply_revision(db, course.id, {"summary": "x", "ops": [
        {"op": "modify_lesson", "lesson_id": str(l1.id), "instruction": "harder", "reason": "r"}]})
    db.refresh(l1)
    assert (l1.meta or {})["draft_status"] == "queued"
    assert (l1.meta or {})["revise_instruction"] == "harder"
    assert (l1.meta or {})["prev_body"] == "old body"

def test_insert_module_queues_its_lessons_and_clamps_tier(db_and_tree):
    db, course, m1, m2, *_ = db_and_tree
    course.meta = {**(course.meta or {}), "gap_policy": "general"}; db.commit()
    revise.apply_revision(db, course.id, {"summary": "x", "ops": [
        {"op": "insert_module", "after_module_id": str(m1.id), "title": "M-new",
         "objective": "o", "tier": "library", "reason": "r",
         "lessons": [{"title": "a", "objective": "oa"}, {"title": "b", "objective": "ob"}]}]})
    mods = _children(db, course.id, "module")
    assert [x.title for x in mods] == ["M1", "M-new", "M2"]
    m_new = mods[1]
    assert [x.title for x in _children(db, m_new.id, "lesson")] == ["a", "b"]
    assert all((x.meta or {})["draft_status"] == "queued" for x in _children(db, m_new.id, "lesson"))

def test_apply_is_atomic_all_or_nothing(db_and_tree, monkeypatch):
    """A raise partway through leaves the tree untouched (one transaction)."""
    db, course, m1, m2, l1, l2, l3 = db_and_tree
    before = {b.id: b.order for b in db.query(Block).all()}
    # second op references a lesson we delete in-flight is hard to force; instead
    # monkeypatch edit._add_lesson to raise on the 2nd call:
    ...
    with pytest.raises(Exception):
        revise.apply_revision(db, course.id, plan_with_two_inserts)
    db.rollback()
    assert {b.id: b.order for b in db.query(Block).all()} == before
```

2. **Run — expect failure:** `cd apps/api && .venv/bin/python -m pytest tests/test_revise_apply.py -q`.

3. **Refactor `edit.py` into commit-free cores + add `move_block`.** Public `add_module`/`add_lesson` keep committing (existing tests + HTTP callers unchanged); apply uses the cores.

```python
def _add_module(db, root_id, *, title, objective="", tier="general_knowledge", after=None):
    """Commit-free core of add_module. Flushes, renormalises siblings in memory,
    does NOT commit — so apply_revision can batch many ops into one transaction."""
    course = _get(db, root_id, kind="course")
    siblings = db.scalars(
        select(Block).where(Block.parent_id == course.id, Block.kind == "module").order_by(Block.order)
    ).all()
    at = len(siblings)
    if after is not None:
        target = _get(db, after, kind="module")
        if target.parent_id != course.id:
            raise EditError(f"module {after} is not part of this curriculum")
        at = next(i for i, s in enumerate(siblings) if s.id == target.id) + 1
    module = Block(kind="module", title=title, body=objective or None, order=at,
                   parent_id=course.id, language=course.language,
                   meta={"tier": tier, "objective": objective, "coverage_note": "", "added_by_tutor": True})
    siblings.insert(at, module)
    db.add(module); db.flush()
    for i, sib in enumerate(siblings): sib.order = i
    return module


def add_module(db, root_id, *, title, objective="", tier="general_knowledge", after=None):
    module = _add_module(db, root_id, title=title, objective=objective, tier=tier, after=after)
    db.commit(); db.refresh(module); return module
```
Do the identical extraction for `add_lesson` → `_add_lesson` (keep the `minutes` derivation from `course.meta["shape"]["minutes_per_lesson"]` inside the core). Then add:

```python
def _move_block(db, block_id, new_parent_id, *, after=None):
    """Commit-free core: move a lesson to another module, renormalising BOTH the
    old parent (the hole it leaves) AND the new parent (where it lands). This is
    the cross-parent capability edit.py never had — reorder_block only ever moved
    a block AMONG ITS OWN siblings; _renormalise only ever touched ONE parent."""
    block = _get(db, block_id, kind="lesson")
    old_parent_id = block.parent_id
    new_parent = _get(db, new_parent_id, kind="module")
    # both modules must live under the same course
    if old_parent_id is not None:
        old_module = db.get(Block, old_parent_id)
        if old_module is None or old_module.parent_id != new_parent.parent_id:
            raise EditError("cannot move a lesson across curricula")
    dest = db.scalars(
        select(Block).where(Block.parent_id == new_parent.id, Block.kind == "lesson").order_by(Block.order)
    ).all()
    at = len(dest)
    if after is not None:
        target = _get(db, after, kind="lesson")
        if target.parent_id != new_parent.id:
            raise EditError(f"lesson {after} is not part of the destination module")
        at = next(i for i, s in enumerate(dest) if s.id == target.id) + 1
    block.parent_id = new_parent.id
    dest.insert(at, block)
    db.flush()
    for i, sib in enumerate(dest): sib.order = i          # new parent contiguous
    if old_parent_id is not None and old_parent_id != new_parent.id:
        _renormalise(db, old_parent_id)                  # old parent hole closed
    return block


def move_block(db, block_id, new_parent_id, *, after=None):
    block = _move_block(db, block_id, new_parent_id, after=after)
    db.commit(); db.refresh(block); return block
```

4. **Implement `revise.apply_revision`** in `revise.py`. ONE transaction; queue new/changed lessons.

```python
from app.curriculum import edit
from app.curriculum.outline import POLICY_GENERAL, TIER_GAP, clamp_tier, gap_body


def _queue(block: Block) -> None:
    block.meta = {**(block.meta or {}), "draft_status": "queued", "error": None}


def apply_revision(db, root_id: uuid.UUID, plan: dict) -> dict:
    """Execute an APPROVED plan in ONE transaction (constraint #3). Re-validates
    every id (defence in depth — the chat path round-trips the plan through the
    model). New/changed lessons are queued; the caller chains the draft fan-out."""
    course = db.get(Block, root_id)
    if course is None or course.kind != "course":
        raise ReviseError(f"not a curriculum root: {root_id}")
    validated = validate_ops(db, root_id, plan)           # re-resolve against live tree
    gap_policy = (course.meta or {}).get("gap_policy") or POLICY_GENERAL
    applied = 0
    try:
        for op in validated["ops"]:
            name = op["op"]
            if name == "insert_lesson":
                lesson = edit._add_lesson(
                    db, uuid.UUID(op["module_id"]), title=op["title"], objective=op["objective"],
                    after=uuid.UUID(op["after_lesson_id"]) if op.get("after_lesson_id") else None)
                _queue(lesson)                            # _add_lesson already queues; explicit for clarity
            elif name == "insert_module":
                tier = clamp_tier(op["tier"], gap_policy)
                module = edit._add_module(
                    db, root_id, title=op["title"], objective=op["objective"], tier=tier,
                    after=uuid.UUID(op["after_module_id"]) if op.get("after_module_id") else None)
                if tier == TIER_GAP:
                    module.body = gap_body(course.language)
                else:
                    for li, spec in enumerate(op["lessons"]):
                        lesson = Block(kind="lesson", title=spec["title"], body=spec["objective"] or None,
                                       order=li, parent_id=module.id, language=course.language,
                                       meta={"draft_status": "queued", "objective": spec["objective"] or ""})
                        db.add(lesson)
            elif name == "move_lesson":
                lesson = edit._move_block(
                    db, uuid.UUID(op["lesson_id"]), uuid.UUID(op["to_module_id"]),
                    after=uuid.UUID(op["after_lesson_id"]) if op.get("after_lesson_id") else None)
                _queue(lesson)                            # re-draft in its new position/context
            elif name == "modify_lesson":
                lesson = db.get(Block, uuid.UUID(op["lesson_id"]))
                lesson.meta = {**(lesson.meta or {}), "draft_status": "queued", "error": None,
                               "revise_instruction": op["instruction"], "prev_body": lesson.body}
            elif name == "remove_lesson":
                lesson = db.get(Block, uuid.UUID(op["lesson_id"]))
                parent_id = lesson.parent_id
                db.delete(lesson); db.flush()
                if parent_id is not None:
                    edit._renormalise(db, parent_id)
            applied += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"applied": applied, "root_id": str(root_id)}
```

5. **Thread `revise_instruction` through the draft fan-out.** In `apps/api/app/jobs/curriculum_draft.py`:
   - `_claim` (`:96`) — its meta scrub already drops `deepen`; also drop and RETURN `revise_instruction`. Change the return to `(lesson, deepen, revise_instruction)`, add `revise_instruction = meta.get("revise_instruction")`, and exclude it in the scrubbed-meta comprehension alongside `deepen`.
   - `_draft_one` (`:180`) — unpack the third value; when set, append it to the objective it passes into `LessonContext`:
   ```python
   claimed = _claim(db, lesson_id)
   if claimed is None: return
   lesson, deepen, revise_instruction = claimed
   ...
   objective = (lesson.meta or {}).get("objective") or (lesson.body or "")
   if revise_instruction:
       objective = f"{objective}\n\nΑναθεώρηση από τον καθηγητή: {revise_instruction}"
   ctx = LessonContext(..., lesson_objective=objective, ...)
   ```
   This is contained to the file the plan already touches; the instruction rides the per-lesson **volatile** objective (already outside the cached library prefix — no cache impact), and is consumed at claim time so a later unrelated Resume never re-applies it (same discipline `deepen` uses).

6. **Add `move_block` tests to `tests/test_curriculum_edit.py`** (cross-parent renormalises both; rejects a lesson from another course; `after` in the wrong module → `EditError`). **Run both suites:**

```bash
cd apps/api && .venv/bin/python -m pytest tests/test_revise_apply.py tests/test_curriculum_edit.py -q
```
Expected: all green (existing edit tests unchanged — public wrappers still commit).

7. **Commit** `feat(revise): transactional apply engine + edit.move_block + draft requeue`.

---

# TASK D1c — Runner + `POST /curricula/{root_id}/revise` (202, plan vs apply)

Wire the planner and the apply engine to the async job pattern.

### Files
- **Create:** `apps/api/app/jobs/curriculum_revise.py`
- **Modify:** `apps/api/app/routers/curriculum.py` (endpoint + schema import)
- **Modify:** `apps/api/app/schemas/curriculum.py` (`ReviseRequest`)
- **Create test:** `apps/api/tests/test_revise_api.py`

### Interfaces
Produces:
- `app.jobs.curriculum_revise.run_curriculum_revise_job(job_id: uuid.UUID) -> None` — reads `job.params`; `"plan"` present → apply + chain draft; absent → run planner, store plan in `job.progress`.
- `POST /curricula/{root_id}/revise` → `202` + `JobAccepted`. Body `ReviseRequest{instruction: str, mode: "plan"|"apply" = "plan", plan: dict | None}`.

Consumes: `revise.plan_revision`/`apply_revision`, `jobs.curriculum_draft.run_curriculum_draft_job`, `models.generation_job.GenerationJob`, the same `LLMError`/`CurriculumContextError`/`LLMNotConfigured` classification as `module_generate.py`.

### Steps

1. **Write the failing test** `apps/api/tests/test_revise_api.py`, mirroring `test_curriculum_api.py`'s TestClient + `monkeypatch.setattr("app.routers.curriculum.run_curriculum_revise_job", ...)` (Starlette runs BackgroundTasks in-process after the response — an unpatched test fires the real planner). Assert:
   - `POST /curricula/{root}/revise {instruction, mode:"plan"}` → 202, a `GenerationJob(kind="curriculum_revise")` row exists with `params` carrying `root_id`+`instruction` and NO `plan`.
   - `mode:"apply"` with a `plan` → 202, params carry the `plan`.
   - a non-course root → 404.
   - **Runner unit test** (call `run_curriculum_revise_job` directly, provider + `run_curriculum_draft_job` stubbed): plan-mode stores `job.progress["plan"]` and `status=="succeeded"`; apply-mode calls `apply_revision` then enqueues+runs a `curriculum_draft` job.

2. **Run — expect failure.**

3. **Implement the runner** `apps/api/app/jobs/curriculum_revise.py` — mirror `module_generate.run_module_generate_job` (two job rows on apply; the same error taxonomy):

```python
"""The revise job. Two modes on one kind:
  * no "plan" in params  -> run the PLANNER, store the plan on job.progress. Read-only.
  * "plan" in params     -> APPLY it (one tx), then chain the ordinary draft fan-out —
                            same "two job rows, the planning/applying row succeeds the
                            moment the tree is right, the draft row owns partial success"
                            shape as jobs/module_generate.py.
Reused by BOTH callers: POST /curricula/{root}/revise (mode drives params) and the chat
apply tool via resolve_approval (which enqueues params={root_id, plan})."""
from __future__ import annotations
import logging, uuid
from app.curriculum.corpus import CurriculumContextError
from app.curriculum.revise import ReviseError, apply_revision, plan_revision
from app.db import SessionLocal
from app.jobs.curriculum_draft import run_curriculum_draft_job
from app.llm.errors import LLMError, LLMNotConfigured
from app.models.generation_job import GenerationJob
log = logging.getLogger(__name__)


def run_curriculum_revise_job(job_id: uuid.UUID) -> None:
    db = SessionLocal()
    root_id = None
    do_chain = False
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_curriculum_revise_job: job %s not found", job_id); return
        job.status = "running"; job.progress = {"phase": "planning"}; db.commit()
        root_id = uuid.UUID(str(job.params["root_id"]))
        plan = job.params.get("plan")
        if plan is None:
            result = plan_revision(db, root_id, instruction=job.params["instruction"])
            job.status = "succeeded"; job.result_root_id = root_id
            job.progress = {"phase": "done", "plan": result}     # the plan the poller reads
            db.commit()
        else:
            out = apply_revision(db, root_id, plan)
            job.status = "succeeded"; job.result_root_id = root_id
            job.progress = {"phase": "drafting", **out}; db.commit()
            do_chain = True
    except ReviseError as e:
        _fail(db, job_id, "internal", str(e)); return
    except CurriculumContextError as e:
        _fail(db, job_id, "upstream", str(e)); return
    except LLMNotConfigured:
        _fail(db, job_id, "auth", "No API key is configured. Open Settings, add your key, then try again."); return
    except LLMError as e:
        _fail(db, job_id, "rate_limit" if e.kind == "rate_limit" else "upstream",
              "The model is rate-limited right now. Wait a minute and try again."
              if e.kind == "rate_limit" else (str(e) or "The revision could not be planned. Try again.")); return
    except Exception:
        log.exception("run_curriculum_revise_job: job %s failed", job_id)
        _fail(db, job_id, "internal", "The revision could not be completed. Try again."); return
    finally:
        db.close()

    if do_chain:                                                 # same chain as module_generate
        db = SessionLocal()
        try:
            draft = GenerationJob(kind="curriculum_draft", status="pending",
                                  params={"root_id": str(root_id)})
            db.add(draft); db.commit(); draft_id = draft.id
        except Exception:
            log.exception("run_curriculum_revise_job: could not enqueue draft chain for %s — "
                          "revised lessons stay queued for Resume", root_id); return
        finally:
            db.close()
        run_curriculum_draft_job(draft_id)


def _fail(db, job_id, kind, message):
    try:
        db.rollback(); job = db.get(GenerationJob, job_id)
        if job is None: return
        job.status = "failed"; job.error_kind = kind; job.error = message; db.commit()
    except Exception:
        log.warning("could not record failure for job %s", job_id, exc_info=True)
```

4. **Add `ReviseRequest` to `apps/api/app/schemas/curriculum.py`:**
```python
class ReviseRequest(BaseModel):
    instruction: str = Field(min_length=1)
    mode: Literal["plan", "apply"] = "plan"
    plan: dict | None = None
```

5. **Add the endpoint** to `apps/api/app/routers/curriculum.py` (import `run_curriculum_revise_job` at module level for monkeypatchability; place beside `generate_curriculum_module:409`):

```python
@router.post("/curricula/{root_id}/revise", response_model=JobAccepted, status_code=202,
             dependencies=[Depends(require_llm_configured)])
def revise_curriculum(root_id: UUID, payload: ReviseRequest,
                      background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> JobAccepted:
    """PLAN or APPLY a curriculum revision — 202 + a curriculum_revise job.
    mode="plan": the job runs the read-only planner and stores the plan on
    job.progress["plan"] (poll GET /jobs/{id}). mode="apply": the job applies the
    approved `plan` in one transaction and chains the draft fan-out. The planner
    reads the whole library (20-60s) — exactly the timeout class the job table
    exists for. Both modes MUTATE NOTHING until an approved plan is applied."""
    course = _get_block_or_404(db, root_id)
    if course.kind != "course":
        raise HTTPException(status_code=404, detail="not a curriculum root")
    if payload.mode == "apply" and not payload.plan:
        raise HTTPException(status_code=422, detail="apply requires a plan")
    params = {"root_id": str(root_id), "instruction": payload.instruction}
    if payload.mode == "apply":
        params["plan"] = payload.plan
    job = GenerationJob(kind="curriculum_revise", status="pending", params=params)
    db.add(job); db.commit(); db.refresh(job)
    background_tasks.add_task(run_curriculum_revise_job, job.id)
    return JobAccepted(job_id=job.id, status=job.status)
```
Add `ReviseRequest` to the `schemas.curriculum` import block and `from app.jobs.curriculum_revise import run_curriculum_revise_job`. Note: `GenerationJob.kind` is `String(30)` — `"curriculum_revise"` is 17 chars, fits.

6. **Run — expect green:** `cd apps/api && .venv/bin/python -m pytest tests/test_revise_api.py -q`.

7. **Commit** `feat(revise): POST /curricula/{root}/revise + curriculum_revise runner (plan|apply)`.

---

# TASK D2a — The two agent tools + registry guards + chat wiring + session binding

Reuse the chat HITL machinery. Two tools; one nullable column; one context injection.

### Files
- **Modify:** `apps/api/app/agent/tools.py` (2 tool fns + 2 registry entries)
- **Modify:** `apps/api/app/routers/chat.py` (`curriculum_revise` runner/label; context injection; `create_chat_session` root_id)
- **Modify:** `apps/api/app/models/chat.py` (`ChatSession.root_id`), `apps/api/app/schemas/chat.py` (`ChatSessionCreate.root_id`)
- **Create:** `apps/api/alembic/versions/<newrev>_chat_session_root_id.py`
- **Modify tests:** `apps/api/tests/test_agent_tools.py`, `apps/api/tests/test_agent_hitl.py`
- **Create test:** `apps/api/tests/test_revise_chat.py`

### Interfaces
Produces:
- `tools.propose_curriculum_revision(root_id, instruction)` — `kind="read"`; `fn=_propose_curriculum_revision` → `revise.plan_revision(...)` (inline).
- `tools.apply_curriculum_revision(root_id, plan)` — `kind="mutation"`, `async_job=True`, `job_kind="curriculum_revise"`; `fn=_apply_curriculum_revision` (registered-but-not-called, like `_generate_curriculum`).
- `ChatSession.root_id: UUID | None` (nullable column).
- `chat._inject_curriculum_context(db, session, wire) -> list[dict]` — transiently appends compact tree + brief to the last user turn when `session.root_id` is set (never persisted, never in the cached prefix).

### Steps

1. **Write the failing registry/completeness tests** (constraint #7). Update the exact-set assertions:
   - `test_agent_tools.py::test_registry_has_exactly_the_read_tools_registered_so_far` — add `"propose_curriculum_revision"` to the expected read set.
   - `test_agent_hitl.py::test_registry_has_exactly_the_fourteen_mutation_tools_registered_so_far` — rename/extend to include `"apply_curriculum_revision"` (15 mutations).
   - `test_agent_hitl.py::test_exactly_generate_curriculum_and_draft_lesson_are_marked_async_job` — expected async set becomes `{"generate_curriculum", "draft_lesson_from_selection", "apply_curriculum_revision"}`.
   - New `test_revise_chat.py`: (a) `propose` fn calls `plan_revision` and returns its dict, mutating nothing; (b) `apply` entry is `async_job` with `job_kind=="curriculum_revise"`; (c) `resolve_approval` on an `apply_curriculum_revision` approval enqueues a `curriculum_revise` job with `params["plan"]` and returns `status=="job_pending"` (monkeypatch `chat_router.run_curriculum_revise_job`); (d) `_inject_curriculum_context` appends the tree to the last user message only when `session.root_id` is set.

2. **Run — expect failure.**

3. **Add the tool fns** in `apps/api/app/agent/tools.py` (near `_generate_curriculum:669`):

```python
from app.curriculum.revise import plan_revision as _plan_revision_service

def _propose_curriculum_revision(db, *, root_id: str, instruction: str) -> dict:
    """READ-ONLY: read the whole curriculum + the tutor's library and return a
    PLAN of structural revisions, each with a reason. Answers "should I add
    this?" without changing anything — apply_curriculum_revision is the gated
    step that actually applies an approved plan. Runs the planner inline (20-60s
    over the library); the chat turn shows a spinner meanwhile."""
    parsed = _parse_uuid(root_id)
    if parsed is None:
        return {"error": f"invalid root_id: {root_id!r}"}
    try:
        return _plan_revision_service(db, parsed, instruction=instruction)
    except Exception as e:   # ReviseError etc. — graceful dict, never a loop crash
        return {"error": str(e)}

def _apply_curriculum_revision(db, *, root_id: str, plan: dict) -> dict:
    """Registered for COMPLETENESS ONLY — the loop never calls this inline. It is
    async_job=True, so resolve_approval enqueues a curriculum_revise job (which
    applies the plan + chains the draft fan-out). Kept a working fn, like
    _generate_curriculum, so the registry entry isn't a dead end."""
    parsed = _parse_uuid(root_id)
    if parsed is None:
        return {"error": f"invalid root_id: {root_id!r}"}
    from app.curriculum.revise import apply_revision
    return apply_revision(db, parsed, plan)
```

4. **Register both tools** in `TOOLS` (`:993`). `propose` mirrors the `get_curriculum` read schema shape; `apply` mirrors the `generate_curriculum` async schema (`:1440-1497`):

```python
"propose_curriculum_revision": ToolEntry(
    schema={"type": "function", "function": {
        "name": "propose_curriculum_revision",
        "description": (
            "Propose structural changes to an EXISTING curriculum (add/move/"
            "modify/remove lessons and modules) by reading the whole course and "
            "the tutor's library. Returns a PLAN of operations, each with a "
            "reason — it CHANGES NOTHING. Use it to answer 'should I add X?' and "
            "to draft a revision the tutor can then approve with "
            "apply_curriculum_revision."),
        "parameters": {"type": "object", "properties": {
            "root_id": {"type": "string", "description": "the curriculum root id (from context)"},
            "instruction": {"type": "string", "description": "what the tutor wants changed"}},
            "required": ["root_id", "instruction"]}}},
    fn=_propose_curriculum_revision, kind="read"),
"apply_curriculum_revision": ToolEntry(
    schema={"type": "function", "function": {
        "name": "apply_curriculum_revision",
        "description": (
            "Apply a revision plan produced by propose_curriculum_revision to the "
            "curriculum. This is a MUTATION — it requires the tutor's explicit "
            "approval before anything changes. Pass the EXACT plan object "
            "propose returned; on approval the new/changed lessons are drafted in "
            "the background and the tutor watches them arrive."),
        "parameters": {"type": "object", "properties": {
            "root_id": {"type": "string", "description": "the curriculum root id (from context)"},
            "plan": {"type": "object", "description": "the plan object from propose_curriculum_revision"}},
            "required": ["root_id", "plan"]}}},
    fn=_apply_curriculum_revision, kind="mutation", async_job=True, job_kind="curriculum_revise"),
```

5. **Wire `curriculum_revise` into `resolve_approval`** (`apps/api/app/routers/chat.py`):
   - Import: `from app.jobs.curriculum_revise import run_curriculum_revise_job` (module level — monkeypatchable, same reason as `run_curriculum_job`).
   - `_ASYNC_JOB_LABELS` (`:65`): add `"curriculum_revise": "Curriculum revision"`.
   - In the async branch (`:612`) extend the in-body runner lookup:
     `runner = {"curriculum": run_curriculum_job, "lesson": run_lesson_job, "curriculum_revise": run_curriculum_revise_job}[job_kind]`.
   The rest of the async branch is generic — it already enqueues `GenerationJob(kind=job_kind, params=args)` where `args` is the approval's `{root_id, plan}` (constraint #8: no schema change, `params["plan"]` present → the runner applies).

6. **Bind the session to a curriculum.** Add the column + injection:
   - `apps/api/app/models/chat.py` `ChatSession`: `root_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)` — NO `ForeignKey` (same decoupling rationale as `student_id`: a transcript must survive the curriculum being edited/deleted).
   - `apps/api/app/schemas/chat.py` `ChatSessionCreate`: add `root_id: UUID | None = None`.
   - `create_chat_session` (`chat.py:266`): `ChatSession(student_id=..., locale=..., root_id=payload.root_id)`.
   - Migration `<newrev>_chat_session_root_id.py`: `op.add_column("chat_session", sa.Column("root_id", sa.Uuid(as_uuid=True), nullable=True))` / `op.drop_column(...)`. `down_revision = <current head, i.e. Unit C's blueprint_default migration — get via alembic heads>`.
   - Add `_inject_curriculum_context` and call it in `post_message`, `post_message_stream`, and `resolve_approval` right after building `wire`, before `run_agent_turn`/`stream_plain_turn`:
   ```python
   def _inject_curriculum_context(db, session, wire):
       """Bind the turn to the session's curriculum WITHOUT touching the cached
       system+tools prefix or the persisted transcript: append a compact tree +
       brief to the LAST user message of the transient wire (same position the
       forced-retrieval grounding block already uses — loop.py:397). Re-injected
       every turn so the model always sees the CURRENT tree (after an apply)."""
       if not session.root_id:
           return wire
       from app.models.block import Block
       from app.curriculum.revise import compact_tree_text
       course = db.get(Block, session.root_id)
       if course is None or course.kind != "course":
           return wire
       brief = (course.meta or {}).get("brief") or ""
       ctx = (f"\n\n[CURRICULUM CONTEXT — this conversation is about curriculum "
              f"{course.id} titled \"{course.title}\". Use this root_id with "
              f"propose_curriculum_revision / apply_curriculum_revision."
              + (f" Brief: {brief}." if brief else "")
              + f"\nCurrent structure:\n{compact_tree_text(db, course)}]")
       wire = list(wire)
       for i in range(len(wire) - 1, -1, -1):
           if wire[i].get("role") == "user":
               wire[i] = {**wire[i], "content": (wire[i].get("content") or "") + ctx}
               break
       return wire
   ```

7. **Run — expect green:**
```bash
cd apps/api && .venv/bin/python -m pytest tests/test_agent_tools.py tests/test_agent_hitl.py tests/test_revise_chat.py -q
```

8. **Commit** `feat(chat): propose/apply curriculum-revision tools + curriculum-bound sessions`.

---

# TASK D2b — The revise drawer + `RevisionPlanCard` on `curricula/[rootId]`

A right-side drawer on the detail route; the plan renders through an `ApprovalCard` variant.

### Files
- **Create:** `apps/web/src/components/curriculum/revise-drawer.tsx`
- **Create:** `apps/web/src/components/curriculum/revision-plan-card.tsx`
- **Modify:** `apps/web/src/app/[locale]/(cockpit)/curricula/[rootId]/page.tsx` (mount the drawer + a trigger; refresh the tree on apply)
- **Modify:** `apps/web/src/lib/api.ts` (`createChatSession` root_id passthrough; types)
- **Modify:** `apps/web/src/components/chat/chat-panel.tsx` (accept optional `rootId` + `onJobDone`; render `RevisionPlanCard` for the revise tool) OR wrap `ChatPanel` in the drawer (see step 1 decision)
- **Modify i18n:** `apps/web/src/i18n/messages/{en,el}.json` (`curricula.revise.*`)
- **Modify test:** the curricula Playwright spec (grep tests for `template-item`/`curricula-back`)

### Interfaces
Consumes: `ChatPanel` (transcript + streaming + HITL + job polling — all reused), `createChatSession(studentId, locale, rootId)`, `getCurriculum`, `getCurriculumProgress`, `ApprovalCard`.
Produces: a drawer holding a curriculum-bound `ChatPanel`; a `RevisionPlanCard` rendering `plan.ops[].reason` with Approve/Reject.

### Steps

1. **Decision — reuse `ChatPanel` inside the drawer.** The drawer creates a curriculum-bound session (`createChatSession(null, locale, rootId)`) once on first open, stores the id, and renders `<ChatPanel sessionId={id} rootId={rootId} onJobDone={refreshTree} />`. `ChatPanel` already owns transcript/stream/HITL/polling — add two optional props:
   - `rootId?: string` — passed only so the panel can special-case the revise approval card (below).
   - `onJobDone?: () => void` — called from `pollJob` on `succeeded` INSTEAD of appending the "view curriculum" link when `rootId` is set (the tutor is already looking at the board; refresh it so the queued lessons + progress bar appear).

2. **`revision-plan-card.tsx` — the `ApprovalCard` variant.** Reuse `ApprovalCard`'s shell but render ops legibly. Simplest: in `chat-panel.tsx`, when `pendingApproval.toolName === "apply_curriculum_revision"`, render `<RevisionPlanCard plan={pendingApproval.toolArgs.plan} summary=... resolving error onApprove onReject />` instead of the generic `ApprovalCard`; otherwise render `ApprovalCard` as today. `RevisionPlanCard` maps `plan.ops` to a list of `{op, reason, title?}` rows with per-op icons, and reuses the same Approve/Reject buttons (`onApprove()` with no edited args — the plan is applied verbatim; constraint: the approved plan IS the applied plan). Keep the raw-JSON edit affordance OUT (a hand-edited plan defeats the id-validation story; the server re-validates regardless).

```tsx
export function RevisionPlanCard({ summary, ops, resolving, error, onApprove, onReject }: Props) {
  const t = useTranslations("curricula.revise");
  return (
    <Card data-testid="revision-plan-card" className="border-primary/30">
      <CardHeader><CardTitle className="text-sm">{t("planHeading")}</CardTitle>
        {summary && <CardDescription>{summary}</CardDescription>}</CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ul data-testid="revision-plan-ops" className="flex flex-col gap-2 text-sm">
          {ops.map((op, i) => (
            <li key={i} className="rounded-md border border-border p-2">
              <span className="font-medium">{t(`op.${op.op}`)}</span>
              {op.title ? <span> — {op.title}</span> : null}
              <p className="text-xs text-muted-foreground">{op.reason}</p>
            </li>
          ))}
        </ul>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <div className="flex gap-2">
          <Button data-testid="revision-approve" disabled={resolving} onClick={() => onApprove()}>
            {resolving && <Loader2 className="animate-spin" />}{t("approve")}</Button>
          <Button variant="destructive" data-testid="revision-reject" onClick={onReject}>{t("reject")}</Button>
        </div>
      </CardContent>
    </Card>
  );
}
```

3. **`revise-drawer.tsx`** — a right-side panel (reuse the app's existing Sheet/Drawer primitive if present under `components/ui`; otherwise a fixed `aside` with a translate transition). It holds a "Revise with chat" trigger `Button data-testid="revise-open"`, lazily creates the bound session on first open, and mounts `ChatPanel`. On `onJobDone` it calls the page's `refreshTree`.

4. **Mount on the detail page.** In `curricula/[rootId]/page.tsx`, add the drawer beside `TreeBoard` and a `refreshTree` that re-runs `getCurriculum(rootId)` into `setTree` (the board also self-polls progress via its own `refresh`, so a simple re-fetch suffices). Pass `rootId` down.

5. **`api.ts`** — `createChatSession(studentId?, locale?, rootId?)` adds `root_id: rootId ?? null` to the POST body; add `rootId?` to the type. No other client changes (reuse `resolveApproval`, `getJob`, `streamChatMessage`).

6. **i18n** — add `curricula.revise.{planHeading, approve, reject, open, title, op.insert_lesson, op.insert_module, op.modify_lesson, op.move_lesson, op.remove_lesson}` in both `en` and `el`.

7. **Verify + test.**
```bash
cd apps/web && npx tsc --noEmit
```
Extend the curricula Playwright spec: open the detail route, click `revise-open`, type an instruction, mock/stub the chat turn to yield an `apply_curriculum_revision` approval (or drive against a seeded backend), assert `revision-plan-card` + `revision-plan-ops` render with reasons, click `revision-approve`, and assert the board shows a newly `queued` lesson (poll `board`/progress). If the suite mocks the API, assert the POST to the resolve endpoint fired.

8. **Commit** `feat(curricula): revise-with-chat drawer + RevisionPlanCard on the detail route`.

---

## Final whole-branch verification

```bash
cd apps/api && .venv/bin/python -m pytest tests/test_revise_planner.py tests/test_revise_apply.py \
  tests/test_revise_api.py tests/test_revise_chat.py tests/test_curriculum_edit.py \
  tests/test_agent_tools.py tests/test_agent_hitl.py -q
cd apps/web && npx tsc --noEmit && npx playwright test curricula
cd apps/api && .venv/bin/alembic upgrade head && .venv/bin/alembic downgrade -1 && .venv/bin/alembic upgrade head
```
All green; the migration up/down/up cycle is clean (additive `chat_session.root_id`).

## Risk register (resolve before or during implementation)

- **`modify_lesson` = full re-draft under an instruction** (the design here) vs. a synchronous in-place `refine_block`. This plan re-drafts (constraint #4: "changed lessons → queued → chain draft"), stashing `prev_body` for recovery. A full re-draft is heavier and replaces the current body; if the controller prefers the lighter in-place refine, swap the `modify_lesson` branch in `apply_revision` to call `refine.refine_block` — but that makes apply do an LLM call inside the transaction (breaks constraint #3) so it would need to move to the runner. **Flagged for the controller.**
- **Chat apply re-copies the plan through the model** (`apply_curriculum_revision(root_id, plan)`). The plan is compact (no bodies) and the apply engine RE-VALIDATES every id, so a garbled id is dropped, not misapplied — safe by construction, but a large plan costs tokens. The REST endpoint is the exact-fidelity path for the non-chat UI. Acceptable for the PoC.
- **Inline planner latency in chat** (`propose_curriculum_revision` runs 20-60s in the agent loop). Matches `module_generate`'s cost class but is unusual for a read tool; the drawer shows a spinner. If unacceptable, `propose` could be re-shaped to enqueue a `curriculum_revise` plan-mode job and the drawer polls it — more moving parts; deferred.
