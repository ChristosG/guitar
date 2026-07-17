"""Curriculum authoring: outline the course from the WHOLE library, persist the
tree, and let the lesson fan-out fill it in.

WHAT THIS MODULE USED TO BE, AND WHY NONE OF IT SURVIVED.

It was a two-phase generator: plan the modules blind (one guided-JSON call that
had never seen the library), then, per module, run a retrieval query and draft
that module's lessons from whatever came back. `PLAN_SCHEMA`, `MODULE_SCHEMA`,
`_build_plan_messages`, `_build_module_draft_messages` — all deleted. Four things
were wrong with it, and the tutor felt every one:

1. THE PLANNER HAD NOT READ THE BOOK. It proposed modules from general knowledge,
   and only afterwards did anyone check whether his library had anything to say
   about them. A course outlined by a model that has read his 90K-token library is
   a different object.

2. "GAP" WAS A COSINE FLOOR. A module was declared uncovered when retrieval
   returned nothing above a threshold — on a corpus where the measured margin
   between the lowest COVERED topic (0.881) and the highest UNCOVERED one (0.860)
   is 0.021. That is a coin flip with a decimal point, deciding, unattended,
   whether twenty lessons come from his book or from the model's memory. Now the
   model reads the book and SAYS whether it is in there.

3. ONE CALL PER MODULE COULD NOT PRODUCE A REAL LESSON. 4-5 lessons x 2,200 words,
   in Greek, is 30-40k output tokens in one response — through `max_tokens`, and
   truncated JSON surfaces as a PARSE error, so it gets debugged in the wrong file.
   The fan-out unit is now the LESSON (`app.curriculum.draft`).

4. IT WAS SYNCHRONOUS AND ALL-OR-NOTHING. The tutor watched a spinner for four
   minutes and then either got everything or got nothing. Now the tree is
   materialized at CONFIRM — the board opens instantly on 20 queued lessons — and
   they arrive one at a time.

WHAT THIS MODULE IS NOW: the entry point that turns a curriculum REQUEST into a
persisted, queued tree. It is called by `jobs/runner.run_curriculum_job` (the
`POST /curricula/generate` path and the chat agent's `generate_curriculum` tool);
the interview drives `outline.py` directly, because it needs to show the tutor the
outline and let him edit it before any of this happens.
"""
from __future__ import annotations

import uuid

from app.curriculum.corpus import build_curriculum_context
from app.curriculum.outline import POLICY_GENERAL, POLICY_LIBRARY_ONLY, generate_outline, materialize_outline
from app.curriculum.shape import Shape, plan_shape
from app.i18n import DEFAULT_LOCALE
from app.students.context import build_student_brief

# The session length assumed when a caller gives us a total course length and
# nothing else (the legacy `target_minutes_total` shape, still used by the chat
# agent's tool). 50 minutes is the tutor's own default and the one his 40+10 split
# is quoted against.
DEFAULT_SESSION_MINUTES = 50


def shape_from_request(
    *,
    weeks: int | None = None,
    sessions_per_week: int = 1,
    minutes_per_session: int | None = None,
    target_minutes_total: int | None = None,
) -> Shape:
    """The requested shape, from whichever fields the caller actually has.

    `weeks` + `minutes_per_session` is the real answer, and it is what the
    interview collects. `target_minutes_total` is the legacy shape (one number,
    from a form and from the chat tool's schema) and it is derived from rather than
    honoured exactly: "600 minutes" says nothing about whether that is twelve
    50-minute lessons or six 100-minute ones, and a 100-minute guitar lesson is not
    a thing. So it becomes as many 50-minute sessions as it holds.
    """
    minutes = minutes_per_session or DEFAULT_SESSION_MINUTES
    if weeks:
        return plan_shape(weeks, sessions_per_week, minutes)
    if target_minutes_total:
        return plan_shape(max(1, round(target_minutes_total / minutes)), 1, minutes)
    # Neither given: a course of one term, weekly. Better than raising at the tutor.
    return plan_shape(12, 1, minutes)


def generate_curriculum(
    db,
    *,
    title: str,
    language: str = DEFAULT_LOCALE,
    profile: dict | None = None,
    brief: str | None = None,
    gap_policy: str | None = None,
    allow_general: bool = True,
    weeks: int | None = None,
    sessions_per_week: int = 1,
    minutes_per_session: int | None = None,
    target_minutes_total: int | None = None,
    source_ids: list[uuid.UUID] | None = None,
    student_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Outline the course over the WHOLE selected library and persist the tree with
    every lesson `queued`. Returns the course (root) Block id. Commits.

    THIS FUNCTION DOES NOT DRAFT ANY LESSONS. It runs exactly ONE model call — the
    outline — which is also the call that WARMS THE PROMPT CACHE for the fan-out
    that follows (`jobs/curriculum_draft.py`). That ordering is not incidental: a
    fan-out started before the cache exists has every worker writing the same 90K
    tokens in parallel at 1.25x, and the run costs ~$7 instead of ~$2.72.

    `student_id` reaches the OUTLINE prompt *and*, via `Block.meta`, every lesson
    draft — which is the fix for the thing that made personalization a lie: until
    now `level` and `name` appeared in the outline prompt and NOWHERE ELSE, so
    every lesson body ever generated by this app was written with zero knowledge of
    the learner.

    `allow_general` is kept for the callers that still speak it (the chat tool, the
    old form) and is translated into the `gap_policy` vocabulary rather than being
    a second, parallel switch. `allow_general=False` means "show me the gaps" —
    which is exactly `library_only`.
    """
    profile = profile or {}
    # None-vs-[] preserved (see outline.materialize_outline): [] is a
    # deliberate "draw on none of my sources", not a synonym for "everything".
    if source_ids is not None:
        source_ids = [
            s if isinstance(s, uuid.UUID) else uuid.UUID(str(s)) for s in source_ids
        ]
    if student_id is not None and not isinstance(student_id, uuid.UUID):
        student_id = uuid.UUID(str(student_id))

    policy = gap_policy or (POLICY_GENERAL if allow_general else POLICY_LIBRARY_ONLY)
    shape = shape_from_request(
        weeks=weeks or profile.get("weeks"),
        sessions_per_week=sessions_per_week or profile.get("sessions_per_week") or 1,
        minutes_per_session=minutes_per_session or profile.get("minutes_per_session"),
        target_minutes_total=target_minutes_total,
    )

    # ROUTES the whole selection: full-context verbatim at/below the canon
    # threshold, the compiled canon above it. Below threshold this is a pure
    # passthrough to `build_library_context`, so his ~90K library is untouched.
    library = build_curriculum_context(db, source_ids)
    student_brief = build_student_brief(db, student_id)

    outline = generate_outline(
        db,
        title=title,
        brief=brief,
        language=language,
        shape=shape,
        library=library,
        student_brief=student_brief,
        gap_policy=policy,
    )

    return materialize_outline(
        db,
        outline,
        title=title,
        language=language,
        shape=shape,
        library=library,
        brief=brief,
        gap_policy=policy,
        student_id=student_id,
        source_ids=source_ids,
        profile=profile,
    )
