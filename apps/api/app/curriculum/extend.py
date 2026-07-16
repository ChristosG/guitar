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
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.curriculum.corpus import LibraryContext, prefix_messages
from app.curriculum.outline import (
    POLICY_GENERAL,
    TIER_GAP,
    TIER_ORDER,
    _est_minutes,
    _policy_sentence,
    clamp_tier,
    gap_body,
)
from app.i18n import answer_in, language_directive
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
) -> list[dict]:
    """The messages for the add-module call. Pure — same testability contract as
    `outline.build_outline_messages`: the prefix is the shared cached one, and
    every request-specific fact sits strictly after it."""
    messages = prefix_messages(library)

    tail = [
        "YOUR TASK: design ONE new module to EXTEND an existing course — a title, "
        "a one-sentence objective, and its lessons (titles and one-sentence "
        "objectives only; the lessons are drafted in full later). It must fit the "
        "course's arc: build on what earlier modules teach, and do NOT repeat any "
        "module or lesson the course already has.",
        f"\nCOURSE: {course_title}",
    ]
    if brief:
        tail.append(f"\nWHAT THE TUTOR WANTS FROM THIS COURSE, IN HIS OWN WORDS:\n{brief}")
    tail.append(f"\nTHE COURSE AS IT STANDS (in teaching order):\n{existing}")
    tail.append(
        f"\nTHE NEW MODULE'S TOPIC: {topic.strip()}" if topic and topic.strip()
        else (
            "\nTHE TUTOR DID NOT NAME A TOPIC. Choose the most valuable module "
            "this course is missing — the thing a student who finished the "
            "existing modules would most need next."
        )
    )
    tail.append(
        f"\nSHAPE: give it EXACTLY {lesson_count} lessons of {minutes_per_lesson} "
        f"minutes each; each will later be drafted to ~{target_words:,} words."
    )
    tail.append(
        "\nTIER THE MODULE HONESTLY. You have read his entire library; you are "
        "the only one who can say whether it actually covers this topic. A module "
        "tiered 'library' will be drafted from his pages and cited to them — if "
        "it is not really in there, that citation is a lie the tutor will click "
        "on. Say 'general_knowledge' instead. That is not a failure; an "
        "unlabelled gap is."
    )
    tail.append(f"\nGAP POLICY: {_policy_sentence(gap_policy)}")
    tail.append(f"\n{language_directive(language)}")
    tail.append(f"\n{answer_in(language)}")

    messages.append({"role": "user", "content": "\n".join(tail)})
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

    from app.curriculum.corpus import build_library_context

    meta = course.meta or {}
    # Same None-vs-[] rule as the draft fan-out: [] is the tutor's explicit
    # "none of my sources" and must NOT widen to the whole library.
    raw_sources = meta.get("source_ids")
    source_ids = None if raw_sources is None else [uuid.UUID(s) for s in raw_sources]
    library = build_library_context(db, source_ids)

    module_json = generate_module_json(db, course=course, library=library, topic=topic)
    return materialize_module(db, course, module_json)
