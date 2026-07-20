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

50 MEANS 50. Chris, 2026-07-21, reversing the earlier hardcoded 40+10 split:
"forget the extra Q&A. if he has Q&A on his blueprint, then he does. otherwise
it doesnt. so 50 is 50. and if he has Q&A it just takes it from his weight set."
So the WHOLE session is teaching time and the word target is derived from all of
it; Q&A is not a fixed carve-out any more — it is an ordinary blueprint section
(`qa_prompts`, kind="qa") that takes its share of the minutes from its own
weight, exactly like theory or exercises, and disappears entirely when the tutor
disables it. `teaching_minutes` is kept on the wire (== `minutes_per_lesson`)
so `ShapeOut` consumers don't churn; `qa_minutes` is retired to 0.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.curriculum.depth import floor_words, target_words

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
        """A one-line English summary — FOR LOGS AND TESTS ONLY.

        NOT for the tutor's screen. It used to be: the interview rendered this
        string verbatim, so a Greek-speaking tutor on a fully Greek page was
        shown *"20 sessions -> 5 modules (4+4+4+4+4 lessons) -> ~2,200 words
        each"* in English. Caught by driving the real interview.

        The seam was simply in the wrong place. The API has no business
        formatting user-facing prose — it does not know the locale, and the
        frontend already holds both translations (`messages/{en,el}.json`). So
        the wire carries the NUMBERS (`ShapeOut`), and
        `interview-sources-step.tsx` formats them with `next-intl`. Anything that
        renders a server-built sentence to the tutor is a Greek bug waiting to
        happen; this method's job is to make a log line readable.
        """
        per = "+".join(str(n) for n in self.lessons_per_module)
        return (
            f"{self.lessons_total} sessions -> {self.modules} modules "
            f"({per} lessons) -> ~{self.target_words_per_lesson:,} words each "
            f"({self.teaching_minutes} min per session)"
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

    # 50 means 50: the whole session is teaching time. Q&A, when the tutor's
    # blueprint has it, is an ordinary weighted section INSIDE these minutes —
    # never a fixed carve-out (see the module docstring).
    return Shape(
        lessons_total=lessons_total,
        modules=modules,
        lessons_per_module=lessons_per_module,
        minutes_per_lesson=minutes,
        teaching_minutes=minutes,
        qa_minutes=0,
        target_words_per_lesson=target_words(minutes),
        floor_words_per_lesson=floor_words(minutes),
    )


# Placeholder copy for padded modules/lessons, in the COURSE's language. These
# strings land on Block.title / the outline editor in front of a Greek tutor —
# "Module 3" in the middle of his Greek course reads as a bug, not a hole.
_PAD_LABELS = {
    "el": {
        "module": "Ενότητα {n}",
        "lesson": "Μάθημα {n}",
        "coverage": "Η ενότητα προστέθηκε για να συμπληρωθεί το ζητούμενο μέγεθος "
                    "του προγράμματος — μετονόμασέ τη και όρισε τον στόχο της.",
    },
    "en": {
        "module": "Module {n}",
        "lesson": "Lesson {n}",
        "coverage": "This module was added to complete the requested course "
                    "length — rename it and set its objective.",
    },
}


def enforce_shape(outline: dict, shape: Shape, language: str = "el") -> dict:
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

    labels = _PAD_LABELS.get(language, _PAD_LABELS["el"])

    modules = modules[: shape.modules]
    while len(modules) < shape.modules:
        n = len(modules) + 1
        modules.append(
            {
                "title": labels["module"].format(n=n),
                "objective": "",
                "tier": "general_knowledge",
                "coverage_note": labels["coverage"],
                "lessons": [],
            }
        )

    out_modules = []
    for module, wanted in zip(modules, shape.lessons_per_module):
        lessons = list(module.get("lessons") or [])[:wanted]
        while len(lessons) < wanted:
            lessons.append(
                {
                    "title": labels["lesson"].format(n=len(lessons) + 1),
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
