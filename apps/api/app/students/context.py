"""The STUDENT BRIEF: everything the tutor knows about one student, rendered as
prose a model can actually use, for injection into the prompts that write his
lessons.

TWO THINGS WERE WRONG, AND THE SECOND IS THE EXPENSIVE ONE.

1. The student never reached the content prompt. `level` and `name` appeared in
   the OUTLINE prompt and nowhere else — so every lesson BODY in every curriculum
   this app has ever generated was written with zero knowledge of the learner. The
   personalization was in the table of contents.

2. It was rendered as a Python dict repr. `Student profile: {'student_name':
   'Nikos', 'level': 'beginner', 'weeks': 20}` — a model reads that as noise with
   some words in it. Prose is not a nicety here; it is the difference between the
   model using the information and the model stepping around it.

NO VECTOR RAG. DIRECT INJECTION. A solo tutor has TENS of notes about a student,
not thousands — the whole set is a few hundred tokens. Retrieving the "relevant"
ones would add an embedding call, a floor, a failure mode, and a way to silently
drop the one note that mattered ("he cannot barre yet"), in exchange for saving
tokens we were never short of. The correct amount of retrieval over 40 notes is
none.

THE STUDENT IS FULLY OPTIONAL. Chris: "this has to be optional dude.. the student
part here has to be TOTALLY optional". `build_student_brief(db, None)` returns
None, and every caller appends nothing — so a curriculum authored for no
particular student produces BYTE-IDENTICAL prompts to one authored before this
module existed. That is the property that makes this safe to thread through four
job producers at once.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select

from app.i18n import language_name
from app.models.note import Note
from app.models.student import Student

# Notes carrying this tag are what the tutor has flagged as "he is stuck on this".
# Surfaced FIRST in the brief, before the general notes, because a lesson that
# ignores a known struggle is worse than a generic one — it teaches past him.
STRUGGLE_TAG = "struggle"

# How many notes reach the prompt. A cap, not a ranking: a tutor with 200 notes on
# one student is not the case this is designed for, and truncating the OLDEST is
# the least surprising rule when it happens. Newest first.
MAX_NOTES = 40


def _age(birthdate: date | None) -> int | None:
    if birthdate is None:
        return None
    today = date.today()
    return today.year - birthdate.year - (
        (today.month, today.day) < (birthdate.month, birthdate.day)
    )


def build_student_brief(db, student_id: UUID | None) -> str | None:
    """Prose brief for `student_id`, or None when there is no student.

    Returns None — not an empty string, not "no student selected" — because None
    is what makes the caller's `if brief:` skip the whole block and leave the
    prompt exactly as it was. A placeholder sentence would be a real change to
    every prompt in the app to say nothing.
    """
    if student_id is None:
        return None
    student = db.get(Student, student_id)
    if student is None:
        return None

    facts: list[str] = []
    age = _age(student.birthdate)
    if age is not None:
        facts.append(f"{age} years old")
    if student.level:
        facts.append(f"{student.level} level")
    if student.instrument:
        facts.append(f"plays {student.instrument}")
    if student.preferred_language:
        facts.append(f"is taught in {language_name(student.preferred_language)}")

    lines = [f"THE STUDENT: {student.name}" + (f" — {', '.join(facts)}." if facts else ".")]

    if student.goals:
        lines.append(f"What he wants from these lessons: {student.goals.strip()}")

    notes = db.scalars(
        select(Note)
        .where(Note.student_id == student.id)
        .order_by(Note.created_at.desc())
        .limit(MAX_NOTES)
    ).all()

    struggles = [n for n in notes if STRUGGLE_TAG in (n.tags or [])]
    others = [n for n in notes if n not in struggles]

    if struggles:
        lines.append("What he is STRUGGLING with (the tutor flagged these — teach to them):")
        lines.extend(f"  - {n.title}: {n.body.strip()}" for n in struggles)
    if others:
        lines.append("The tutor's other notes on him:")
        lines.extend(f"  - {n.title}: {n.body.strip()}" for n in others)

    lines.append(
        "Write for THIS student: pitch the explanations at his level, and where "
        "his notes say he is stuck, address it directly instead of teaching past it."
    )
    return "\n".join(lines)
