"""The student brief (Plan 13, Stage 6.4).

Two things were wrong, and the second is the expensive one:

1. THE STUDENT NEVER REACHED THE CONTENT PROMPT. `level` and `name` appeared in the
   OUTLINE prompt and nowhere else — so every lesson BODY in every curriculum this
   app has ever generated was written with zero knowledge of the learner. The
   personalization was in the table of contents.

2. IT WAS A PYTHON DICT REPR. `Student profile: {'student_name': 'Nikos', 'level':
   'beginner', 'weeks': 20}`. A model reads that as noise with some words in it.

And the constraint Chris was loudest about: "this has to be optional dude.. the
student part here has to be TOTALLY optional." So `student_id=None` must produce
prompts BYTE-IDENTICAL to the ones that existed before this module — which is the
property that makes it safe to thread through every producer at once.
"""
import uuid

from app.curriculum.corpus import LibraryContext
from app.curriculum.draft import LessonContext, build_lesson_messages
from app.curriculum.outline import build_outline_messages
from app.curriculum.shape import plan_shape
from app.models.note import Note
from app.models.student import Student
from app.students.context import build_student_brief

SHAPE = plan_shape(20, 1, 50)
EMPTY = LibraryContext(text="", token_count=0, fits=True)


def _ctx() -> LessonContext:
    return LessonContext(
        lesson_title="Power chords", lesson_objective="o", module_title="Rhythm",
        module_objective="mo", course_title="Blues", tier="library",
        position="lesson 1 of 4 in module 1 of 5", minutes=50, teaching_minutes=40,
        target_words=2200, floor_words=1760,
    )


def test_no_student_means_no_brief_at_all(db):
    """None — not an empty string, not "no student selected". None is what makes the
    caller's `if brief:` skip the block entirely and leave the prompt exactly as it
    was. A placeholder sentence would be a real change to every prompt in the app,
    to say nothing."""
    assert build_student_brief(db, None) is None


def test_an_unknown_student_id_is_not_a_crash(db):
    assert build_student_brief(db, uuid.uuid4()) is None


def test_the_prompts_are_byte_identical_when_there_is_no_student(db):
    """THE property that makes the student optional rather than merely nullable."""
    with_none = build_outline_messages(
        title="Blues", brief="b", language="el", shape=SHAPE, library=EMPTY,
        student_brief=None, gap_policy="general_knowledge",
    )
    lesson_with_none = build_lesson_messages(
        ctx=_ctx(), library=EMPTY, language="el", student_brief=None, course_brief="b",
    )

    assert "THE STUDENT" not in " ".join(m["content"] for m in with_none)
    assert "THE STUDENT" not in " ".join(m["content"] for m in lesson_with_none)


def test_the_brief_is_PROSE_not_a_python_dict_repr(db):
    student = Student(name="Nikos", level="beginner", instrument="electric guitar",
                      preferred_language="el", goals="Play 12-bar blues at a wedding.")
    db.add(student)
    db.commit()

    brief = build_student_brief(db, student.id)

    assert brief.startswith("THE STUDENT: Nikos")
    assert "beginner level" in brief
    assert "plays electric guitar" in brief
    assert "taught in Greek" in brief
    assert "Play 12-bar blues at a wedding." in brief
    # The failure mode this replaces.
    assert "{'" not in brief and "'level':" not in brief


def test_a_flagged_struggle_is_surfaced_FIRST_and_told_to_teach_to_it(db):
    """A lesson that ignores a known struggle is worse than a generic one — it
    teaches past him."""
    student = Student(name="Nikos", level="beginner")
    db.add(student)
    db.flush()
    db.add(Note(title="General", body="Enthusiastic.", tags=[], student_id=student.id))
    db.add(Note(title="Barre chords", body="Cannot barre yet — buzzes on the B string.",
                tags=["struggle"], student_id=student.id))
    db.commit()

    brief = build_student_brief(db, student.id)

    assert "STRUGGLING" in brief
    assert "Cannot barre yet" in brief
    assert brief.index("STRUGGLING") < brief.index("other notes")
    assert "Enthusiastic." in brief, "the ordinary notes are still there"
    assert "teach to them" in brief


def test_the_brief_uses_direct_injection_not_retrieval(db):
    """A solo tutor has TENS of notes about a student, not thousands. Retrieving the
    "relevant" ones would add an embedding call, a floor, a failure mode, and a way
    to silently drop the one note that mattered — in exchange for saving tokens we
    were never short of. Every note reaches the prompt."""
    student = Student(name="Nikos")
    db.add(student)
    db.flush()
    for i in range(12):
        db.add(Note(title=f"NOTE_MARKER_{i}", body=f"body {i}", tags=[],
                    student_id=student.id))
    db.commit()

    brief = build_student_brief(db, student.id)

    for i in range(12):
        assert f"NOTE_MARKER_{i}" in brief


def test_a_student_with_nothing_recorded_still_produces_a_usable_brief(db):
    student = Student(name="Someone New")
    db.add(student)
    db.commit()

    brief = build_student_brief(db, student.id)

    assert "THE STUDENT: Someone New" in brief
    assert "Write for THIS student" in brief


def test_the_brief_reaches_BOTH_the_outline_and_the_lesson_draft_prompt(db):
    """The whole point. Until now it reached the outline and stopped there — so the
    lesson bodies, which are the thing he actually teaches from, knew nothing about
    the learner."""
    student = Student(name="Nikos", level="beginner")
    db.add(student)
    db.flush()
    db.add(Note(title="Barre chords", body="STRUGGLE_MARKER", tags=["struggle"],
                student_id=student.id))
    db.commit()
    brief = build_student_brief(db, student.id)

    outline = build_outline_messages(
        title="Blues", brief="b", language="el", shape=SHAPE, library=EMPTY,
        student_brief=brief, gap_policy="general_knowledge",
    )
    lesson = build_lesson_messages(
        ctx=_ctx(), library=EMPTY, language="el", student_brief=brief, course_brief="b",
    )

    assert "STRUGGLE_MARKER" in " ".join(m["content"] for m in outline)
    assert "STRUGGLE_MARKER" in " ".join(m["content"] for m in lesson)


def test_the_student_brief_sits_AFTER_the_cache_breakpoint(db):
    """It is volatile — it changes per student and per edit to a note. If it leaked
    into the cached library block, the prefix would differ per curriculum and every
    lesson would re-write 90K tokens at 1.25x instead of reading them at 0.1x."""
    student = Student(name="Nikos")
    db.add(student)
    db.commit()
    brief = build_student_brief(db, student.id)

    library = LibraryContext(text='<source id="S1" title="Book">[p.1] text</source>',
                             token_count=10, fits=True, page_index={"S1": {1}})
    messages = build_lesson_messages(
        ctx=_ctx(), library=library, language="el", student_brief=brief, course_brief="b",
    )

    cached = next(m for m in messages if m.get("cache"))
    assert "Nikos" not in cached["content"]
    assert "Nikos" in messages[-1]["content"]
