"""REVISE — read a curriculum + the passages relevant to ONE instruction, PLAN
structural changes, mutate nothing. Apply is a separate, approval-gated step
(apply_revision).

GROUNDED BY TARGETED RETRIEVAL, NOT THE WHOLE LIBRARY. A revise is one focused
ask ("add a lesson on the DS-1"), and running the whole-library/canon full-context
planner for every message was slow, expensive, and the thing that made `claude -p`
exit 1 on a large corpus — for a request that touches one topic. So `plan_revision`
grounds via `ground_topic` (the SAME BM25 + e5 retrieval the oversized-library
draft path and per-block `refine` already use), scoped to the course's own
`source_ids`, and feeds the model the retrieved passages plus the compact tree.
The passages ground CITATIONS ONLY — they never cap what the model may suggest;
see `build_revise_messages`. NOTE: curriculum GENERATION (wizard/outline/draft)
still runs full-context/canon — this retrieval path is the revise chat alone.

The tree is serialised COMPACTLY — ids + titles + objectives + one-line summaries,
never lesson bodies — because the model needs to locate an insertion point, not
re-read 45,000 words it already wrote. `role="plan"` guided_json, same None-vs-[]
source_ids rule as the rest of the curriculum path. The task in the tail is not
"design one module" but "propose a list of ops on THIS tree".

THE PLANNER SEES THE SEGMENT TREE AND THE BLUEPRINT (2026-07-20, Task 3 — the fix
for the "structurally impossible plan" incident of the same date). Before this,
`compact_tree_text` stopped at lessons: the model could not target a segment it
could not see, and had no way to know which blueprint sections a lesson was even
built from — so it proposed `modify_lesson` rewrites for asks a surgical
`edit_segment` would have covered, or an `add_segment` under a `section_key` the
current blueprint had disabled. Now the tree carries one indented `[id] title`
line per segment, and `build_revise_messages` renders `REVISE_BLUEPRINT_BLOCK` —
the course's own enabled/disabled section keys — right after it. The tail's
guidance follows from having both: prefer the surgical ops (Tasks 1-2) over
`modify_lesson`, couple a new RECURRING section to `update_blueprint` +
per-lesson `add_segment` in the same plan, and say so in `summary`, never plan
around it, when the current blueprint makes the request impossible.

NOTHING IN THE PLANNER WRITES. plan_revision is a pure read. validate_ops resolves
every id against the live tree and DROPS what does not resolve, so a hallucinated
module id can never reach apply. apply_revision (below) is the only writer, and it
commits exactly once.

THE "add a homework section" INCIDENT (2026-07-20) — WHY `set_section_enabled`
EXISTS AND `plan_revision` REPAIRS ONCE. A tutor asked for a homework section on
every lesson. The planner correctly saw the coupled shape (enable the section,
`add_segment` per lesson) but had only `update_blueprint` to enable it with, and
`update_blueprint` requires the FULL replacement blueprint object — sections,
weights, kinds, audiences — which the model is never shown (`REVISE_BLUEPRINT_
BLOCK` is a SUMMARY: keys and labels only). It could not reproduce that JSON, so
`update_blueprint` was dropped ("missing ['blueprint']"), the stored+plan-
blueprint union in `validate_ops` never fired, and every coupled `add_segment`
died with it ("not an enabled blueprint section"). `set_section_enabled` fixes
the ROOT CAUSE: flipping one section's `enabled` (+ optional label) needs no
full blueprint at all — `apply_revision` rebuilds it from what is already
stored. `validate_ops` now returns WHY each op was dropped (`dropped`), and
`plan_revision` spends exactly one repair re-prompt (mirroring draft.py's
citation-repair precedent) naming every drop before giving up — so a plan that
used to come back `{"summary": <rosy>, "ops": []}` with no trace of what broke
now either self-corrects or tells the tutor honestly what did not survive.
"""
from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select

from app.curriculum.sanitize import strip_inline_citations
from app.curriculum.blueprint import (
    BlueprintInvalid,
    blueprint_from_course_meta,
    section_keys,
    section_labels,
    validate_blueprint,
)
from app.curriculum.corpus import CURRICULUM_SYSTEM, CURRICULUM_SYSTEM_SLICE_ID
from app.curriculum.ground import ground_topic
from app.curriculum.outline import TIER_ORDER
from app.i18n import answer_in, curriculum_style, language_directive
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
#
# `add_segment` / `edit_segment` / `remove_segment` (2026-07-20, Spec A) are the
# SURGICAL ops: they target one SEGMENT inside a lesson rather than the whole
# lesson `modify_lesson` rewrites. Apply only creates/marks/deletes the segment
# block here — GENERATING a queued segment's body is a later task (the job), same
# division as every other queued op in this module.
#
# `set_section_enabled` (2026-07-20, hotfix for the "homework section" incident)
# flips ONE existing blueprint section's `enabled` flag (and, optionally, its
# label) without the model ever having to reproduce the FULL blueprint object —
# which it cannot: `REVISE_BLUEPRINT_BLOCK` only shows the tutor-facing SUMMARY
# (enabled/disabled keys + labels), never the full section dicts (description,
# weight, kind, audience) `update_blueprint`'s `blueprint` field requires. A
# planner asked to "add a homework section" had no way to satisfy
# `update_blueprint`'s required field and dropped the whole op — silently
# taking every coupled `add_segment(section_key="homework")` down with it (the
# stored+plan-blueprint union in `validate_ops` never fired because the
# update_blueprint op itself never survived). `update_blueprint` stays for a
# FULL restructure (new sections, reordering, reweighting); enabling/disabling/
# renaming ONE section that already exists is `set_section_enabled`, always.
_OP_ENUM = [
    "insert_lesson", "insert_module", "modify_lesson", "move_lesson",
    "remove_lesson", "update_blueprint", "set_section_enabled",
    "add_segment", "edit_segment", "remove_segment",
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
                                  "description": "modify_lesson/move_lesson/remove_lesson: the target lesson id; "
                                                 "add_segment: the lesson to add the new segment to"},
                    "segment_id": {"type": "string",
                                   "description": "edit_segment/remove_segment: the target segment id"},
                    "section_key": {"type": "string",
                                    "description": "add_segment: file the new segment under this ENABLED "
                                                   "blueprint section key; omit for a custom, unfiled segment. "
                                                   "set_section_enabled: the section to enable/disable/rename — "
                                                   "must already exist in the blueprint, enabled or not"},
                    "enabled": {"type": "boolean",
                                "description": "set_section_enabled: true to enable, false to disable. "
                                               "Omit for true (the common case: enabling a section)."},
                    "label": {"type": "string",
                              "description": "set_section_enabled: optional new label for this section, in "
                                             "THIS COURSE'S OWN LANGUAGE only — the other language's label is "
                                             "left untouched. Omit to keep the current label."},
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "instruction": {"type": "string",
                                    "description": "modify_lesson: what to change about the lesson; "
                                                   "add_segment/edit_segment: what the new/edited segment should say"},
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
    "set_section_enabled": ("section_key",),
    "add_segment": ("lesson_id", "title", "instruction"),
    "edit_segment": ("segment_id", "instruction"),
    "remove_segment": ("segment_id",),
}


def compact_tree_text(db, course: Block) -> str:
    """The tree as the model needs it to locate an insertion point: every module
    and lesson with its id, title and objective, and — one indented line each —
    every SEGMENT a lesson already has, id + title only. This is what the
    2026-07-20 "structurally impossible plan" incident was missing: a planner
    that could see modules and lessons but not the segment tree beneath them had
    no way to know a surgical `edit_segment`/`remove_segment` even had a target,
    and proposed `modify_lesson` rewrites (or worse) for asks a one-segment edit
    would have covered.

    NO LESSON OR SEGMENT BODIES. A drafted lesson's `body` is a one-line summary
    (persist_lesson sets it from the draft's `summary`), but it is still content
    the planner does not need to say "add a lesson after this one" — and an
    UN-drafted lesson's body carries its objective, which we already print. The
    full section prose lives in the segments' `body` (45k words across a lesson)
    and never comes near here — only the id and title the model needs to
    reference one with `edit_segment`/`remove_segment`, or to place a new one
    relative to with `add_segment`. Keeping the tree to ids + titles + objectives
    is the token discipline the whole read-only planner rides on (Global
    Constraint #5)."""
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
            segments = db.scalars(
                select(Block).where(Block.parent_id == l.id, Block.kind == "segment").order_by(Block.order)
            ).all()
            for s in segments:
                lines.append(f"    [{s.id}] {s.title}")
    return "\n".join(lines) or "(the course has no modules yet)"


REVISE_SLICE_ID = "curriculum.revise"
REVISE_TAIL = (
    "YOUR TASK: propose STRUCTURAL revisions to an existing course, as a list of "
    "operations. Do NOT rewrite lessons here — insert, move, modify (by "
    "instruction), or remove them, and explain WHY each change belongs.\n"
    "\nUSE YOUR FULL KNOWLEDGE, FREELY. Propose and teach whatever genuinely "
    "serves the course and answers what the tutor asked — do NOT narrow, hedge, or "
    "refuse a good revision just because the passages below do not cover it. Those "
    "passages are retrieved from his library for ONE purpose: grounding CITATIONS. "
    "They are a slice of his shelf, never the limit of the subject or of what you "
    "may suggest. The ONLY discipline is citation honesty — point a new module at "
    "the 'library' tier ONLY where a retrieved passage genuinely supports it (never "
    "misattribute a claim to a book), and where his library is thin, say so plainly "
    "and tier it 'general_knowledge', teaching it well from what you know. A missing "
    "or incomplete passage is never a reason to leave a gap in his course.\n"
    "\nCOURSE: {course_title}{course_brief_block}\n"
    "\nTHE COURSE AS IT STANDS (teaching order; [id] is what you reference):\n{tree}\n"
    "{blueprint_block}"
    "{retrieved_block}"
    "\nTHE TUTOR ASKS:\n{instruction}\n"
    "\nReference modules, lessons and segments ONLY by the EXACT bracketed [id] "
    "shown above — e.g. [3fae1c2b-...] — NEVER the M1/L1/M2-L3-style position "
    "label printed before it. That label is for YOUR orientation only; an id you "
    "build from it (like \"L1\" or \"M1-L1\") does not exist in the tree and the "
    "whole op will be silently dropped. Every op needs a one-sentence `reason`. "
    "Tier any new module honestly.\n"
    "\nPREFER SURGICAL OPS. Reach for add_segment, edit_segment or remove_segment "
    "before modify_lesson whenever the request targets one piece of a lesson — a "
    "new paragraph, a rewritten explanation, a section that no longer belongs. "
    "modify_lesson rewrites the WHOLE lesson; use it only when the request genuinely "
    "needs the whole thing re-taught, not as the default move.\n"
    "\nENABLING, DISABLING OR RENAMING A SECTION THAT SHOULD RECUR ACROSS LESSONS "
    "is a set_section_enabled op, NOT update_blueprint — you are only ever shown "
    "a SUMMARY of the blueprint above (its enabled/disabled keys and labels), "
    "never the full section objects update_blueprint's `blueprint` field "
    "requires, so you cannot reproduce that JSON and the op will be dropped if "
    "you try. To enable a disabled section (e.g. \"homework\") and start filing "
    "material under it, propose set_section_enabled (section_key, enabled: true, "
    "and label if renaming) TOGETHER WITH an add_segment per affected lesson "
    "carrying that same section_key, in the SAME plan — a section you enable but "
    "file no segments under teaches nothing. Reach for update_blueprint only for "
    "a genuine full restructure: a brand-new section the blueprint does not have "
    "at all, reordering, or reweighting.\n"
    "\nIF THE CURRENT BLUEPRINT MAKES THE REQUEST IMPOSSIBLE AS ASKED, SAY SO "
    "PLAINLY in `summary` — never silently plan around it or quietly substitute "
    "something smaller.\n"
    "\n{language_directive}\n{curriculum_style}\n\n{answer_in}"
)
REVISE_BRIEF_BLOCK = "\n\nWHAT THE TUTOR WANTS FROM THIS COURSE, IN HIS OWN WORDS:\n{brief}"
# The passages `ground_topic` retrieved for THIS instruction — for grounding
# citations, NOT a cap on what the model may propose (see the freedom directive in
# REVISE_TAIL). Empty when retrieval found nothing, which is fine: the model then
# suggests from general knowledge, exactly as the directive tells it to.
REVISE_RETRIEVED_BLOCK = (
    "\nRELEVANT PASSAGES FROM HIS LIBRARY (retrieved for THIS request — for "
    "grounding citations only, not a limit on what you may propose):\n{retrieved}\n"
)
# The lesson BLUEPRINT this course drafts under — the 2026-07-20 incident's root
# cause was a planner that could see the tree but not the SHAPE each lesson is
# built from, so it proposed sections the blueprint had disabled and structural
# changes the current shape could not hold. `enabled` names the sections
# add_segment's `section_key` may target today; `disabled` are the ones a
# `update_blueprint` op would need to re-enable first — never silently worked
# around.
REVISE_BLUEPRINT_BLOCK = (
    "\nCURRENT LESSON STRUCTURE (blueprint) — every lesson in this course is built "
    "from these sections; add_segment's section_key must be one of the ENABLED "
    "ones, or omitted for a custom, unfiled segment:\n"
    "enabled: {enabled}\ndisabled: {disabled}\n"
)


def _blueprint_block_text(course_meta: dict | None, language: str) -> str:
    """`REVISE_BLUEPRINT_BLOCK`, filled from the course's own blueprint (its frozen
    `meta["blueprint"]`, or the code default for a course that has none —
    `blueprint_from_course_meta`'s own fallback rule). Labelled in the course's
    language, same as everything else the tutor-facing UI shows for a section."""
    bp = blueprint_from_course_meta(course_meta)
    labels = section_labels(bp, language)
    enabled = [s["key"] for s in bp["sections"] if s.get("enabled", True)]
    disabled = [s["key"] for s in bp["sections"] if not s.get("enabled", True)]
    fmt = lambda keys: ", ".join(f"{k} ({labels[k]})" for k in keys) or "(none)"
    return REVISE_BLUEPRINT_BLOCK.format(enabled=fmt(enabled), disabled=fmt(disabled))


def build_revise_messages(*, course_title, brief, language, tree_text, instruction,
                          retrieved=None, course_meta=None, source=None) -> list[dict]:
    """Pure. The curriculum SYSTEM message (shared, so overrides re-mint one cache,
    not one-per-variant), then every request-specific fact after it — the compact
    tree, the current blueprint, the targeted retrieval passages, and the tutor's
    instruction last.

    `retrieved` is the pre-formatted grounding block (`ground_topic` passages), or
    None when retrieval found nothing. It grounds CITATIONS only; the tail's freedom
    directive tells the model an empty/thin block must not narrow what it suggests.
    No whole-library prefix here — that is curriculum GENERATION's path, not the
    revise chat's (see the module docstring).

    `course_meta` is the course's own `meta` dict (or None), fed straight to
    `blueprint_from_course_meta` — its fallback to the code default means every
    call here (including a bare `course_meta=None`, e.g. a caller that has not
    threaded a course through yet) renders SOME blueprint block, never a hole."""
    system = resolve(source, CURRICULUM_SYSTEM_SLICE_ID, CURRICULUM_SYSTEM)
    content = resolve(source, REVISE_SLICE_ID, REVISE_TAIL).format(
        course_title=course_title,
        course_brief_block=(REVISE_BRIEF_BLOCK.format(brief=brief) if brief else ""),
        tree=tree_text,
        blueprint_block=_blueprint_block_text(course_meta, language),
        retrieved_block=(REVISE_RETRIEVED_BLOCK.format(retrieved=retrieved) if retrieved else ""),
        instruction=instruction.strip(),
        language_directive=language_directive(language, source),
        # The revise planner WRITES course material — the titles and objectives
        # it proposes land verbatim on the tutor's board (`apply_revision`), so
        # it carries the curriculum register exactly like the other five
        # content flows (the rule `app.i18n.curriculum_style` documents).
        # Without it, a Greek course grew planner-register module titles.
        curriculum_style=curriculum_style(language, source),
        answer_in=answer_in(language, source),
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]


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


def _segment_course_id(db, segment_id_str: str) -> uuid.UUID | None:
    """The course id a segment lives under, walking its fixed-depth ancestry
    (segment -> lesson -> module -> course), or None if `segment_id_str` does not
    resolve to a live `kind=="segment"` block with that whole chain intact —
    covers a malformed id, an id that names a lesson/module instead, and a
    segment that is a real block but orphaned or outside a course."""
    try:
        segment_id = uuid.UUID(segment_id_str)
    except (ValueError, TypeError, AttributeError):
        return None
    seg = db.get(Block, segment_id)
    if seg is None or seg.kind != "segment" or seg.parent_id is None:
        return None
    lesson = db.get(Block, seg.parent_id)
    if lesson is None or lesson.kind != "lesson" or lesson.parent_id is None:
        return None
    module = db.get(Block, lesson.parent_id)
    return module.parent_id if module is not None and module.kind == "module" else None


def validate_ops(db, root_id: uuid.UUID, raw: dict) -> dict:
    """Drop every op whose required fields are missing or whose ids do not resolve
    to a real block of the right kind UNDER THIS ROOT (constraint #2). Logs each
    drop WITH the offending value (2026-07-20 hotfix: a bare "an id/payload does
    not resolve" told nobody whether the model sent a malformed uuid, an
    M1/L1-style shorthand, or a real id from a different course — see this
    function's `detail` variable). Returns {summary, ops, dropped} — `ops`
    trimmed, ids kept as the model's strings (apply re-resolves them); `dropped`
    is `[{"op": <the raw op>, "reason": <why>}]` for every op that did NOT make
    it into `ops`, in the SAME order they were seen. `plan_revision` reads
    `dropped` to decide whether to run its one-shot repair pass, and forwards it
    to the tutor-facing plan so an approval never hides that something was
    silently cut."""
    module_ids, lesson_ids = _tree_ids(db, root_id)
    course = db.get(Block, root_id)
    stored_bp = blueprint_from_course_meta(course.meta if course else None)
    stored_keys = {s["key"] for s in stored_bp["sections"]}      # enabled OR disabled
    enabled_keys = set(section_keys(stored_bp))
    # The coupled shape REVISE_TAIL instructs — enabling a section IN THE SAME
    # PLAN as `add_segment` ops that target it, via `set_section_enabled` (the
    # common case) or a full `update_blueprint` restructure — was landing with
    # every add_segment dropped, because section_key was checked against the
    # STORED blueprint only, which neither op has touched yet (apply runs ops in
    # order, but validate_ops runs before apply). Widen the accepted keys to the
    # union of stored + whatever the plan's OWN blueprint-shaping ops would
    # enable — but only for an op that itself resolves/validates; a
    # hallucinated section_key or an invalid update_blueprint grants nothing
    # here, same as before.
    #
    # 2026-07-20 hotfix: the union used to only GROW — a `set_section_enabled`
    # that DISABLES an already-enabled key never took it back out, so a plan
    # that disabled X while ALSO adding a segment under section_key=X let that
    # add through anyway (the disable "won" at apply, the stray segment stayed
    # filed under a section the tutor just turned off). Compute each
    # `set_section_enabled` op's FINAL per-key effect first — last op for a
    # given key wins, same as apply_revision's own sequential rebuild — and
    # apply that against the stored-enabled baseline (add OR remove) BEFORE
    # union-ing in update_blueprint's keys, which still only ever widens.
    toggles: dict[str, bool] = {}
    for op in raw.get("ops") or []:
        if op.get("op") == "set_section_enabled":
            key = op.get("section_key")
            if key in stored_keys:
                toggles[key] = bool(op.get("enabled", True))
    for key, is_enabled in toggles.items():
        if is_enabled:
            enabled_keys.add(key)
        else:
            enabled_keys.discard(key)
    for op in raw.get("ops") or []:
        if op.get("op") == "update_blueprint":
            try:
                plan_bp = validate_blueprint(op.get("blueprint"))
            except BlueprintInvalid:
                continue
            enabled_keys |= set(section_keys(plan_bp))
    kept: list[dict] = []
    dropped: list[dict] = []
    for op in raw.get("ops") or []:
        name = op.get("op")
        if name not in _REQUIRED:
            detail = f"unknown op {name!r}"
            log.warning("revise: dropping op with unknown/absent 'op': %r", name)
            dropped.append({"op": op, "reason": detail})
            continue
        missing = [f for f in _REQUIRED[name] if not op.get(f)]
        if missing:
            detail = f"missing required field(s) {missing}"
            log.warning("revise: dropping %s op — missing %s", name, missing)
            dropped.append({"op": op, "reason": detail})
            continue
        # id resolution / payload validation, per op. `detail` names the exact
        # offending value on failure — read by the shared log line + `dropped`
        # entry below, never left as a bare "does not resolve".
        ok = True
        detail = ""
        if name == "insert_lesson":
            ok = op["module_id"] in module_ids and (
                op.get("after_lesson_id") in (None, "") or
                (lesson_ids.get(op["after_lesson_id"]) is not None
                 and _lesson_module(db, lesson_ids[op["after_lesson_id"]]) == module_ids[op["module_id"]]))
            if not ok:
                detail = (f"module_id={op.get('module_id')!r} / "
                          f"after_lesson_id={op.get('after_lesson_id')!r} does not resolve")
        elif name == "insert_module":
            ok = op.get("after_module_id") in (None, "") or op["after_module_id"] in module_ids
            if not ok:
                detail = f"after_module_id={op.get('after_module_id')!r} does not resolve"
        elif name in ("modify_lesson", "remove_lesson"):
            ok = op["lesson_id"] in lesson_ids
            if not ok:
                detail = f"lesson_id={op.get('lesson_id')!r} does not resolve"
        elif name == "move_lesson":
            # A lesson "moved after itself" (after_lesson_id == lesson_id) resolves
            # fine id-by-id — both are the SAME live lesson — but is a degenerate op:
            # edit._move_block excludes the block being moved from its destination
            # siblings, so `after` can never be found once it equals the block's own
            # id, and it raises StopIteration mid apply_revision (rolling back the
            # whole approved plan). Caught here instead, same as any other op whose
            # id/payload does not resolve.
            self_move = (op.get("after_lesson_id") not in (None, "") and
                         op.get("after_lesson_id") == op.get("lesson_id"))
            ok = (not self_move and op["lesson_id"] in lesson_ids and op["to_module_id"] in module_ids and
                  (op.get("after_lesson_id") in (None, "") or
                   (lesson_ids.get(op["after_lesson_id"]) is not None
                    and _lesson_module(db, lesson_ids[op["after_lesson_id"]]) == module_ids[op["to_module_id"]])))
            if not ok:
                detail = (f"lesson_id={op.get('lesson_id')!r} / to_module_id={op.get('to_module_id')!r} / "
                          f"after_lesson_id={op.get('after_lesson_id')!r} does not resolve"
                          + (" (a lesson cannot move after itself)" if self_move else ""))
        elif name == "update_blueprint":
            # Not an id to resolve but a payload to validate: a malformed blueprint
            # dropped here can never reach course.meta (constraint #2, generalised).
            try:
                validate_blueprint(op["blueprint"])
            except BlueprintInvalid as e:
                detail = f"invalid blueprint ({e.code})"
                ok = False
        elif name == "set_section_enabled":
            # section_key must already exist in the STORED blueprint, enabled or
            # not — set_section_enabled flips a section, it never invents one
            # (that is what a full update_blueprint restructure is for).
            ok = op["section_key"] in stored_keys
            if not ok:
                detail = (f"section_key={op.get('section_key')!r} is not a section of this "
                          f"course's blueprint (has: {sorted(stored_keys)})")
        elif name == "add_segment":
            ok = op["lesson_id"] in lesson_ids
            if not ok:
                detail = f"lesson_id={op.get('lesson_id')!r} does not resolve"
            else:
                section_key = op.get("section_key")
                if section_key and section_key not in enabled_keys:
                    ok = False
                    if toggles.get(section_key) is False:
                        # Disabled by THIS SAME PLAN's own set_section_enabled — name
                        # that explicitly, not just "not enabled", so the tutor-facing
                        # drop reason reads as a consequence of the disable, not a
                        # mystery.
                        detail = (f"section_key={section_key!r} was disabled by this plan's "
                                  f"set_section_enabled op (enabled: {sorted(enabled_keys)})")
                    else:
                        detail = (f"section_key={section_key!r} is not an enabled blueprint section "
                                  f"(enabled: {sorted(enabled_keys)})")
        elif name in ("edit_segment", "remove_segment"):
            ok = _segment_course_id(db, op["segment_id"]) == root_id
            if not ok:
                detail = f"segment_id={op.get('segment_id')!r} does not resolve under this course"
        if not ok:
            log.warning("revise: dropping %s op — %s (root %s)", name, detail, root_id)
            dropped.append({"op": op, "reason": detail or "id/payload does not resolve"})
            continue
        kept.append(op)
    return {"summary": raw.get("summary") or "", "ops": kept, "dropped": dropped}


def compute_impact(ops: list[dict]) -> dict:
    """Pure, deterministic blast-radius summary of a VALIDATED ops list — NEVER
    LLM-derived. The model's `reason` text on each op is its own claim about why
    a change belongs; this counts what the op actually DOES, from the op shape
    alone, so a tutor-facing approval card can show honest numbers the model
    cannot spin. Called from `_validate_pending_revision` (chat.py) on the
    already-`validate_ops`-cleaned plan, so a dropped/hallucinated op is never
    counted.

    Buckets:
      - rewrites: `modify_lesson` + `move_lesson` — an EXISTING lesson's content
        or position changes.
      - segment_additions / segment_edits / segment_removals: the three
        surgical segment ops, 1:1.
      - lesson_removals: `remove_lesson`.
      - lessons_added: `insert_lesson` (1 each) + the length of each
        `insert_module` op's `lessons` array. An `insert_module` op carries its
        new lessons INLINE (see `REVISION_PLAN_SCHEMA["lessons"]` and
        `apply_revision`'s `insert_module` branch) rather than as separate ops,
        so counting them means reading that array, not counting `insert_module`
        occurrences; a module with no/empty `lessons` (e.g. a TIER_GAP module,
        which `apply_revision` fills with `gap_body` instead of lessons) adds 0.
      - blueprint_changed: any `update_blueprint` OR `set_section_enabled` op
        present — either one changes `course.meta["blueprint"]`.
      - destructive: True iff something above REMOVES or REWRITES existing
        material (`rewrites`, `lesson_removals`, `segment_removals`) — pure
        additions (`insert_lesson`, `insert_module`, `add_segment`) and a bare
        blueprint reshape (`update_blueprint`, `set_section_enabled`) are not,
        by themselves, destructive."""
    rewrites = 0
    segment_additions = 0
    segment_edits = 0
    segment_removals = 0
    lesson_removals = 0
    lessons_added = 0
    blueprint_changed = False
    for op in ops:
        name = op.get("op")
        if name in ("modify_lesson", "move_lesson"):
            rewrites += 1
        elif name == "add_segment":
            segment_additions += 1
        elif name == "edit_segment":
            segment_edits += 1
        elif name == "remove_segment":
            segment_removals += 1
        elif name == "remove_lesson":
            lesson_removals += 1
        elif name == "insert_lesson":
            lessons_added += 1
        elif name == "insert_module":
            lessons_added += len(op.get("lessons") or [])
        elif name in ("update_blueprint", "set_section_enabled"):
            blueprint_changed = True
    return {
        "rewrites": rewrites,
        "segment_additions": segment_additions,
        "segment_edits": segment_edits,
        "segment_removals": segment_removals,
        "lesson_removals": lesson_removals,
        "lessons_added": lessons_added,
        "blueprint_changed": blueprint_changed,
        "destructive": rewrites > 0 or lesson_removals > 0 or segment_removals > 0,
    }


REVISE_RETRIEVAL_K = 8

# ---------------------------------------------------------------------------
# The ONE repair pass (2026-07-20 hotfix). Mirrors draft.py's citation-repair
# precedent EXACTLY (`_repair_message`/`REPAIR_MESSAGE`/`REPAIR_SLICE_ID` there):
# a single corrective re-prompt naming what went wrong, one retry, whatever
# survives THAT is final. Before this, a plan that dropped ops (a missing
# `blueprint`, an M1/L1-shorthand id) returned `{"summary": <rosy>, "ops": []}`
# with nothing telling the tutor — or the model — that anything had been cut.
# ---------------------------------------------------------------------------
REVISE_REPAIR_MESSAGE = (
    "STOP. {n} of the operations you proposed were REJECTED by validation and "
    "never reached the tutor:\n{detail}\n"
    "Two rules explain almost every rejection: (1) every id you reference — "
    "module_id, lesson_id, segment_id, after_lesson_id, to_module_id — MUST be "
    "the EXACT bracketed [id] shown in the tree above, never an M1/L1-style "
    "position label; (2) to enable/disable/rename ONE existing blueprint "
    "section, use set_section_enabled, not update_blueprint — update_blueprint "
    "needs the FULL section objects, which you were never shown and cannot "
    "reproduce. Produce the WHOLE plan again: fix every rejected operation (or "
    "drop it yourself if it no longer makes sense), and keep everything that "
    "was already correct."
)
REVISE_REPAIR_SLICE_ID = "curriculum.revise.repair"


# ---------------------------------------------------------------------------
# "Talk it through first" for the revise chat — transcript -> ONE crafted
# instruction. The same trick the planning chat already has (`interview.
# DISTILL_SYSTEM` -> brief -> outline), pointed at revision: the tutor thinks
# out loud with the assistant, reaches a conclusion, and one cheap call writes
# the instruction he MEANT — better than the one he would have typed — which
# lands in his composer to review, edit, and send. Nothing is planned or
# applied by this call; it only writes text.
# ---------------------------------------------------------------------------
REVISE_DISTILL_SYSTEM = (
    "You read a conversation between a guitar TUTOR and an assistant about "
    "changes the tutor wants to make to an existing course. Distill what the "
    "TUTOR actually decided into ONE clear, complete revision instruction, "
    "written as if the tutor wrote it himself — naming the lessons, modules or "
    "sections concerned in plain words (never internal ids), what should "
    "change in each, and anything that must stay as it is. Keep ONLY "
    "conclusions the tutor stated or clearly agreed to — dead ends and "
    "rejected ideas stay out. No preamble, no commentary; return ONLY the "
    "JSON the schema describes.\n\n{language_directive}\n\n"
    "THE CONVERSATION:\n{transcript}"
)
REVISE_DISTILL_SLICE_ID = "curriculum.revise_distill"

REVISE_DISTILL_SCHEMA = {
    "type": "object",
    "properties": {"instruction": {"type": "string"}},
    "required": ["instruction"],
    "additionalProperties": False,
}


def distill_revise_instruction(db, session) -> str:
    """The revise chat's "crystallize the ask" exit: transcript in, one
    tutor-voiced revision instruction out. Raises ValueError when there is
    nothing to distill (no tutor turns) or the model returns nothing usable —
    the router maps both to a 409 the UI can explain. Mirrors
    `interview.distill_planning_brief` deliberately, including the locale
    preference (the session's own locale, then the default)."""
    from app.i18n import DEFAULT_LOCALE, normalize_locale
    from app.models.chat import Message

    messages = db.scalars(
        select(Message).where(Message.session_id == session.id).order_by(Message.created_at)
    ).all()
    visible = [
        m for m in messages if m.role in ("user", "assistant") and (m.content or "").strip()
    ]
    if not any(m.role == "user" for m in visible):
        raise ValueError("this chat has no tutor turns to distill")

    transcript = "\n".join(f"{m.role.upper()}: {m.content.strip()}" for m in visible)
    lang_code = normalize_locale(session.locale or DEFAULT_LOCALE)

    prompt = resolve(db, REVISE_DISTILL_SLICE_ID, REVISE_DISTILL_SYSTEM).format(
        language_directive=language_directive(lang_code, db), transcript=transcript,
    )
    result = get_provider().guided_json(
        [{"role": "user", "content": prompt}], REVISE_DISTILL_SCHEMA, role="chat",
    )
    instruction = (result.get("instruction") or "").strip()
    if not instruction:
        raise ValueError("distillation produced an empty instruction — try again")
    return instruction


def _revise_repair_message(dropped: list[dict], source=None) -> dict:
    """The one corrective re-prompt `plan_revision` sends back after a plan came
    back with dropped ops — same shape as draft.py's `_repair_message`, applied
    to `validate_ops`'s `dropped` diagnostics instead of citation misses."""
    detail = "\n".join(
        f"  - {d['op'].get('op', '?')}: {d['reason']}" for d in dropped
    ) or "  - the plan came back with no operations at all"
    return {
        "role": "user",
        "content": resolve(source, REVISE_REPAIR_SLICE_ID, REVISE_REPAIR_MESSAGE).format(
            n=len(dropped) or "all", detail=detail,
        ),
    }


def plan_revision(db, root_id: uuid.UUID, *, instruction: str) -> dict:
    """The whole read-only planner. Mutates nothing.

    Grounds via TARGETED RETRIEVAL for the instruction topic (`ground_topic`),
    scoped to the course's own `source_ids` — NOT `build_curriculum_context`'s whole
    library/canon (which was slow and made `claude -p` exit 1 for a one-topic ask).
    The retrieved passages ground citations only; `build_revise_messages`' directive
    keeps the model free to suggest beyond them.

    ONE REPAIR PASS (2026-07-20 hotfix). If `validate_ops` dropped anything, OR
    the model returned a non-empty raw `ops` list that validated down to nothing,
    this sends ONE corrective re-prompt (`_revise_repair_message`, mirroring
    draft.py's citation-repair precedent) naming every dropped op and why, then
    re-validates. Whatever survives THAT pass is final — `dropped` on the
    returned plan reflects only the last attempt, so a tutor reading it sees
    "what is still wrong", not a stale first-pass list."""
    course = db.get(Block, root_id)
    if course is None:
        raise ReviseError(f"curriculum not found: {root_id}")
    if course.kind != "course":
        raise ReviseError(f"block {root_id} is a {course.kind!r}, not a course")

    meta = course.meta or {}
    raw_sources = meta.get("source_ids")
    source_ids = None if raw_sources is None else [uuid.UUID(s) for s in raw_sources]

    # Retrieve the passages relevant to THIS instruction, the same BM25 + e5 way
    # `refine` and the oversized-library draft path do. An empty list is fine — the
    # planner then works from general knowledge (its citation-honesty directive).
    passages = ground_topic(db, instruction, source_ids=source_ids, k=REVISE_RETRIEVAL_K)
    retrieved = "\n\n".join(
        f"[{p.source_title}, p.{p.page_no}] {p.text}" for p in passages
    ) or None

    messages = build_revise_messages(
        course_title=course.title, brief=meta.get("brief"), language=course.language,
        tree_text=compact_tree_text(db, course), instruction=instruction,
        retrieved=retrieved, course_meta=meta, source=db,
    )
    provider = get_provider()
    raw = provider.guided_json(messages, REVISION_PLAN_SCHEMA, role="plan")
    validated = validate_ops(db, root_id, raw)

    raw_ops = raw.get("ops") or []
    needs_repair = bool(validated["dropped"]) or (not validated["ops"] and bool(raw_ops))
    if needs_repair:
        log.warning("revise: plan for root %s dropped %d op(s) — running the one repair pass",
                    root_id, len(validated["dropped"]))
        repair_messages = [*messages, _revise_repair_message(validated["dropped"], source=db)]
        raw = provider.guided_json(repair_messages, REVISION_PLAN_SCHEMA, role="plan")
        validated = validate_ops(db, root_id, raw)

    return validated


# ---------------------------------------------------------------------------
# APPLY — the one writer. ONE transaction, one commit (Global Constraint #3).
# ---------------------------------------------------------------------------
#
# `edit` and the outline helpers are imported HERE, inside apply_revision, rather
# than at module top: it keeps the read-only planner above import-light (it needs
# neither), avoids re-shuffling the prompt registry's call-site/source-ref line
# numbers, and is the same lazy-import move outline.py uses for blueprint_store.


def _queue(block: Block) -> None:
    """Send a block back into the draft queue via a WHOLE-DICT meta reassignment —
    `Block.meta` is plain `sa.JSON` with no `MutableDict`, so `block.meta["k"] = v`
    silently does nothing in production (Global Constraint #4)."""
    block.meta = {**(block.meta or {}), "draft_status": "queued", "error": None}


_SLUG_NON_WORD_RE = re.compile(r"[^\w-]+")


def _slug(title: str) -> str:
    """lowercase, spaces -> '-', strip anything non-word — the tiny helper behind
    a custom segment's `section: "custom:<slug>"` key. Never empty: an all-symbol
    title falls back to "segment" rather than minting `custom:`."""
    slug = _SLUG_NON_WORD_RE.sub("", title.strip().lower().replace(" ", "-"))
    return slug or "segment"


def _recompute_lesson_word_count(db, lesson: Block) -> None:
    """After ANY segment op on `lesson`, its `word_count`/`meets_floor` meta must
    reflect the segments as they now stand — a queued segment's body is "" until
    the (later) generation job fills it in, so this undercounts until then, same
    as any other queued lesson. WHOLE-DICT reassignment (Constraint #4)."""
    segments = db.scalars(
        select(Block).where(Block.parent_id == lesson.id, Block.kind == "segment")
    ).all()
    word_count = sum(len((s.body or "").split()) for s in segments)
    meta = {**(lesson.meta or {}), "word_count": word_count}
    floor_words = meta.get("floor_words")
    if floor_words is not None:
        meta["meets_floor"] = word_count >= floor_words
    lesson.meta = meta


def apply_revision(db, root_id: uuid.UUID, plan: dict) -> dict:
    """Execute an APPROVED plan in ONE transaction (Global Constraint #3). Re-validates
    every id (defence in depth — the chat path round-trips the plan through the
    model, so a garbled id must be dropped, not misapplied). New/changed lessons are
    queued; the caller chains the draft fan-out. Returns {applied, root_id}."""
    from app.curriculum import edit
    from app.curriculum.outline import POLICY_GENERAL, TIER_GAP, clamp_tier, gap_body

    course = db.get(Block, root_id)
    if course is None or course.kind != "course":
        raise ReviseError(f"not a curriculum root: {root_id}")
    validated = validate_ops(db, root_id, plan)           # re-resolve against the live tree
    gap_policy = (course.meta or {}).get("gap_policy") or POLICY_GENERAL
    applied = 0
    try:
        for op in validated["ops"]:
            name = op["op"]
            # The planner's titles/objectives land VERBATIM on the board — no
            # chained draft ever rewrites them — so they pass through the same
            # citation net every drafted body does (`sanitize.py`). A module
            # titled "Συγχορδίες (σελ. 47)" is exactly the leak the net exists
            # to catch, and until now this was the one writer that skipped it.
            for key in ("title", "objective"):
                if isinstance(op.get(key), str):
                    op[key] = strip_inline_citations(op[key])
            for spec in op.get("lessons") or []:
                for key in ("title", "objective"):
                    if isinstance(spec.get(key), str):
                        spec[key] = strip_inline_citations(spec[key])
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
                    # The tutor's policy leaves this topic outside his library — an
                    # honest, empty gap, never invented content (mirrors outline.py).
                    module.body = gap_body(course.language)
                else:
                    for li, spec in enumerate(op["lessons"]):
                        db.add(Block(
                            kind="lesson", title=spec["title"], body=spec["objective"] or None,
                            order=li, parent_id=module.id, language=course.language,
                            meta={"draft_status": "queued", "objective": spec["objective"] or ""}))
            elif name == "move_lesson":
                lesson = edit._move_block(
                    db, uuid.UUID(op["lesson_id"]), uuid.UUID(op["to_module_id"]),
                    after=uuid.UUID(op["after_lesson_id"]) if op.get("after_lesson_id") else None)
                _queue(lesson)                            # re-draft in its new position/context
            elif name == "modify_lesson":
                lesson = db.get(Block, uuid.UUID(op["lesson_id"]))
                # `prev_body` used to be stashed here too, but nothing ever reads it —
                # `_draft_one` now threads the lesson's LIVE SEGMENTS (not this stale
                # top-level `body`, which `persist_lesson` only ever sets from the
                # draft's one-line `summary`) into the re-draft prompt instead
                # (`revise_current`, Spec D). Writing it was a dead write.
                lesson.meta = {**(lesson.meta or {}), "draft_status": "queued", "error": None,
                               "revise_instruction": op["instruction"]}
            elif name == "remove_lesson":
                lesson = db.get(Block, uuid.UUID(op["lesson_id"]))
                parent_id = lesson.parent_id
                db.delete(lesson)
                db.flush()
                if parent_id is not None:
                    edit._renormalise(db, parent_id)
            elif name == "update_blueprint":
                # WHOLE-DICT reassignment (Constraint #4). Re-validated here to store
                # the NORMALISED blueprint. NEVER re-drafts existing lessons — that is
                # the opt-in `POST /curricula/{root}/redraft` route (Unit C), not this.
                course.meta = {**(course.meta or {}), "blueprint": validate_blueprint(op["blueprint"])}
            elif name == "set_section_enabled":
                # ZERO LLM calls: rebuild course.meta["blueprint"] from what is
                # ALREADY there, flipping exactly one section's `enabled` (and,
                # optionally, its label in the course's own language) — the whole
                # point of this op is that the model never has to reproduce the
                # full blueprint object (see the module docstring). Reads the
                # blueprint fresh off `course.meta` (not `validated` at call
                # time) so an EARLIER op in this same plan (an `update_blueprint`
                # ahead of this one) is respected, not clobbered. WHOLE-DICT
                # reassignment through `validate_blueprint`, same as
                # `update_blueprint` above — the stored shape is always the
                # normalised one.
                bp = blueprint_from_course_meta(course.meta)
                lang = course.language or "el"
                sections = []
                for s in bp["sections"]:
                    if s["key"] != op["section_key"]:
                        sections.append(s)
                        continue
                    s = {**s, "enabled": bool(op.get("enabled", True))}
                    if op.get("label"):
                        s = {**s, "label": {**s["label"], lang: op["label"]}}
                    sections.append(s)
                course.meta = {**(course.meta or {}),
                               "blueprint": validate_blueprint({"version": bp["version"], "sections": sections})}
            elif name == "add_segment":
                lesson = db.get(Block, uuid.UUID(op["lesson_id"]))
                siblings = db.scalars(
                    select(Block).where(Block.parent_id == lesson.id, Block.kind == "segment")
                ).all()
                section_key = op.get("section_key")
                seg_meta = {"segment_status": "queued", "segment_instruction": op["instruction"]}
                if section_key:
                    seg_meta["section"] = section_key
                else:
                    # No enabled section named — files as its OWN custom section, never
                    # silently attached to an existing one (persist_lesson's survival
                    # rule keys off `meta.custom`, not this key's shape).
                    seg_meta["custom"] = True
                    seg_meta["section"] = f"custom:{_slug(op['title'])}"
                db.add(Block(
                    kind="segment", title=op["title"], body="", order=len(siblings),
                    parent_id=lesson.id, language=lesson.language, meta=seg_meta))
                db.flush()
                _recompute_lesson_word_count(db, lesson)
            elif name == "edit_segment":
                segment = db.get(Block, uuid.UUID(op["segment_id"]))
                lesson = db.get(Block, segment.parent_id)
                # The refine-flow meta contract VERBATIM (refine.py:169-175) so
                # POST /blocks/{id}/undo (undo_refine, refine.py:179-190) works
                # unchanged on a surgically-edited segment.
                segment.meta = {
                    **(segment.meta or {}),
                    "segment_status": "queued",
                    "segment_instruction": op["instruction"],
                    "prev_body": segment.body,
                    "prev_title": segment.title,
                    "refined": True,
                    "refine_instruction": op["instruction"],
                }
                _recompute_lesson_word_count(db, lesson)
            elif name == "remove_segment":
                segment = db.get(Block, uuid.UUID(op["segment_id"]))
                lesson_id = segment.parent_id
                db.delete(segment)
                db.flush()
                if lesson_id is not None:
                    edit._renormalise(db, lesson_id)
                    lesson = db.get(Block, lesson_id)
                    if lesson is not None:
                        _recompute_lesson_word_count(db, lesson)
            applied += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"applied": applied, "root_id": str(root_id)}
