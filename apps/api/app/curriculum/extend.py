"""Adding ONE module to an EXISTING curriculum — AI-planned, from the same library.

The tutor's "Add a module" used to create an empty titled box, which put the whole
burden of fitting it into the course back on him — the exact work the outline call
does better, because it has read both his library AND the course it is extending.
So the button now asks the model for one module (optionally about a topic the
tutor names), shows it where the outline's modules already live, and queues its
lessons for the SAME draft fan-out that wrote the rest of the course.

THE PROMPT RIDES THE STANDARD PREFIX (`corpus.prefix_messages`): system + cached
library, byte-identical to the lesson drafts'. That is what makes the chained
drafting cheap — this call writes the 90K-token cache entry at 1.25x, and the new
module's 3-5 lesson drafts, which run immediately after in the same job, read it
at 0.1x each.

Everything specific to THIS request — the existing modules, the topic, the course
rhythm — sits in the volatile tail, after the breakpoint.

THE SAME ARGUMENT, ONE LEVEL DOWN, lives at the bottom of this file: "Add a
lesson" used to create a box titled «Νέο μάθημα». `plan_lesson_json` /
`generate_lesson` give it the module's own lessons and the tutor's brief instead.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.curriculum.corpus import LibraryContext, prefix_messages
from app.curriculum.edit import EditError, _add_lesson
from app.curriculum.outline import (
    POLICY_GENERAL,
    TIER_GAP,
    TIER_ORDER,
    _est_minutes,
    _policy_sentence,
    clamp_tier,
    gap_body,
)
from app.i18n import answer_in, curriculum_style, language_directive
from app.prompts.overrides import resolve
from app.llm.factory import get_provider
from app.models.block import Block

log = logging.getLogger(__name__)

# One module, same fields the outline schema gives each of ITS modules — the
# tutor cannot tell (and should not be able to tell) which modules arrived with
# the course and which were added later.
MODULE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "objective": {"type": "string"},
        "tier": {
            "type": "string",
            "enum": list(TIER_ORDER),
            "description": (
                "You have just read the tutor's entire library. Say honestly "
                "where this module's material comes from: 'library' if his own "
                "sources genuinely teach it, 'general_knowledge' if they do not "
                "and you would be writing it from what you know, 'web' if it "
                "needs current information neither of you has. Do NOT say "
                "'library' because a page mentions the word."
            ),
        },
        "coverage_note": {
            "type": "string",
            "description": (
                "One sentence, for the tutor: what in his library covers this "
                "(name the source and pages), or what is missing from it."
            ),
        },
        "lessons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "est_minutes": {"type": "integer"},
                },
                "required": ["title", "objective", "est_minutes"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "objective", "tier", "coverage_note", "lessons"],
    "additionalProperties": False,
}


class ExtendError(ValueError):
    """A module-generation request the course cannot accept (wrong kind, no
    course). The router/job turns it into a 4xx or a failed job — never a 500."""


def _existing_modules(db, course: Block) -> list[Block]:
    return list(db.scalars(
        select(Block)
        .where(Block.parent_id == course.id, Block.kind == "module")
        .order_by(Block.order)
    ))


def _module_lines(db, modules: list[Block]) -> str:
    """The course as the model needs to see it: every module with its tier and
    its lesson titles. Lesson TITLES matter — 'do not repeat yourself' is only
    checkable against what was actually planned, not against module names."""
    lines: list[str] = []
    for i, module in enumerate(modules, start=1):
        meta = module.meta or {}
        lessons = db.scalars(
            select(Block)
            .where(Block.parent_id == module.id, Block.kind == "lesson")
            .order_by(Block.order)
        ).all()
        lines.append(
            f"{i}. {module.title} [{meta.get('tier') or 'general_knowledge'}] — "
            f"{meta.get('objective') or module.body or ''}"
        )
        for lesson in lessons:
            lines.append(f"     - {lesson.title}")
    return "\n".join(lines) or "(the course has no modules yet)"


def _typical_lesson_count(course: Block, modules: list[Block], db) -> int:
    """How many lessons the new module should get: the course's own rhythm.

    The shape's `lessons_per_module` is the plan as generated; the tree may have
    drifted since (added/deleted lessons), so live counts win when they exist.
    """
    counts = [
        len(db.scalars(
            select(Block.id).where(Block.parent_id == m.id, Block.kind == "lesson")
        ).all())
        for m in modules
    ]
    counts = [c for c in counts if c > 0]
    if counts:
        return max(1, round(sum(counts) / len(counts)))
    shape = (course.meta or {}).get("shape") or {}
    per_module = shape.get("lessons_per_module") or []
    if per_module:
        return max(1, round(sum(per_module) / len(per_module)))
    return 4


# THE ADD-MODULE PROMPT, lifted out of the builder byte-identically so the tutor can
# rewrite it. `{topic_block}` carries the two-way branch the `if topic` expression used
# to make — the two halves are their own constants below, so he can edit either.
MODULE_TAIL = (
    "YOUR TASK: design ONE new module to EXTEND an existing course — a title, "
    "a one-sentence objective, and its lessons (titles and one-sentence "
    "objectives only; the lessons are drafted in full later). It must fit the "
    "course's arc: build on what earlier modules teach, and do NOT repeat any "
    "module or lesson the course already has.\n"
    "\nCOURSE: {course_title}"
    "{course_brief_block}"
    "\n\nTHE COURSE AS IT STANDS (in teaching order):\n{existing}\n"
    "{topic_block}\n"
    "\nSHAPE: give it EXACTLY {lesson_count} lessons of {minutes_per_lesson} "
    "minutes each; each will later be drafted to ~{target_words} words.\n"
    "\nTIER THE MODULE HONESTLY. You have read his entire library; you are "
    "the only one who can say whether it actually covers this topic. A module "
    "tiered 'library' will be drafted from his pages and cited to them — if "
    "it is not really in there, that citation is a lie the tutor will click "
    "on. Say 'general_knowledge' instead. That is not a failure; an "
    "unlabelled gap is.\n"
    "\nGAP POLICY: {gap_policy}\n"
    "\n{language_directive}\n"
    "\n{style_directive}\n"
    "\n{answer_in}"
)
MODULE_SLICE_ID = "curriculum.extend"

MODULE_COURSE_BRIEF_BLOCK = "\n\nWHAT THE TUTOR WANTS FROM THIS COURSE, IN HIS OWN WORDS:\n{brief}"
MODULE_TOPIC_BLOCK = "\nTHE NEW MODULE'S TOPIC: {topic}"
MODULE_NO_TOPIC_BLOCK = (
    "\nTHE TUTOR DID NOT NAME A TOPIC. Choose the most valuable module "
    "this course is missing — the thing a student who finished the "
    "existing modules would most need next."
)


def build_module_messages(
    *,
    course_title: str,
    brief: str | None,
    language: str,
    existing: str,
    topic: str | None,
    lesson_count: int,
    minutes_per_lesson: int,
    target_words: int,
    library: LibraryContext,
    gap_policy: str,
    source=None,
) -> list[dict]:
    """The messages for the add-module call. Pure — same testability contract as
    `outline.build_outline_messages`: the prefix is the shared cached one, and
    every request-specific fact sits strictly after it."""
    messages = prefix_messages(library, source)

    content = resolve(source, MODULE_SLICE_ID, MODULE_TAIL).format(
        course_title=course_title,
        course_brief_block=(
            MODULE_COURSE_BRIEF_BLOCK.format(brief=brief) if brief else ""
        ),
        existing=existing,
        topic_block=(
            MODULE_TOPIC_BLOCK.format(topic=topic.strip())
            if topic and topic.strip() else MODULE_NO_TOPIC_BLOCK
        ),
        lesson_count=lesson_count,
        minutes_per_lesson=minutes_per_lesson,
        target_words=f"{target_words:,}",
        gap_policy=_policy_sentence(gap_policy),
        language_directive=language_directive(language, source),
        style_directive=curriculum_style(language, source),
        answer_in=answer_in(language, source),
    )

    messages.append({"role": "user", "content": content})
    return messages


def generate_module_json(db, *, course: Block, library: LibraryContext,
                         topic: str | None) -> dict:
    """ONE guided_json over the whole library -> one module dict, tier clamped
    to the course's own gap policy. Raises whatever the provider raises — the
    job runner owns turning that into a failed-job message."""
    meta = course.meta or {}
    shape = meta.get("shape") or {}
    gap_policy = meta.get("gap_policy") or POLICY_GENERAL
    modules = _existing_modules(db, course)

    messages = build_module_messages(
        course_title=course.title,
        brief=meta.get("brief"),
        language=course.language,
        existing=_module_lines(db, modules),
        topic=topic,
        lesson_count=_typical_lesson_count(course, modules, db),
        minutes_per_lesson=shape.get("minutes_per_lesson", 50),
        target_words=shape.get("target_words_per_lesson", 2200),
        library=library,
        gap_policy=gap_policy,
        source=db,
    )
    module = get_provider().guided_json(messages, MODULE_SCHEMA, role="plan")

    requested = module.get("tier")
    module["tier"] = clamp_tier(requested, gap_policy)
    if module["tier"] != requested:
        module["tier_requested"] = requested
    return module


def materialize_module(db, course: Block, module: dict) -> Block:
    """Persist the generated module under the course, its lessons `queued` —
    exactly the shape `outline.materialize_outline` gives a module, so the draft
    fan-out, the board, and the progress GROUP BY need no new cases. Appends
    after the last module. Commits.
    """
    if course.kind != "course":
        raise ExtendError(f"block {course.id} is a {course.kind!r}, not a course")

    shape = (course.meta or {}).get("shape") or {}
    siblings = _existing_modules(db, course)

    tier = module.get("tier") or "general_knowledge"
    is_gap = tier == TIER_GAP
    module_block = Block(
        kind="module",
        title=module.get("title") or "New module",
        body=gap_body(course.language) if is_gap else (module.get("objective") or None),
        order=len(siblings),
        parent_id=course.id,
        language=course.language,
        meta={
            "tier": tier,
            "tier_requested": module.get("tier_requested"),
            "coverage_note": module.get("coverage_note") or "",
            "objective": module.get("objective") or "",
            "added_by": "ai",
        },
    )
    db.add(module_block)
    db.flush()

    if not is_gap:
        for l_i, lesson in enumerate(module.get("lessons") or []):
            db.add(Block(
                kind="lesson",
                title=lesson.get("title") or "New lesson",
                body=lesson.get("objective") or None,
                est_minutes=_est_minutes(
                    lesson.get("est_minutes"), shape.get("minutes_per_lesson", 50)
                ),
                order=l_i,
                parent_id=module_block.id,
                language=course.language,
                meta={
                    "draft_status": "queued",
                    "objective": lesson.get("objective") or "",
                },
            ))

    db.commit()
    db.refresh(module_block)
    return module_block


def generate_module(db, root_id: uuid.UUID, *, topic: str | None = None) -> Block:
    """The whole operation: plan one module from the library, persist it queued.
    The caller (the job runner) chains the draft fan-out afterwards."""
    course = db.get(Block, root_id)
    if course is None:
        raise ExtendError(f"curriculum not found: {root_id}")
    if course.kind != "course":
        raise ExtendError(f"block {root_id} is a {course.kind!r}, not a course")

    from app.curriculum.corpus import build_curriculum_context

    meta = course.meta or {}
    # Same None-vs-[] rule as the draft fan-out: [] is the tutor's explicit
    # "none of my sources" and must NOT widen to the whole library.
    raw_sources = meta.get("source_ids")
    source_ids = None if raw_sources is None else [uuid.UUID(s) for s in raw_sources]
    # SAME ROUTING as the course's own draft used — so an add-module call reads
    # the same representation (library or canon) and hits the same warm prefix.
    library = build_curriculum_context(db, source_ids)

    module_json = generate_module_json(db, course=course, library=library, topic=topic)
    return materialize_module(db, course, module_json)


# ---------------------------------------------------------------------------
# ONE LESSON, from the tutor's own brief
# ---------------------------------------------------------------------------
#
# «Προσθήκη μαθήματος» used to create a box titled «Νέο μάθημα» and hand the
# whole job of making it fit back to the tutor — the same mistake add-module
# made, one level down. Now he types what he wants the lesson to be, and ONE
# planning call (the same cached library prefix) turns that into a title and an
# objective that sit correctly among the module's existing lessons.
#
# The brief is NOT spent by the plan. It is stored on `meta.brief`, and
# `draft.LESSON_TUTOR_BRIEF_BLOCK` renders it whole into the drafting call — so
# the words he chose reach the model that actually writes the lesson, not just
# the one that named it.

LESSON_PLAN_SCHEMA_ONE: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "the lesson's title, in the course language"},
        "objective": {"type": "string",
                      "description": "one or two sentences: what the student can do after it"},
        "est_minutes": {"type": "integer"},
    },
    "required": ["title", "objective", "est_minutes"],
    "additionalProperties": False,
}

LESSON_PLAN_TAIL = (
    "YOUR TASK: design ONE new lesson for an existing module — a title and a "
    "one-to-two-sentence objective (it is drafted in full later, from the "
    "tutor's brief below). It must fit the module's arc and must NOT repeat any "
    "lesson the module already has.\n"
    "\nCOURSE: {course_title}{course_brief_block}\n"
    "\nTHE COURSE'S MODULES:\n{course_map}\n"
    "\nTHE MODULE THIS LESSON JOINS: {module_title} — {module_objective}\n"
    "ITS LESSONS, IN ORDER:\n{siblings}\n"
    "\nΤΟ ΜΑΘΗΜΑ ΠΟΥ ΖΗΤΗΣΕ Ο ΚΑΘΗΓΗΤΗΣ, με τα δικά του λόγια:\n{brief}\n"
    "{title_block}"
    "\nSHAPE: {minutes_per_lesson} minutes, later drafted to ~{target_words} words.\n"
    "\n{language_directive}\n"
    "\n{style_directive}\n"
    "\n{answer_in}"
)
LESSON_PLAN_SLICE_ID = "curriculum.extend.lesson"
LESSON_PLAN_TITLE_BLOCK = "\nTHE TUTOR ALREADY CHOSE THE TITLE — keep it exactly: {title}\n"

LESSON_NO_SIBLINGS = "(no lessons yet)"


def _lesson_lines(db, module: Block) -> str:
    """The module's lessons in teaching order, each with its objective — what the
    new lesson must fit between and must not repeat."""
    lessons = db.scalars(
        select(Block)
        .where(Block.parent_id == module.id, Block.kind == "lesson")
        .order_by(Block.order)
    ).all()
    lines = [
        f"{i}. {lesson.title} — {(lesson.meta or {}).get('objective') or ''}"
        for i, lesson in enumerate(lessons, start=1)
    ]
    return "\n".join(lines) or LESSON_NO_SIBLINGS


def build_lesson_plan_messages(
    *,
    course_title: str,
    brief_course: str | None,
    language: str,
    course_map: str,
    module_title: str,
    module_objective: str,
    siblings: str,
    brief: str,
    title: str | None,
    minutes_per_lesson: int,
    target_words: int,
    library: LibraryContext,
    source=None,
) -> list[dict]:
    """The messages for the add-lesson planning call. Pure, and riding the SAME
    `prefix_messages` prefix as the outline, the drafts and add-module — so the
    chained draft that follows reads a cache entry this call just warmed."""
    messages = prefix_messages(library, source)

    content = resolve(source, LESSON_PLAN_SLICE_ID, LESSON_PLAN_TAIL).format(
        course_title=course_title,
        course_brief_block=(
            MODULE_COURSE_BRIEF_BLOCK.format(brief=brief_course) if brief_course else ""
        ),
        course_map=course_map,
        module_title=module_title,
        module_objective=module_objective,
        siblings=siblings,
        brief=brief,
        title_block=(
            LESSON_PLAN_TITLE_BLOCK.format(title=title.strip())
            if title and title.strip() else ""
        ),
        minutes_per_lesson=minutes_per_lesson,
        target_words=f"{target_words:,}",
        language_directive=language_directive(language, source),
        style_directive=curriculum_style(language, source),
        answer_in=answer_in(language, source),
    )

    messages.append({"role": "user", "content": content})
    return messages


def plan_lesson_json(db, *, course: Block, module: Block, library: LibraryContext,
                     brief: str, title: str | None) -> dict:
    """ONE guided_json over the whole library -> `{title, objective, est_minutes}`.

    A title the tutor typed WINS over the model's, and the prompt says so rather
    than overruling him silently — a model told to keep his title writes the
    objective FOR that title, where one that named its own would write the
    objective for a lesson he is not getting.
    """
    meta = course.meta or {}
    shape = meta.get("shape") or {}

    messages = build_lesson_plan_messages(
        course_title=course.title,
        brief_course=meta.get("brief"),
        language=course.language,
        course_map=_module_lines(db, _existing_modules(db, course)),
        module_title=module.title,
        module_objective=(module.meta or {}).get("objective") or module.body or "",
        siblings=_lesson_lines(db, module),
        brief=brief.strip(),
        title=title,
        minutes_per_lesson=shape.get("minutes_per_lesson", 50),
        target_words=shape.get("target_words_per_lesson", 2200),
        library=library,
        source=db,
    )
    planned = get_provider().guided_json(messages, LESSON_PLAN_SCHEMA_ONE, role="plan")

    if title and title.strip():
        planned["title"] = title.strip()
    return planned


def generate_lesson(db, module_id: uuid.UUID, *, brief: str, title: str | None = None,
                    after: uuid.UUID | None = None) -> Block:
    """Plan one lesson from the tutor's brief and persist it `queued`, in place.

    `edit._add_lesson` is the ONE insertion path — the same renormalised sibling
    order, the same `added_by_tutor` marking, the same minutes from the course
    shape — so a lesson born from a brief is indistinguishable downstream from
    one he typed a title for. The caller (the job) chains the draft.
    """
    module = db.get(Block, module_id)
    if module is None:
        raise ExtendError(f"module not found: {module_id}")
    if module.kind != "module":
        raise ExtendError(f"block {module_id} is a {module.kind!r}, not a module")
    course = db.get(Block, module.parent_id) if module.parent_id else None
    if course is None or course.kind != "course":
        raise ExtendError(f"module {module_id} does not belong to a course")

    from app.curriculum.corpus import build_curriculum_context

    meta = course.meta or {}
    # Same None-vs-[] rule as everywhere else: [] is the tutor's explicit "none
    # of my sources" and must NOT widen to the whole library.
    raw_sources = meta.get("source_ids")
    source_ids = None if raw_sources is None else [uuid.UUID(s) for s in raw_sources]
    library = build_curriculum_context(db, source_ids)

    planned = plan_lesson_json(db, course=course, module=module, library=library,
                               brief=brief, title=title)

    shape = meta.get("shape") or {}
    try:
        lesson = _add_lesson(
            db, module.id,
            title=(title or planned.get("title") or "").strip() or "Νέο μάθημα",
            objective=planned.get("objective") or "",
            after=after,
        )
    except EditError as e:
        # A structural refusal (`after` is not a lesson of this module) is the
        # job's failure to report, not a 500 — same contract as `generate_module`.
        raise ExtendError(str(e)) from e

    lesson.est_minutes = _est_minutes(planned.get("est_minutes"),
                                      shape.get("minutes_per_lesson", 50))
    # WHOLE-DICT REASSIGNMENT — a mutated JSONB dict is not seen by SQLAlchemy.
    lesson.meta = {**(lesson.meta or {}), "brief": brief.strip()}
    db.commit()
    db.refresh(lesson)
    return lesson
