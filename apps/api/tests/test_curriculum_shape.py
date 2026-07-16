"""The shape and depth arithmetic (Plan 13, Stage 6.1) — pure functions, no DB,
no model.

Chris, looking at the deployed app: "im making a curriculum with 20 weeks, and
only 4 modules are here. are those enough? i dont want us to be frugal here."
And: "each lesson has to be around 40 mins, then 10 mins for questions/discussion,
and those 40 mins has to be around 4-5 pages."

Both complaints are arithmetic, and both used to be *suggestions in a prompt*.
These tests are where they become facts.
"""
import pytest

from app.curriculum.depth import (
    DEEPEN_MAX_PASSES,
    FLOOR_RATIO,
    LESSON_DRAFT_SCHEMA,
    SECTION_WEIGHTS,
    SECTIONS,
    WORDS_PER_MINUTE,
    count_words,
    floor_words,
    measure,
    target_words,
)
from app.curriculum.shape import QA_MINUTES, enforce_shape, plan_shape
from app.llm.errors import GuidedJSONError
from app.llm.schema import to_anthropic_schema, unsupported_keywords


# ---------------------------------------------------------------------------
# THE COMPLAINT, ANSWERED
# ---------------------------------------------------------------------------

def test_twenty_weeks_is_five_modules_of_four_lessons_not_four_modules():
    """Chris's actual sentence, as an assertion."""
    shape = plan_shape(20, 1, 50)

    assert shape.lessons_total == 20
    assert shape.modules == 5
    assert shape.lessons_per_module == (4, 4, 4, 4, 4)


def test_a_fifty_minute_session_is_forty_taught_minutes_and_a_ten_minute_qa():
    """"each lesson has to be around 40 mins, then 10 mins for questions" — so the
    word target is computed from the TAUGHT minutes, not the session length.
    Counting the Q&A block as prose would inflate every lesson by 25% and then fail
    it for being thin."""
    shape = plan_shape(20, 1, 50)

    assert shape.teaching_minutes == 40
    assert shape.qa_minutes == QA_MINUTES == 10
    # "those 40 mins has to be around 4-5 pages" — 2,200 words is ~4.5 pages.
    assert shape.target_words_per_lesson == 2200
    assert shape.floor_words_per_lesson == 1760


def test_the_derived_shape_is_echoed_back_to_the_tutor_before_he_pays_for_it():
    line = plan_shape(20, 1, 50).describe()
    assert "20 sessions" in line
    assert "5 modules" in line
    assert "2,200 words" in line


@pytest.mark.parametrize(
    "weeks,per_week,expect_modules,expect_split",
    [
        (20, 1, 5, (4, 4, 4, 4, 4)),
        (8, 1, 2, (4, 4)),
        (6, 1, 2, (3, 3)),          # remainder spread, NOT [4, 2]
        (10, 1, 3, (4, 3, 3)),      # remainder FRONT-loaded, while he is fresh
        (3, 1, 1, (3,)),
        (1, 1, 1, (1,)),
        (12, 2, 6, (4, 4, 4, 4, 4, 4)),   # twice a week => 24 sessions
    ],
)
def test_the_lessons_always_add_up_to_the_sessions_he_asked_for(
    weeks, per_week, expect_modules, expect_split,
):
    shape = plan_shape(weeks, per_week, 50)
    assert shape.modules == expect_modules
    assert shape.lessons_per_module == expect_split
    # THE identity `enforce_shape` depends on.
    assert sum(shape.lessons_per_module) == shape.lessons_total == weeks * per_week


def test_a_short_session_still_gets_real_teaching_minutes():
    """A 12-minute session is not 2 minutes of teaching and 10 of Q&A — a word
    target of ~110 would make every lesson trivially 'deep enough' and the floor
    would stop meaning anything."""
    shape = plan_shape(4, 1, 12)
    assert shape.teaching_minutes >= 10
    assert shape.target_words_per_lesson > 0


@pytest.mark.parametrize("bad", [(0, 1, 50), (4, 0, 50), (4, 1, 0), (-2, 1, 50)])
def test_plan_shape_refuses_a_nonsense_course(bad):
    with pytest.raises(ValueError):
        plan_shape(*bad)


# ---------------------------------------------------------------------------
# enforce_shape IS the enforcement — the schema cannot be
# ---------------------------------------------------------------------------

def _outline(n_modules: int, lessons_each: int) -> dict:
    return {
        "title": "Blues",
        "modules": [
            {
                "title": f"Module {i}",
                "objective": "obj",
                "tier": "library",
                "coverage_note": "p.19",
                "lessons": [
                    {"title": f"L{j}", "objective": "o", "est_minutes": 30}
                    for j in range(lessons_each)
                ],
            }
            for i in range(n_modules)
        ],
    }


def test_a_model_that_returns_four_modules_for_twenty_weeks_is_corrected_not_accepted():
    """THE regression. The old code asked for "about 2-6 modules" in a prompt and
    persisted whatever came back — which is how the tutor got 4 modules for a
    20-week course."""
    shape = plan_shape(20, 1, 50)

    fixed = enforce_shape(_outline(4, 4), shape)

    assert len(fixed["modules"]) == 5
    assert [len(m["lessons"]) for m in fixed["modules"]] == [4, 4, 4, 4, 4]


def test_an_over_supplying_model_is_truncated():
    shape = plan_shape(8, 1, 50)   # 2 modules x 4
    fixed = enforce_shape(_outline(6, 9), shape)

    assert len(fixed["modules"]) == 2
    assert [len(m["lessons"]) for m in fixed["modules"]] == [4, 4]


def test_padding_is_honestly_labelled_rather_than_invented():
    """A padded module is a HOLE THE TUTOR CAN SEE AND RENAME, not a fabricated
    module pretending to be content. Under-supply must not fail the job either — a
    19-lesson answer to a 20-lesson request is a good outline with a gap in it."""
    shape = plan_shape(20, 1, 50)
    fixed = enforce_shape(_outline(3, 4), shape)

    padded = fixed["modules"][4]
    assert padded["lessons"] == [] or len(padded["lessons"]) == 4
    assert padded["tier"] == "general_knowledge", "a placeholder must never claim to be from his library"
    # Default locale is GREEK — the padded placeholder speaks it (review fix:
    # "Module 5" in the middle of a Greek course read as a bug, not a hole).
    assert "μετονόμασέ" in padded["coverage_note"]
    assert padded["title"] == "Ενότητα 5"

    # And English courses still pad in English.
    fixed_en = enforce_shape(_outline(3, 4), shape, language="en")
    assert "rename" in fixed_en["modules"][4]["coverage_note"]
    assert fixed_en["modules"][4]["title"] == "Module 5"


def test_every_lesson_gets_the_session_length_the_tutor_actually_booked():
    shape = plan_shape(20, 1, 50)
    fixed = enforce_shape(_outline(5, 4), shape)   # the model said 30 minutes
    for module in fixed["modules"]:
        for lesson in module["lessons"]:
            assert lesson["est_minutes"] == 50


def test_an_outline_with_no_modules_raises_instead_of_persisting_an_empty_course():
    """The guard nobody had. An empty `modules` list used to sail into
    `materialize_outline` and persist a course Block with no children — a
    curriculum that exists, is listed, and is empty."""
    with pytest.raises(GuidedJSONError):
        enforce_shape({"title": "Blues", "modules": []}, plan_shape(20, 1, 50))


# ---------------------------------------------------------------------------
# Word counting — GREEK-SAFE, which is not optional when Greek is the default
# ---------------------------------------------------------------------------

def test_count_words_counts_greek():
    """`\\w+` with re.UNICODE, not `.split()`. A counter that returned 0 for Greek
    would report every Greek lesson as infinitely thin and trigger a deepen pass on
    all twenty of them."""
    assert count_words("μια συγχορδία ματζόρε") == 3
    assert count_words("Η πεντατονική κλίμακα της Λα.") == 5
    assert count_words("") == 0
    assert count_words(None) == 0


def test_count_words_does_not_count_punctuation_as_words():
    assert count_words("C — Am7 — G7!") == 3


def test_the_word_target_is_the_measured_constant_not_a_guess():
    assert WORDS_PER_MINUTE == 55
    assert FLOOR_RATIO == 0.8
    assert target_words(40) == 2200
    assert floor_words(40) == 1760


def test_deepen_is_capped_at_one_pass():
    """He is paying per token with his own card. A model that missed the floor twice
    will not find another 400 words of substance on the third ask — it will pad."""
    assert DEEPEN_MAX_PASSES == 1


# ---------------------------------------------------------------------------
# measure()
# ---------------------------------------------------------------------------

def _lesson(words_per_section: int, *, greek: bool = False) -> dict:
    word = "συγχορδία" if greek else "word"
    body = " ".join([word] * words_per_section)
    lesson = {"title": "T", "summary": "s"}
    for name in SECTIONS:
        lesson[name] = {"body": body, "citations": []}
    return lesson


def test_a_thin_lesson_is_detected_and_its_thin_sections_are_named():
    """Naming the thin sections is what lets the deepen pass be AIMED. Told only
    'make it longer', a model produces a longer warm-up and nothing else."""
    m = measure(_lesson(20), teaching_minutes=40)

    assert m.total_words < m.floor
    assert m.needs_deepening
    assert not m.meets_floor
    assert "theory" in m.thin_sections
    assert "exercises" in m.thin_sections


def test_a_full_length_lesson_clears_the_floor_and_needs_no_deepening():
    # ~2,200 words spread across the sections in their own weights.
    lesson = {"title": "T", "summary": "s"}
    for name in SECTIONS:
        n = int(SECTION_WEIGHTS[name] * 2200)
        lesson[name] = {"body": " ".join(["word"] * n), "citations": []}

    m = measure(lesson, teaching_minutes=40)

    assert m.meets_floor, (m.total_words, m.floor)
    assert not m.needs_deepening
    assert m.thin_sections == []


def test_a_greek_lesson_is_measured_the_same_as_an_english_one():
    en = measure(_lesson(300), teaching_minutes=40)
    el = measure(_lesson(300, greek=True), teaching_minutes=40)
    assert en.total_words == el.total_words


def test_exercise_instructions_and_answer_keys_are_counted_as_substance():
    """A large part of a real lesson lives in `exercises.items[].instructions` and
    `qa_prompts.items[].answer_key`. Counting only `body` would report a fully
    written exercise set as an empty section and send it back to be deepened."""
    lesson = _lesson(10)
    lesson["exercises"] = {
        "body": "short",
        "items": [
            {"title": "Riff", "instructions": " ".join(["word"] * 500), "est_minutes": 5},
        ],
        "citations": [],
    }
    lesson["qa_prompts"] = {
        "body": "short",
        "items": [{"question": "why?", "answer_key": " ".join(["word"] * 200)}],
        "citations": [],
    }

    m = measure(lesson, teaching_minutes=40)

    assert m.per_section["exercises"] > 500
    assert m.per_section["qa_prompts"] > 200
    # A fully-written exercise set is NOT thin, even though its `body` is one word.
    assert "exercises" not in m.thin_sections
    assert "qa_prompts" not in m.thin_sections
    # ...and the section next to it, which really is empty, still is.
    assert "theory" in m.thin_sections


# ---------------------------------------------------------------------------
# The lesson schema itself
# ---------------------------------------------------------------------------

def test_the_lesson_schema_is_tutor_student_separable_and_carries_answer_keys():
    """Designed ONCE, now, with the print/handout stage in mind — so producing the
    tutor's script and the student's handout never forces a re-draft of 20 lessons."""
    props = LESSON_DRAFT_SCHEMA["properties"]
    for section in ("warm_up", "theory", "demonstration", "exercises",
                    "common_mistakes", "recap", "homework", "qa_prompts"):
        assert section in props

    qa_item = props["qa_prompts"]["properties"]["items"]["items"]["properties"]
    assert "question" in qa_item and "answer_key" in qa_item, (
        "the tutor is holding this page while the student answers — a question "
        "without its answer key is a question he cannot use"
    )


def test_every_section_can_carry_page_citations():
    for section in SECTIONS:
        cites = LESSON_DRAFT_SCHEMA["properties"][section]["properties"]["citations"]
        item = cites["items"]["properties"]
        assert "source_id" in item and "page" in item


def test_the_lesson_schema_survives_the_anthropic_sanitizer():
    """Claude's structured outputs reject `minItems`/`minLength`/`minimum` — the SDK
    strips them and validates client-side, i.e. it raises AFTER you have paid for
    the tokens. So the schema must carry none of them, and the counts/lengths are
    enforced in the prompt and in `depth`/`shape` instead."""
    clean = to_anthropic_schema(LESSON_DRAFT_SCHEMA)
    assert unsupported_keywords(clean) == []
