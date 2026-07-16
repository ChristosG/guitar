"""The OUTLINE: one call, over the whole library, that produces the course the
tutor edits before we spend his money drafting it.

WHAT CHANGED, AND WHY IT IS ONE CALL AND NOT N+1.

The old generator planned an outline blind (no library at all), then retrieved
per-module and drafted per-module. Two consequences, both of which the tutor felt:
the plan could propose modules his book had nothing to say about (it had not read
it), and the "is this a gap?" decision was a COSINE FLOOR — a number with a 0.021
separation margin on this corpus, i.e. a coin flip with a decimal point.

Now the model reads the entire library (`corpus.py`) and writes the outline from
it. The tier — 📚 library / 🧠 general knowledge / 🌐 web — is assigned PER MODULE
BY THE MODEL, HAVING READ THE BOOK, and a "gap" is the model saying "this isn't in
here" rather than a similarity score failing to clear a constant. That is not a
better heuristic; it is a different KIND of answer.

THIS CALL WARMS THE CACHE. It writes the 90K-token library block at 1.25x
($0.34); the 20 lesson drafts that follow each read it at 0.1x ($0.027). So it
must run FIRST and ALONE — a fan-out started before the cache exists has every
worker paying the write price in parallel, and the whole cost model of Stage 6
(~$2.72 for a 45,000-word Greek curriculum) turns into ~$7.

MATERIALIZATION HAPPENS AT CONFIRM, NOT AT THE END OF DRAFTING. The course ->
module -> lesson Blocks are persisted the moment the tutor approves the outline,
every lesson carrying `meta.draft_status = "queued"`. Two things fall out of that
for free: the board OPENS INSTANTLY on a tree that already exists, and progress
becomes a GROUP BY over the lessons rather than a counter on a job row that only
one process can update.
"""
from __future__ import annotations

import logging
import uuid

from app.curriculum.corpus import LibraryContext, prefix_messages
from app.curriculum.shape import Shape, enforce_shape
from app.i18n import answer_in, language_directive
from app.llm.factory import get_provider
from app.models.block import Block

log = logging.getLogger(__name__)

# 📚 -> 🧠 -> 🌐, in order of how far a module strays from the tutor's own
# material. `gap_policy` is a CEILING on this scale, not a suggestion.
TIER_LIBRARY = "library"
TIER_GENERAL = "general_knowledge"
TIER_WEB = "web"
TIER_GAP = "gap"
TIER_ORDER = (TIER_LIBRARY, TIER_GENERAL, TIER_WEB)

# What the tutor may allow, at the "scope" step.
POLICY_LIBRARY_ONLY = "library_only"
POLICY_GENERAL = "general_knowledge"
POLICY_WEB = "web"
GAP_POLICIES = (POLICY_LIBRARY_ONLY, POLICY_GENERAL, POLICY_WEB)

_POLICY_CEILING = {
    POLICY_LIBRARY_ONLY: 0,   # only 📚. Anything else is an honest, unfilled GAP.
    POLICY_GENERAL: 1,        # 📚 or 🧠.
    POLICY_WEB: 2,            # 📚, 🧠 or 🌐.
}

OUTLINE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "modules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "tier": {
                        "type": "string",
                        "enum": list(TIER_ORDER),
                        "description": (
                            "You have just read the tutor's entire library. Say "
                            "honestly where this module's material comes from: "
                            "'library' if his own sources genuinely teach it, "
                            "'general_knowledge' if they do not and you would be "
                            "writing it from what you know, 'web' if it needs "
                            "current information neither of you has. Do NOT say "
                            "'library' because a page mentions the word."
                        ),
                    },
                    "coverage_note": {
                        "type": "string",
                        "description": (
                            "One sentence, for the tutor: what in his library "
                            "covers this (name the source and pages), or what is "
                            "missing from it."
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
            },
        },
    },
    "required": ["title", "modules"],
    "additionalProperties": False,
}


def build_outline_messages(
    *,
    title: str,
    brief: str | None,
    language: str,
    shape: Shape,
    library: LibraryContext,
    student_brief: str | None,
    gap_policy: str,
) -> list[dict]:
    """The messages for the outline call. Pure — no model, no DB — so the ONE
    property that costs real money if it is wrong (the library block is the
    stable prefix, and every volatile field sits AFTER it) is unit-testable.

    Read the order below as a cache diagram: system, then the cached library, then
    everything that changes between calls. The lesson drafts build the identical
    prefix and diverge only after the breakpoint, which is why they read the cache
    at 0.1x instead of re-writing it at 1.25x.
    """
    counts = ", ".join(
        f"module {i + 1}: {n} lessons" for i, n in enumerate(shape.lessons_per_module)
    )

    # THE STABLE PREFIX (`corpus.prefix_messages`). Nothing above the tail varies
    # between the outline call, the lesson drafts, and an add-module call — the
    # shape counts and the language used to live in a bespoke system message here,
    # which made this call's 90K-token cache write unreadable by every draft that
    # followed it (a different system is a different cache key).
    messages = prefix_messages(library)

    # --- everything from here down is VOLATILE and must stay outside the cache ---
    tail = [
        "YOUR TASK: design the outline of a course — titles and one-sentence "
        "objectives only. You are NOT writing lesson content here.",
        f"\nTHE COUNTS ARE NOT NEGOTIABLE AND THEY ARE NOT SUGGESTIONS. Produce "
        f"EXACTLY {shape.modules} modules and EXACTLY {shape.lessons_total} lessons "
        f"in total, distributed as: {counts}. Every lesson is "
        f"{shape.minutes_per_lesson} minutes.",
        "\nTIER EVERY MODULE HONESTLY. You have read his entire library; you are "
        "the only one who can say whether it actually covers a topic. A module "
        "tiered 'library' will be drafted from his pages and cited to them — if it "
        "is not really in there, that citation is a lie the tutor will click on. "
        "Say 'general_knowledge' instead. That is not a failure; an unlabelled "
        "gap is.",
        f"\n{language_directive(language)}",
        f"\nCOURSE TITLE: {title}",
    ]
    if brief:
        tail.append(f"\nWHAT THE TUTOR WANTS FROM THIS COURSE, IN HIS OWN WORDS:\n{brief}")
    if student_brief:
        tail.append(f"\n{student_brief}")
    tail.append(
        f"\nSHAPE: {shape.lessons_total} lessons across {shape.modules} modules "
        f"({counts}). Each lesson is {shape.minutes_per_lesson} minutes "
        f"({shape.teaching_minutes} taught + {shape.qa_minutes} of Q&A) and will "
        f"later be drafted to ~{shape.target_words_per_lesson:,} words."
    )
    tail.append(f"\nGAP POLICY: {_policy_sentence(gap_policy)}")
    tail.append(f"\n{answer_in(language)}")

    messages.append({"role": "user", "content": "\n".join(tail)})
    return messages


def _policy_sentence(gap_policy: str) -> str:
    if gap_policy == POLICY_LIBRARY_ONLY:
        return (
            "the tutor wants this course drawn ONLY from his own library. A module "
            "his sources do not cover will be left deliberately empty and shown to "
            "him as a gap — so tier it honestly rather than reaching."
        )
    if gap_policy == POLICY_WEB:
        return (
            "the tutor allows modules his library does not cover to be filled from "
            "general knowledge, or from the web where the topic needs current "
            "information. Both are labelled as such in the finished curriculum."
        )
    return (
        "the tutor allows modules his library does not cover to be filled from "
        "your general knowledge — clearly labelled as not coming from his material."
    )


def clamp_tier(tier: str | None, gap_policy: str) -> str:
    """The model's requested tier, clamped to what the tutor allowed.

    A module the model tiers `general_knowledge` under a `library_only` policy does
    NOT become `library` — it becomes `gap`. That distinction is the whole of G3:
    "your library doesn't cover this" is a true and useful thing to tell a tutor,
    and "here is some content, from somewhere, unlabelled" is the bug he reported.
    """
    ceiling = _POLICY_CEILING.get(gap_policy, 1)
    try:
        want = TIER_ORDER.index(tier)
    except ValueError:
        # A missing or unrecognised tier is NOT a library tier. Defaulting to
        # `library` here would be the single most damaging line in this file: a
        # module the model never claimed to have found in the book would be drafted
        # as if it had been, and told to cite pages for it. An absent answer means
        # "I don't know where this came from", and the honest floor for that is
        # general knowledge.
        want = TIER_ORDER.index(TIER_GENERAL)
    if want <= ceiling:
        return TIER_ORDER[want]
    if ceiling == 0:
        return TIER_GAP
    return TIER_ORDER[ceiling]


def generate_outline(
    db,
    *,
    title: str,
    brief: str | None,
    language: str,
    shape: Shape,
    library: LibraryContext,
    student_brief: str | None,
    gap_policy: str,
) -> dict:
    """ONE guided_json over the whole library -> the outline, shape-enforced.

    `role="plan"` (thinking on, streamed, 16K out). The result has EXACTLY
    `shape`'s counts by the time it returns, because `enforce_shape` makes it so —
    Claude's structured outputs cannot enforce array lengths and `llm/schema.py`
    strips the keywords that pretend to (see its docstring).
    """
    messages = build_outline_messages(
        title=title, brief=brief, language=language, shape=shape,
        library=library, student_brief=student_brief, gap_policy=gap_policy,
    )
    raw = get_provider().guided_json(messages, OUTLINE_SCHEMA, role="plan")
    # raises GuidedJSONError when there are no modules
    outline = enforce_shape(raw, shape, language=language)

    for module in outline["modules"]:
        requested = module.get("tier")
        module["tier"] = clamp_tier(requested, gap_policy)
        if module["tier"] != requested:
            module["tier_requested"] = requested
    return outline


# The body a GAP module carries — in the COURSE's language (it lands on
# Block.body in front of a Greek tutor). Plain, unambiguous, and no invented
# content anywhere near it — the tutor asked for his library and his library
# does not have this. Same principle Plan 11 used for chat: general knowledge
# is not the danger, UNLABELLED general knowledge is.
_GAP_BODIES = {
    "el": (
        "Η βιβλιοθήκη σου δεν καλύπτει αυτό το θέμα, και ζήτησες το πρόγραμμα να "
        "βασιστεί μόνο στο δικό σου υλικό — οπότε δεν γράφτηκε περιεχόμενο. "
        "Πρόσθεσε μια πηγή για το θέμα, άλλαξε την προέλευση της ενότητας, ή "
        "διάγραψέ τη."
    ),
    "en": (
        "Your library doesn't cover this topic, and you asked for this course to "
        "be drawn only from your own material — so no lesson content was generated "
        "for it. Add a source on this topic, change this module's tier, or delete it."
    ),
}
# Kept for existing imports/tests; Greek default per i18n.py's rule.
GAP_BODY = _GAP_BODIES["el"]


def gap_body(language: str) -> str:
    return _GAP_BODIES.get(language, _GAP_BODIES["el"])


def _est_minutes(value, default: int) -> int:
    """An edited `est_minutes` off the wire — anything that is not a positive whole
    number of minutes falls back to the shape's. A `bool` is rejected explicitly:
    `True` is an `int` in Python, and a 1-minute lesson is not what he meant."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value) if value > 0 else default


def materialize_outline(
    db,
    outline: dict,
    *,
    title: str,
    language: str,
    shape: Shape,
    library: LibraryContext,
    brief: str | None = None,
    gap_policy: str = POLICY_GENERAL,
    student_id: uuid.UUID | None = None,
    source_ids: list[uuid.UUID] | None = None,
    profile: dict | None = None,
) -> uuid.UUID:
    """Persist course -> module -> lesson Blocks from an APPROVED outline, every
    lesson `queued`. Returns the course (root) Block id. Commits.

    The tree exists before a single lesson has been drafted. That is deliberate and
    it is the whole live-progress model: the board renders immediately, `GET
    /curricula/{root}/progress` is a GROUP BY over these rows, and a restart
    mid-draft loses nothing but the lessons that were in flight.

    EVERY `meta` WRITE HERE IS A WHOLE-DICT ASSIGNMENT. `Block.meta` is plain
    `sa.JSON` with no `MutableDict`, so `block.meta["k"] = v` is not persisted —
    it works in dev (the identity map returns the same dict) and silently does
    nothing in production.
    """
    course = Block(
        kind="course",
        title=outline.get("title") or title,
        order=0,
        language=language,
        is_template=True,
        # target_profile means ONE thing again: who this template is aimed at.
        # Provenance and grounding live on `meta`.
        target_profile=profile or {},
        meta={
            "brief": brief,
            "gap_policy": gap_policy,
            "student_id": str(student_id) if student_id else None,
            # None-vs-[] is preserved: None means "unscoped — the whole
            # library", [] means "the tutor deliberately picked none". The old
            # `or []` collapsed both to [], and the draft job's own coercion
            # then read the tutor's explicit "none" as "everything".
            "source_ids": None if source_ids is None else [str(s) for s in source_ids],
            "shape": {
                "lessons_total": shape.lessons_total,
                "modules": shape.modules,
                "lessons_per_module": list(shape.lessons_per_module),
                "minutes_per_lesson": shape.minutes_per_lesson,
                "teaching_minutes": shape.teaching_minutes,
                "target_words_per_lesson": shape.target_words_per_lesson,
                "floor_words_per_lesson": shape.floor_words_per_lesson,
            },
            "library": {
                "token_count": library.token_count,
                "fits": library.fits,
                "sources": library.sources,
                # THE HONEST BANNER. False here means the library was too large to
                # read whole and the lessons were drafted from per-module retrieval
                # instead. The tutor is told, on the board, in words. A silent
                # downgrade to retrieval is exactly the failure this whole stage
                # exists to remove.
                "full_context": library.fits and not library.is_empty,
            },
        },
    )
    db.add(course)
    db.flush()

    for m_i, module in enumerate(outline["modules"]):
        tier = module.get("tier") or TIER_GENERAL
        is_gap = tier == TIER_GAP
        module_block = Block(
            kind="module",
            title=module["title"],
            body=gap_body(language) if is_gap else (module.get("objective") or None),
            order=m_i,
            parent_id=course.id,
            language=language,
            meta={
                "tier": tier,
                "tier_requested": module.get("tier_requested"),
                "coverage_note": module.get("coverage_note") or "",
                "objective": module.get("objective") or "",
            },
        )
        db.add(module_block)
        db.flush()

        # A gap module gets NO lessons — not empty lessons, not placeholder
        # lessons. There is nothing to draft, no call is made, and the board shows
        # a module with an honest badge and no children.
        if is_gap:
            continue

        for l_i, lesson in enumerate(module.get("lessons") or []):
            db.add(Block(
                kind="lesson",
                title=lesson["title"],
                body=lesson.get("objective") or None,
                # THE OUTLINE THE TUTOR EDITED IS THE ONE THAT GETS BUILT — including
                # this. He can give one lesson 90 minutes in the editor, and the
                # draft job sizes its word target from these minutes
                # (`jobs/curriculum_draft._lesson_size`), so the number he typed is
                # not merely displayed back at him. `shape.minutes_per_lesson` is the
                # default, not an override.
                est_minutes=_est_minutes(lesson.get("est_minutes"), shape.minutes_per_lesson),
                order=l_i,
                parent_id=module_block.id,
                language=language,
                meta={
                    "draft_status": "queued",
                    "objective": lesson.get("objective") or "",
                },
            ))

    db.commit()
    return course.id
