"""REVISE — read a whole curriculum + the tutor's library, PLAN structural
changes, mutate nothing. Apply is a separate, approval-gated step (apply_revision).

The planner mirrors extend.generate_module_json: same warm prefix
(prefix_messages over build_curriculum_context), same role="plan" guided_json,
same None-vs-[] source_ids rule (extend.py:322-324). The one difference is the
task in the volatile tail: not "design one module" but "propose a list of ops on
THIS tree". The tree is serialised COMPACTLY — ids + titles + objectives +
one-line summaries, never lesson bodies — because the model needs to locate an
insertion point, not re-read 45,000 words it already wrote.

NOTHING IN THE PLANNER WRITES. plan_revision is a pure read. validate_ops resolves
every id against the live tree and DROPS what does not resolve, so a hallucinated
module id can never reach apply. apply_revision (below) is the only writer, and it
commits exactly once.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.curriculum.blueprint import BlueprintInvalid, validate_blueprint
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
#
# `update_blueprint` is the controller's 2026-07-18 addition (the chat/revise can
# also reshape an existing course's lesson blueprint). It carries the full
# replacement blueprint object and NEVER auto-re-drafts — re-drafting existing
# lessons under a new blueprint stays the opt-in `POST /curricula/{root}/redraft`.
_OP_ENUM = [
    "insert_lesson", "insert_module", "modify_lesson", "move_lesson",
    "remove_lesson", "update_blueprint",
]

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
                    "blueprint": {
                        "type": "object",
                        "description": "update_blueprint: the FULL replacement lesson-blueprint object "
                                       "(version + sections). Changes lesson STRUCTURE only; existing "
                                       "lessons keep their content until re-drafted.",
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

# Per-op required fields enforced in Python (constraint #9). Keys that must be
# present (and, for *_id keys, resolve to a live block of the right kind under
# this root — see validate_ops).
_REQUIRED: dict[str, tuple[str, ...]] = {
    "insert_lesson": ("module_id", "title", "objective"),
    "insert_module": ("title", "objective", "tier", "lessons"),
    "modify_lesson": ("lesson_id", "instruction"),
    "move_lesson": ("lesson_id", "to_module_id"),
    "remove_lesson": ("lesson_id",),
    "update_blueprint": ("blueprint",),
}


def compact_tree_text(db, course: Block) -> str:
    """The tree as the model needs it to locate an insertion point: every module
    and lesson with its id, title and objective — and NOTHING ELSE.

    NO LESSON BODIES. A drafted lesson's `body` is a one-line summary
    (persist_lesson sets it from the draft's `summary`), but it is still content
    the planner does not need to say "add a lesson after this one" — and an
    UN-drafted lesson's body carries its objective, which we already print. The
    full section prose lives in child `segment` blocks (45k words) and never comes
    near here. Keeping the tree to ids + titles + objectives is the token
    discipline the whole read-only planner rides on (Global Constraint #5)."""
    modules = db.scalars(
        select(Block).where(Block.parent_id == course.id, Block.kind == "module").order_by(Block.order)
    ).all()
    lines: list[str] = []
    for mi, m in enumerate(modules, start=1):
        mobj = (m.meta or {}).get("objective") or ""
        lines.append(f"M{mi} [{m.id}] {m.title} — {mobj}")
        lessons = db.scalars(
            select(Block).where(Block.parent_id == m.id, Block.kind == "lesson").order_by(Block.order)
        ).all()
        for li, l in enumerate(lessons, start=1):
            lobj = (l.meta or {}).get("objective") or ""
            lines.append(f"  L{li} [{l.id}] {l.title} — {lobj}")
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
        # id resolution / payload validation, per op
        ok = True
        if name == "insert_lesson":
            ok = op["module_id"] in module_ids and (
                op.get("after_lesson_id") in (None, "") or
                (lesson_ids.get(op["after_lesson_id"]) is not None
                 and _lesson_module(db, lesson_ids[op["after_lesson_id"]]) == module_ids[op["module_id"]]))
        elif name == "insert_module":
            ok = op.get("after_module_id") in (None, "") or op["after_module_id"] in module_ids
        elif name in ("modify_lesson", "remove_lesson"):
            ok = op["lesson_id"] in lesson_ids
        elif name == "move_lesson":
            ok = (op["lesson_id"] in lesson_ids and op["to_module_id"] in module_ids and
                  (op.get("after_lesson_id") in (None, "") or
                   (lesson_ids.get(op["after_lesson_id"]) is not None
                    and _lesson_module(db, lesson_ids[op["after_lesson_id"]]) == module_ids[op["to_module_id"]])))
        elif name == "update_blueprint":
            # Not an id to resolve but a payload to validate: a malformed blueprint
            # dropped here can never reach course.meta (constraint #2, generalised).
            try:
                validate_blueprint(op["blueprint"])
            except BlueprintInvalid as e:
                log.warning("revise: dropping update_blueprint op — invalid blueprint (%s)", e.code)
                ok = False
        if not ok:
            log.warning("revise: dropping %s op — an id/payload does not resolve under root %s",
                        name, root_id)
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
