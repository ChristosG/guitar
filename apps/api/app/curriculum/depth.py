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

from app.curriculum.inline_marks import strip_inline_marks

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

# The section headings that land on `Block.title` (a NOT NULL column). The FRONTEND
# renders from `meta.section` — the stable machine key — and is free to ignore
# these; they exist so a segment row is legible in the database, in a CSV export,
# and in the printed handout, without every consumer needing a translation table.
#
# Lives here (a pure module) rather than in `draft.py` so that `blueprint.py` —
# and anything importing it, e.g. the settings router — is not dragged through
# `draft.py`'s retrieval/LLM import chain (corpus, ground, llm.factory,
# prompts.overrides) just to read a label dict. `draft.py` re-exports it below
# for any external consumer still resolving `draft.SECTION_LABELS`.
SECTION_LABELS: dict[str, dict[str, str]] = {
    "el": {
        "warm_up": "Ζέσταμα",
        "theory": "Θεωρία",
        "demonstration": "Επίδειξη",
        "exercises": "Ασκήσεις",
        "common_mistakes": "Συνήθη λάθη",
        "recap": "Ανακεφαλαίωση",
        "homework": "Εργασία για το σπίτι",
        "qa_prompts": "Ερωτήσεις & συζήτηση",
    },
    "en": {
        "warm_up": "Warm-up",
        "theory": "Theory",
        "demonstration": "Demonstration",
        "exercises": "Exercises",
        "common_mistakes": "Common mistakes",
        "recap": "Recap",
        "homework": "Homework",
        "qa_prompts": "Q&A and discussion",
    },
}

# A section is THIN below this fraction of its own share. Deliberately generous:
# the aggregate floor (`FLOOR_RATIO`) is the real gate, and a per-section rule
# that fired on every small deviation would send every lesson through a deepen
# pass and double the bill for nothing.
_SECTION_THIN_RATIO = 0.6


def count_words(text: str | None) -> int:
    """Greek-safe word count. See this module's docstring for why `\\w+` and not
    `.split()`.

    The inline marks are stripped first: `<u>` would otherwise score as the word
    "u" (`\\w+` matches the tag's letter), so an underlined lesson would measure
    LONGER than the same lesson unformatted and could slip past the thin-lesson
    floor on formatting alone."""
    if not text:
        return 0
    return len(_WORD_RE.findall(strip_inline_marks(text)))


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


# The two ALWAYS-PRESENT, non-section properties, as named constants so that this
# module's `LESSON_DRAFT_SCHEMA` and `blueprint.build_lesson_schema` share ONE
# source for them (title/summary are not blueprint sections — they are fixed).
_TITLE_PROP: dict = {"type": "string"}
_SUMMARY_PROP: dict = {
    "type": "string",
    "description": "Two sentences the tutor can read at a glance before the lesson.",
}

# The eight section body/prose `description` strings, extracted VERBATIM from the
# schema so they have exactly one home. `app.curriculum.blueprint._DEFAULT_SECTIONS`
# imports and references these — which is what makes `default_blueprint()` produce a
# byte-identical schema by CONSTRUCTION rather than by careful re-typing (see the
# plan's resolved design call #5). A reworded string here is a changed model prompt,
# and `tests/test_blueprint_schema_golden.py` is the backstop that catches it.
_DESC_WARM_UP = (
    "5 minutes of playing to open the session. Concrete: what to play, at "
    "what tempo, why it prepares this lesson's material."
)
_DESC_THEORY = (
    "The concept, taught. Full prose the tutor can read aloud or teach "
    "from — not bullet points, not a summary of a lesson."
)
_DESC_DEMONSTRATION = (
    "What the tutor plays, step by step, and what the student should be "
    "listening for. Name the fret positions, the chords, the tempo."
)
_DESC_EXERCISES_BODY = "Prose introducing and sequencing the exercises."
_DESC_COMMON_MISTAKES = (
    "What students actually get wrong here, how it sounds when they do, "
    "and the correction the tutor gives."
)
_DESC_RECAP = "What was covered, in the words the student will remember."
_DESC_HOMEWORK = (
    "What to practise before the next session, for how long, and how the "
    "student knows they have it right."
)
_DESC_QA_BODY = "How to open the 10-minute discussion block."


def _exercises_section(body_description: str) -> dict:
    """The `exercises` section shape: a body plus the exercises the student plays.
    Extracted from the inline schema literal so the blueprint can assemble it
    per-kind; `body_description` is the one field a blueprint can vary."""
    return {
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": body_description},
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
    }


def _qa_section(body_description: str) -> dict:
    """The `qa_prompts` section shape: a body plus question/answer-key pairs the
    tutor holds while the student answers. Extracted for the same reason."""
    return {
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": body_description},
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
    }


# Which builder assembles each STRUCTURED (non-prose) section, keyed by `kind`.
# `blueprint.build_lesson_schema` dispatches on this; `prose` sections use
# `_prose_section`. These two structured kinds keep a fixed `items[]` schema and
# can never be renamed or kind-changed (spec invariant #4).
_KIND_BUILDERS = {"exercises": _exercises_section, "qa": _qa_section}


# THE lesson shape. One draft, two documents (see the module docstring). Assembled
# from the same constants/builders the blueprint uses, so the schema this module
# exposes and `build_lesson_schema(default_blueprint())` cannot drift apart.
LESSON_DRAFT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": _TITLE_PROP,
        "summary": _SUMMARY_PROP,
        "warm_up": _prose_section(_DESC_WARM_UP),
        "theory": _prose_section(_DESC_THEORY),
        "demonstration": _prose_section(_DESC_DEMONSTRATION),
        "exercises": _exercises_section(_DESC_EXERCISES_BODY),
        "common_mistakes": _prose_section(_DESC_COMMON_MISTAKES),
        "recap": _prose_section(_DESC_RECAP),
        "homework": _prose_section(_DESC_HOMEWORK),
        "qa_prompts": _qa_section(_DESC_QA_BODY),
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


def measure(
    lesson: dict, blueprint: dict | None = None, *, teaching_minutes: int,
) -> Measurement:
    """Word-count one drafted lesson against its own target, and name the thin
    sections so a deepen pass can be aimed rather than sprayed.

    The set of sections, their ORDER, and their thin-detection weights all come from
    `blueprint` — `None` means the code default, which reproduces the old
    `SECTIONS`/`SECTION_WEIGHTS` behaviour byte-for-byte (that is the point of the
    default: an un-edited course measures exactly as it did before the blueprint
    existed). Imported lazily to avoid a depth<->blueprint import cycle."""
    from app.curriculum import blueprint as _bp

    bp = blueprint if blueprint is not None else _bp.default_blueprint()
    keys = _bp.section_keys(bp)
    weights = _bp.section_weights(bp)

    target = target_words(teaching_minutes)
    floor = floor_words(teaching_minutes)

    per_section = {name: section_words(lesson.get(name)) for name in keys}
    total = count_words(lesson.get("summary")) + sum(per_section.values())

    thin = [
        name
        for name, words in per_section.items()
        if words < _SECTION_THIN_RATIO * weights[name] * target
    ]
    return Measurement(
        total_words=total, target=target, floor=floor,
        per_section=per_section, thin_sections=thin,
    )
