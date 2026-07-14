"""How LONG a lesson has to be, how we measure it, and the one lesson shape the
whole app agrees on.

Chris, on what he actually wants out of a generated lesson: "each lesson has to
be around 40 mins, then 10 mins for questions/discussion, and those 40 mins has
to be around 4-5 pages." Four to five pages of teaching notes is ~2,000-2,400
words. At the 40 taught minutes `app.curriculum.shape` derives from a 50-minute
session, `WORDS_PER_MINUTE = 55` lands on 2,200 — the middle of his range — and
`FLOOR_RATIO = 0.8` says a lesson that comes back under 1,760 words is not a
lesson he can teach from, it is an outline wearing a lesson's clothes.

NOTHING IN THE API ENFORCES THIS. Claude's structured outputs do not support
`minLength` (`llm/schema.py` strips it before the call, so the SDK cannot even
raise on it after billing). The word floor is therefore stated in the PROMPT and
verified HERE, in Python, after the fact — and a thin lesson gets exactly one
`deepen` pass (`DEEPEN_MAX_PASSES`) rather than an unbounded rewrite loop with
the tutor's credit card attached.

`count_words` uses `re.findall(r"\\w+", t, re.UNICODE)`. That is not a stylistic
choice: `str.split()` on Greek is fine, but `\\w` with `re.UNICODE` is what makes
«συγχορδία» one word and not zero, and the default curriculum language IS Greek.
A word counter that quietly returns 0 for Greek would report every Greek lesson
as infinitely thin and trigger a deepen pass on all twenty of them.

LESSON_DRAFT_SCHEMA IS DESIGNED ONCE, HERE, WITH THE PRINT STAGE ALREADY IN MIND.
The tutor's script and the student's handout are two documents cut from one
draft: `warm_up`/`theory`/`demonstration`/`common_mistakes`/`qa_prompts` are the
teacher's half, `exercises`/`homework`/`recap` are the student's, and every
section carries its own `citations`. If the schema had to grow a field later,
twenty already-drafted lessons would have to be re-drafted to get it — so it
grows now, while a lesson costs nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Words a tutor speaks-and-demonstrates per minute of lesson, as PLANNED PROSE
# (not speech rate — a teaching script is read, paused over, and played through
# on the guitar). Calibrated backwards from Chris's own "4-5 pages for 40
# minutes": 40 x 55 = 2,200 words ~= 4.5 pages at 480 words/page.
WORDS_PER_MINUTE = 55

# A lesson may come in under target — prose is not a commodity — but not by much.
# 0.8 of 2,200 is 1,760: still four pages. Below that the model has given us
# bullet points and called them a lesson.
FLOOR_RATIO = 0.8

# ONE deepen pass. Hard. Two reasons, both about the tutor rather than the code:
# he is paying per token with his own card (a 20-lesson curriculum at 32k output
# tokens each is already the most expensive thing this app does), and a model
# that missed the floor twice is not going to find another 400 words of substance
# on the third ask — it will pad. A lesson still under floor after one deepen is
# persisted as it is, with its measured word count on `meta`, and the tutor gets
# a visible word-count chip and a Deepen button rather than a silent retry loop.
DEEPEN_MAX_PASSES = 1

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# The teaching body's sections, and roughly how much of the lesson each one is.
# Weights sum to 1.0 and exist for ONE purpose: to name which section is thin, so
# a deepen pass can be told "expand `theory` and `exercises`" instead of "make it
# longer", which produces a longer warm-up and nothing else.
SECTION_WEIGHTS: dict[str, float] = {
    "warm_up": 0.07,
    "theory": 0.25,
    "demonstration": 0.18,
    "exercises": 0.22,
    "common_mistakes": 0.10,
    "recap": 0.06,
    "homework": 0.06,
    "qa_prompts": 0.06,
}
SECTIONS: tuple[str, ...] = tuple(SECTION_WEIGHTS)

# A section is THIN below this fraction of its own share. Deliberately generous:
# the aggregate floor (`FLOOR_RATIO`) is the real gate, and a per-section rule
# that fired on every small deviation would send every lesson through a deepen
# pass and double the bill for nothing.
_SECTION_THIN_RATIO = 0.6


def count_words(text: str | None) -> int:
    """Greek-safe word count. See this module's docstring for why `\\w+` and not
    `.split()`."""
    if not text:
        return 0
    return len(_WORD_RE.findall(text))


def target_words(teaching_minutes: int) -> int:
    return teaching_minutes * WORDS_PER_MINUTE


def floor_words(teaching_minutes: int) -> int:
    return int(target_words(teaching_minutes) * FLOOR_RATIO)


# ---------------------------------------------------------------------------
# The lesson shape
# ---------------------------------------------------------------------------

_CITATIONS = {
    "type": "array",
    "description": (
        "Every page of the tutor's library this section actually drew on. "
        "source_id is the id attribute of the <source> element (e.g. \"S1\"); "
        "page is the [p.N] marker the text you used sat under. Cite ONLY pages "
        "you actually read in the library block. An empty array is the correct, "
        "honest answer for a section written from general knowledge."
    ),
    "items": {
        "type": "object",
        "properties": {
            "source_id": {"type": "string"},
            "page": {"type": "integer"},
        },
        "required": ["source_id", "page"],
        "additionalProperties": False,
    },
}


def _prose_section(description: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": description},
            "citations": _CITATIONS,
        },
        "required": ["body", "citations"],
        "additionalProperties": False,
    }


# THE lesson shape. One draft, two documents (see the module docstring).
LESSON_DRAFT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {
            "type": "string",
            "description": "Two sentences the tutor can read at a glance before the lesson.",
        },
        "warm_up": _prose_section(
            "5 minutes of playing to open the session. Concrete: what to play, at "
            "what tempo, why it prepares this lesson's material."
        ),
        "theory": _prose_section(
            "The concept, taught. Full prose the tutor can read aloud or teach "
            "from — not bullet points, not a summary of a lesson."
        ),
        "demonstration": _prose_section(
            "What the tutor plays, step by step, and what the student should be "
            "listening for. Name the fret positions, the chords, the tempo."
        ),
        "exercises": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "Prose introducing and sequencing the exercises.",
                },
                "items": {
                    "type": "array",
                    "description": "Each exercise the student actually plays.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "instructions": {"type": "string"},
                            "est_minutes": {"type": "integer"},
                        },
                        "required": ["title", "instructions", "est_minutes"],
                        "additionalProperties": False,
                    },
                },
                "citations": _CITATIONS,
            },
            "required": ["body", "items", "citations"],
            "additionalProperties": False,
        },
        "common_mistakes": _prose_section(
            "What students actually get wrong here, how it sounds when they do, "
            "and the correction the tutor gives."
        ),
        "recap": _prose_section("What was covered, in the words the student will remember."),
        "homework": _prose_section(
            "What to practise before the next session, for how long, and how the "
            "student knows they have it right."
        ),
        "qa_prompts": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "How to open the 10-minute discussion block.",
                },
                "items": {
                    "type": "array",
                    "description": (
                        "Questions to put to the student, EACH WITH AN ANSWER KEY — "
                        "the tutor is holding this page while the student answers."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "answer_key": {"type": "string"},
                        },
                        "required": ["question", "answer_key"],
                        "additionalProperties": False,
                    },
                },
                "citations": _CITATIONS,
            },
            "required": ["body", "items", "citations"],
            "additionalProperties": False,
        },
    },
    "required": ["title", "summary", *SECTIONS],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

@dataclass
class Measurement:
    total_words: int
    target: int
    floor: int
    per_section: dict[str, int] = field(default_factory=dict)
    thin_sections: list[str] = field(default_factory=list)

    @property
    def meets_floor(self) -> bool:
        return self.total_words >= self.floor

    @property
    def needs_deepening(self) -> bool:
        """A lesson is deepened when it misses the AGGREGATE floor — not merely
        because one section is under its share. A 2,400-word lesson with a short
        recap does not need another model call.
        """
        return not self.meets_floor


def section_words(section: dict | None) -> int:
    """Words in one `LESSON_DRAFT_SCHEMA` section, including its `items`.

    `exercises.items[].instructions` and `qa_prompts.items[].answer_key` are
    where a large part of a real lesson's substance lives — counting only `body`
    would report a fully-written exercise set as an empty section and send it
    back for deepening.
    """
    if not isinstance(section, dict):
        return 0
    total = count_words(section.get("body"))
    for item in section.get("items") or []:
        if not isinstance(item, dict):
            continue
        for value in item.values():
            if isinstance(value, str):
                total += count_words(value)
    return total


def measure(lesson: dict, *, teaching_minutes: int) -> Measurement:
    """Word-count one drafted lesson against its own target, and name the thin
    sections so a deepen pass can be aimed rather than sprayed."""
    target = target_words(teaching_minutes)
    floor = floor_words(teaching_minutes)

    per_section = {name: section_words(lesson.get(name)) for name in SECTIONS}
    total = count_words(lesson.get("summary")) + sum(per_section.values())

    thin = [
        name
        for name, words in per_section.items()
        if words < _SECTION_THIN_RATIO * SECTION_WEIGHTS[name] * target
    ]
    return Measurement(
        total_words=total, target=target, floor=floor,
        per_section=per_section, thin_sections=thin,
    )
