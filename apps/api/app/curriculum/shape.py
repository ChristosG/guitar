"""The curriculum's SHAPE: how many modules, how many lessons, how long each.

Chris, looking at the deployed app: "im making a curriculum with 20 weeks, and
only 4 modules are here. are those enough? i dont want us to be frugal here."

He was looking at a 20-week course that the model had decided to express as four
modules — because the only thing that ever constrained the count was a sentence
in a prompt ("about 2-6 modules"), and a sentence in a prompt is a suggestion.
The old `PLAN_SCHEMA` had no `minItems`; even if it had, Claude's structured
outputs do not enforce array constraints (`llm/schema.py` strips them). So the
shape is computed HERE, in Python, from what the tutor actually told us — weeks,
sessions per week, minutes per session — and `enforce_shape` makes the model's
output match it afterwards, by construction rather than by request.

    plan_shape(20, 1, 50)  ->  20 lessons / 5 modules / [4, 4, 4, 4, 4]

"20 weeks, 4 modules" is now unrepresentable.

THE 40+10 SPLIT IS HIS, NOT OURS. Chris: "each lesson has to be around 40 mins,
then 10 mins for questions/discussion, and those 40 mins has to be around 4-5
pages." So a 50-minute session is 40 minutes of taught material plus a 10-minute
Q&A block — which is why `teaching_minutes`, not `minutes_per_lesson`, is what
`app.curriculum.depth` turns into a word target. Counting the Q&A time as prose
would inflate every lesson by 25% and then "fail" it for being thin.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.curriculum.depth import floor_words, target_words

# The Q&A / discussion block at the end of every session. Chris's number, and a
# FIXED span rather than a fraction: a 30-minute lesson and a 60-minute lesson
# both end with roughly ten minutes of "any questions?", because that is a
# property of a room full of students, not of the syllabus.
QA_MINUTES = 10

# Below this, a session is all Q&A and no lesson. Rather than emit a
# zero-teaching-minute shape (which would give a word target of 0 and make every
# lesson trivially "deep enough"), we keep a floor of real teaching time.
MIN_TEACHING_MINUTES = 10

# The lessons-per-module the module count is derived FROM. Four ~40-minute
# lessons is a month of weekly teaching and a coherent unit of subject matter —
# it is the granularity the tutor already thinks in. Everything else here is
# arithmetic around this one editorial choice.
LESSONS_PER_MODULE = 4


@dataclass(frozen=True)
class Shape:
    """The enforced counts for one curriculum. Every field is derived; nothing
    here is a suggestion to a model.

    `lessons_per_module` sums to `lessons_total` BY CONSTRUCTION — that identity
    is what `enforce_shape` relies on, and it is asserted in `plan_shape`.
    """

    lessons_total: int
    modules: int
    lessons_per_module: tuple[int, ...]
    minutes_per_lesson: int
    teaching_minutes: int
    qa_minutes: int
    target_words_per_lesson: int
    floor_words_per_lesson: int

    def describe(self) -> str:
        """The one-line echo the interview's duration step shows the tutor —
        *"20 sessions -> 5 modules x 4 lessons -> ~2,200 words each"*. He is
        agreeing to a size before we spend his money on it.
        """
        per = "+".join(str(n) for n in self.lessons_per_module)
        return (
            f"{self.lessons_total} sessions -> {self.modules} modules "
            f"({per} lessons) -> ~{self.target_words_per_lesson:,} words each "
            f"({self.teaching_minutes} min taught + {self.qa_minutes} min Q&A)"
        )


def plan_shape(weeks: int, sessions_per_week: int = 1, minutes: int = 50) -> Shape:
    """The enforced shape for a course of `weeks` x `sessions_per_week` sessions
    of `minutes` each.

    Raises `ValueError` on a non-positive input rather than silently clamping: a
    zero-week curriculum is a bug in the caller, and the interview validates
    these fields before we ever get here.
    """
    if weeks <= 0 or sessions_per_week <= 0 or minutes <= 0:
        raise ValueError(
            f"plan_shape needs positive weeks/sessions_per_week/minutes, got "
            f"{weeks!r}/{sessions_per_week!r}/{minutes!r}"
        )

    lessons_total = weeks * sessions_per_week

    # Round HALF UP to a whole number of 4-lesson modules, never below 1.
    # 20 -> 5. 8 -> 2. 6 -> 2. 10 -> 3. 3 -> 1.
    #
    # NOT `round()`, which is banker's rounding: `round(2.5)` is 2 but `round(3.5)`
    # is 4. So a 10-lesson course would get 2 modules and a 14-lesson course 4 — the
    # same .5 remainder resolved in opposite directions, for reasons no tutor will
    # ever be able to see. And it breaks the tie DOWNWARD, which is the one
    # direction Chris explicitly ruled out: "i dont want us to be frugal here."
    modules = max(1, int(lessons_total / LESSONS_PER_MODULE + 0.5))
    modules = min(modules, lessons_total)

    base, remainder = divmod(lessons_total, modules)
    # Front-load the remainder: a course that runs long is long at the START,
    # while the student is fresh, not in a bloated final module.
    lessons_per_module = tuple(
        base + (1 if i < remainder else 0) for i in range(modules)
    )
    assert sum(lessons_per_module) == lessons_total  # the identity enforce_shape trusts

    teaching_minutes = max(MIN_TEACHING_MINUTES, minutes - QA_MINUTES)

    return Shape(
        lessons_total=lessons_total,
        modules=modules,
        lessons_per_module=lessons_per_module,
        minutes_per_lesson=minutes,
        teaching_minutes=teaching_minutes,
        qa_minutes=min(QA_MINUTES, minutes),
        target_words_per_lesson=target_words(teaching_minutes),
        floor_words_per_lesson=floor_words(teaching_minutes),
    )


def enforce_shape(outline: dict, shape: Shape) -> dict:
    """Make `outline` (an `OUTLINE_SCHEMA` payload straight off the model) have
    EXACTLY `shape`'s module and lesson counts. Returns a new dict.

    THIS FUNCTION IS THE ENFORCEMENT. Not the schema — Claude's structured
    outputs do not support `minItems`/`maxItems`, and `llm/schema.py` strips them
    before the call precisely so the SDK cannot raise on them after we have paid
    for the tokens. The prompt states the exact counts, this makes them true, and
    between the two the tutor cannot end up with 4 modules for 20 weeks again.

    Under-supply is padded with honestly-titled placeholders rather than being
    rejected: a 19-lesson answer to a 20-lesson request is a good outline with a
    hole in it, and a hole the tutor can rename in the editor is worth far more
    than a failed job. Over-supply is truncated from the end.

    Raises `GuidedJSONError` when there are NO modules at all — the guard nobody
    had. An empty `modules` list used to sail straight through into
    `materialize_outline` and persist a course Block with no children: a
    curriculum that exists, is listed, and is empty.
    """
    from app.llm.errors import GuidedJSONError  # local: keeps this module import-light

    modules = list(outline.get("modules") or [])
    if not modules:
        raise GuidedJSONError(
            "the outline came back with no modules at all — refusing to persist "
            "an empty curriculum"
        )

    modules = modules[: shape.modules]
    while len(modules) < shape.modules:
        n = len(modules) + 1
        modules.append(
            {
                "title": f"Module {n}",
                "objective": "",
                "tier": "general_knowledge",
                "coverage_note": "This module was added to complete the requested "
                                 "course length — rename it and set its objective.",
                "lessons": [],
            }
        )

    out_modules = []
    for module, wanted in zip(modules, shape.lessons_per_module):
        lessons = list(module.get("lessons") or [])[:wanted]
        while len(lessons) < wanted:
            lessons.append(
                {
                    "title": f"Lesson {len(lessons) + 1}",
                    "objective": "",
                    "est_minutes": shape.minutes_per_lesson,
                }
            )
        for lesson in lessons:
            # est_minutes is the SESSION length the tutor booked, not a number
            # for the model to reconsider — the whole shape is derived from it.
            lesson["est_minutes"] = shape.minutes_per_lesson
        out_modules.append({**module, "lessons": lessons})

    return {**outline, "modules": out_modules}
