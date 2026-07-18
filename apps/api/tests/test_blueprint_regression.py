"""Byte-identical regression for Plan C, Task 2.

The drafting/measuring code now reads a `blueprint`. The DEFAULT blueprint must
reproduce today's behaviour EXACTLY: same schema, same `measure()` numbers, same
section keys/order, same Greek/English section-title labels on the persisted
segments. The numbers and label strings below were CAPTURED from the current code
path BEFORE the signature change (per the plan) — they are the frozen backstop.
"""
from app.curriculum.blueprint import (
    default_blueprint,
    build_lesson_schema,
    section_keys,
    section_labels,
)
from app.curriculum.depth import LESSON_DRAFT_SCHEMA, measure


# --- The frozen sample lesson the legacy numbers were captured from ----------
def _lesson() -> dict:
    def prose(n):
        return {"body": " ".join(["word"] * n), "citations": []}

    return {
        "title": "Sample lesson title here",
        "summary": " ".join(["sum"] * 12),
        "warm_up": prose(40),
        "theory": prose(500),
        "demonstration": prose(300),
        "exercises": {
            "body": " ".join(["ex"] * 120),
            "items": [
                {"title": "Ex one", "instructions": " ".join(["do"] * 120), "est_minutes": 5},
                {"title": "Ex two", "instructions": " ".join(["do"] * 80), "est_minutes": 5},
            ],
            "citations": [],
        },
        "common_mistakes": prose(90),
        "recap": prose(30),
        "homework": prose(35),
        "qa_prompts": {
            "body": " ".join(["qa"] * 20),
            "items": [
                {"question": " ".join(["q"] * 10), "answer_key": " ".join(["a"] * 15)},
            ],
            "citations": [],
        },
    }


# --- Captured from the CURRENT code path, before the signature change --------
_LEGACY_TOTAL_WORDS = 1376
_LEGACY_PER_SECTION = {
    "warm_up": 40, "theory": 500, "demonstration": 300, "exercises": 324,
    "common_mistakes": 90, "recap": 30, "homework": 35, "qa_prompts": 45,
}
_LEGACY_THIN = ["warm_up", "common_mistakes", "recap", "homework", "qa_prompts"]

# Captured `depth.SECTION_LABELS` values (the strings that land on Block.title).
_LABELS_EL = {
    "warm_up": "Ζέσταμα",
    "theory": "Θεωρία",
    "demonstration": "Επίδειξη",
    "exercises": "Ασκήσεις",
    "common_mistakes": "Συνήθη λάθη",
    "recap": "Ανακεφαλαίωση",
    "homework": "Εργασία για το σπίτι",
    "qa_prompts": "Ερωτήσεις & συζήτηση",
}
_LABELS_EN = {
    "warm_up": "Warm-up",
    "theory": "Theory",
    "demonstration": "Demonstration",
    "exercises": "Exercises",
    "common_mistakes": "Common mistakes",
    "recap": "Recap",
    "homework": "Homework",
    "qa_prompts": "Q&A and discussion",
}


def test_schema_from_default_equals_module_constant():
    assert build_lesson_schema(default_blueprint()) == LESSON_DRAFT_SCHEMA


def test_measure_with_default_blueprint_matches_legacy_numbers():
    bp = default_blueprint()
    lesson = _lesson()
    m = measure(lesson, bp, teaching_minutes=40)
    assert m.total_words == _LEGACY_TOTAL_WORDS
    assert m.target == 2200
    assert m.floor == 1760
    assert m.per_section == _LEGACY_PER_SECTION
    assert list(m.per_section.keys()) == [
        "warm_up", "theory", "demonstration", "exercises",
        "common_mistakes", "recap", "homework", "qa_prompts",
    ]
    assert m.thin_sections == _LEGACY_THIN
    assert m.meets_floor is False


def test_default_section_keys_and_labels_match_capture():
    bp = default_blueprint()
    assert list(section_keys(bp)) == [
        "warm_up", "theory", "demonstration", "exercises",
        "common_mistakes", "recap", "homework", "qa_prompts",
    ]
    assert section_labels(bp, "el") == _LABELS_EL
    assert section_labels(bp, "en") == _LABELS_EN


def test_persist_lesson_labels_come_from_blueprint(db):
    """Persist a lesson under the default blueprint; the segment titles must be the
    exact captured Greek labels, meta['section'] keys/order must match the
    blueprint, and each segment must carry its section's audience (new plumbing)."""
    from app.curriculum.corpus import LibraryContext
    from app.curriculum.draft import persist_lesson
    from app.models.block import Block

    lesson_block = Block(kind="lesson", title="L1", language="el", meta={})
    db.add(lesson_block)
    db.commit()

    bp = default_blueprint()
    lesson = _lesson()
    library = LibraryContext(text="", token_count=0, fits=True)
    m = measure(lesson, bp, teaching_minutes=40)
    persist_lesson(
        db, lesson_block, lesson, m, library, bp,
        qa_minutes=10, teaching_minutes=40,
    )
    db.commit()
    db.expire_all()

    lesson_block = db.get(Block, lesson_block.id)
    segments = sorted(lesson_block.children, key=lambda b: b.order)

    assert [s.meta["section"] for s in segments] == list(section_keys(bp))

    titles = {s.meta["section"]: s.title for s in segments}
    assert titles == _LABELS_EL

    audiences = {s.meta["section"]: s.meta.get("audience") for s in segments}
    assert audiences["theory"] == "teacher"
    assert audiences["exercises"] == "student"
    assert audiences["recap"] == "student"
    assert audiences["qa_prompts"] == "teacher"
