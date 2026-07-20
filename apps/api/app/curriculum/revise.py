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

NOTHING IN THE PLANNER WRITES. plan_revision is a pure read. validate_ops resolves
every id against the live tree and DROPS what does not resolve, so a hallucinated
module id can never reach apply. apply_revision (below) is the only writer, and it
commits exactly once.
"""
from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select

from app.curriculum.blueprint import (
    BlueprintInvalid,
    blueprint_from_course_meta,
    section_keys,
    validate_blueprint,
)
from app.curriculum.corpus import CURRICULUM_SYSTEM, CURRICULUM_SYSTEM_SLICE_ID
from app.curriculum.ground import ground_topic
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
#
# `add_segment` / `edit_segment` / `remove_segment` (2026-07-20, Spec A) are the
# SURGICAL ops: they target one SEGMENT inside a lesson rather than the whole
# lesson `modify_lesson` rewrites. Apply only creates/marks/deletes the segment
# block here — GENERATING a queued segment's body is a later task (the job), same
# division as every other queued op in this module.
_OP_ENUM = [
    "insert_lesson", "insert_module", "modify_lesson", "move_lesson",
    "remove_lesson", "update_blueprint",
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
                                                   "blueprint section key; omit for a custom, unfiled segment"},
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
    "add_segment": ("lesson_id", "title", "instruction"),
    "edit_segment": ("segment_id", "instruction"),
    "remove_segment": ("segment_id",),
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
    "{retrieved_block}"
    "\nTHE TUTOR ASKS:\n{instruction}\n"
    "\nReference modules and lessons ONLY by an [id] shown above. Every op needs a "
    "one-sentence `reason`. Tier any new module honestly.\n"
    "\n{language_directive}\n\n{answer_in}"
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


def build_revise_messages(*, course_title, brief, language, tree_text, instruction,
                          retrieved=None, source=None) -> list[dict]:
    """Pure. The curriculum SYSTEM message (shared, so overrides re-mint one cache,
    not one-per-variant), then every request-specific fact after it — the compact
    tree, the targeted retrieval passages, and the tutor's instruction last.

    `retrieved` is the pre-formatted grounding block (`ground_topic` passages), or
    None when retrieval found nothing. It grounds CITATIONS only; the tail's freedom
    directive tells the model an empty/thin block must not narrow what it suggests.
    No whole-library prefix here — that is curriculum GENERATION's path, not the
    revise chat's (see the module docstring)."""
    system = resolve(source, CURRICULUM_SYSTEM_SLICE_ID, CURRICULUM_SYSTEM)
    content = resolve(source, REVISE_SLICE_ID, REVISE_TAIL).format(
        course_title=course_title,
        course_brief_block=(REVISE_BRIEF_BLOCK.format(brief=brief) if brief else ""),
        tree=tree_text,
        retrieved_block=(REVISE_RETRIEVED_BLOCK.format(retrieved=retrieved) if retrieved else ""),
        instruction=instruction.strip(),
        language_directive=language_directive(language, source),
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
    drop. Returns {summary, ops} — ops trimmed, ids kept as the model's strings
    (apply re-resolves them)."""
    module_ids, lesson_ids = _tree_ids(db, root_id)
    course = db.get(Block, root_id)
    enabled_keys = set(section_keys(blueprint_from_course_meta(course.meta if course else None)))
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
        elif name == "update_blueprint":
            # Not an id to resolve but a payload to validate: a malformed blueprint
            # dropped here can never reach course.meta (constraint #2, generalised).
            try:
                validate_blueprint(op["blueprint"])
            except BlueprintInvalid as e:
                log.warning("revise: dropping update_blueprint op — invalid blueprint (%s)", e.code)
                ok = False
        elif name == "add_segment":
            ok = op["lesson_id"] in lesson_ids
            section_key = op.get("section_key")
            if ok and section_key and section_key not in enabled_keys:
                log.warning(
                    "revise: dropping add_segment op — section_key %r is not an enabled "
                    "blueprint section (enabled: %s)", section_key, sorted(enabled_keys))
                ok = False
        elif name in ("edit_segment", "remove_segment"):
            ok = _segment_course_id(db, op["segment_id"]) == root_id
        if not ok:
            log.warning("revise: dropping %s op — an id/payload does not resolve under root %s",
                        name, root_id)
            continue
        kept.append(op)
    return {"summary": raw.get("summary") or "", "ops": kept}


REVISE_RETRIEVAL_K = 8


def plan_revision(db, root_id: uuid.UUID, *, instruction: str) -> dict:
    """The whole read-only planner. Mutates nothing.

    Grounds via TARGETED RETRIEVAL for the instruction topic (`ground_topic`),
    scoped to the course's own `source_ids` — NOT `build_curriculum_context`'s whole
    library/canon (which was slow and made `claude -p` exit 1 for a one-topic ask).
    The retrieved passages ground citations only; `build_revise_messages`' directive
    keeps the model free to suggest beyond them."""
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
        retrieved=retrieved, source=db,
    )
    raw = get_provider().guided_json(messages, REVISION_PLAN_SCHEMA, role="plan")
    return validate_ops(db, root_id, raw)


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
                lesson.meta = {**(lesson.meta or {}), "draft_status": "queued", "error": None,
                               "revise_instruction": op["instruction"], "prev_body": lesson.body}
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
