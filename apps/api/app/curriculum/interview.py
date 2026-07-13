"""The guided curriculum-authoring interview (Plan 12 Task 3, G2).

Chris: "maybe llm can act as an assistant there bro, guiding him, and asking
him questions or corrections throughout the process. thats actually would be
dope." And, on how it should ground itself: "Interview first might also be
beneficial if we added here, so it is getting the passages needed while
reading the book -- if that makes sense?" The interview is NOT a form: as it
goes, it RETRIEVES the relevant passages from his actual library and shows
him what it found, so the curriculum that eventually gets built is built
from what he actually has, not the model's general knowledge.

THE HARD-WON DESIGN CONSTRAINT THIS MODULE OBEYS: the interview's STATE
MACHINE LIVES HERE, IN CODE — never in the model's head. Plan 10's live run
needed the model THREE attempts to track a single session id, and Plan 11's
own notes call it "unreliable at id-plumbing". A free-form chat asked to
remember which step it's on, what was already answered, and which sources
were chosen would break exactly the same way. Every step, transition, and
validation rule below is a plain Python `if` — the model's only involvement
anywhere in this module is the SAME `get_provider().guided_json` plan call
`app.curriculum.generate` already makes for Phase 1 (reused, not
reinvented), and it never sees or touches `step`/`answers`/`preview`.

FIVE STEPS, in this fixed order (`STEP_ORDER` below):
  who      -> student/level. Offers his REAL students (`Student` rows) as
              one-tap options; a free-text name is also accepted for
              someone not yet on the roster.
  duration -> weeks + minutes/session.
  sources  -> WHICH of his library sources to draw on. This is the step
              that matters most (see this task's report): his library
              currently contains one real OCR'd book and one synthetic
              filler source ("Guitar Tone & Gear — Course Spine", seeded by
              a script, not his content) that OUTSCORES the real book in
              retrieval on several topics. This step shows EVERY source's
              title/type/char_count so he can tell real material from
              filler and choose for himself — nothing is silently excluded;
              `default_selected` is only a suggestion (sources at/above
              `app.curriculum.ground.LEN_FLOOR`, the SAME "substantive"
              cutoff that module already uses to keep junk passages out of
              a draft prompt — reused here, not a second arbitrary number).
  preview  -> the step Chris asked for. Plans a module OUTLINE (reusing
              `generate.PLAN_SCHEMA`/`_build_plan_messages`/`get_provider` —
              the identical Phase 1 this module's non-interview sibling
              runs) and then calls `app.curriculum.ground.ground_topic` for
              EVERY planned module against the sources he just chose — the
              same retrieval `generate_curriculum` itself uses for Phase 2 —
              showing him what it actually found: real passages with
              source title + page, or an honest GAP when nothing cleared
              the relevance floor. Computed ONCE and cached on the row
              (`CurriculumInterview.preview`) so `GET .../{id}` (a refresh)
              never re-runs an LLM call or a retrieval query.
  confirm  -> he approves, and chooses whether an unsupported module (a
              gap) may be filled from labelled general knowledge. Approval
              builds the EXACT params shape `POST /curricula/generate`
              already builds (`routers/curriculum.py`) and hands it to a
              REAL `GenerationJob` — the existing enqueue/poll machinery,
              completely unmodified. No new async infrastructure is added
              anywhere by this feature.

VALIDATION: every step's answer either advances the step or returns an
error and LEAVES THE STEP UNCHANGED — a blank/garbled/invalid answer
re-asks the exact same question, never crashes, never silently advances
(brief, verbatim).
"""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.curriculum.generate import PLAN_SCHEMA, _build_plan_messages
from app.curriculum.ground import LEN_FLOOR, ground_topic
from app.llm.factory import get_provider
from app.models.interview import CurriculumInterview
from app.models.knowledge import KnowledgeSource
from app.models.student import Student

STEP_ORDER = ["who", "duration", "sources", "preview", "confirm"]


def start_interview(db: Session, *, title: str, domain: str | None = None) -> CurriculumInterview:
    """Create a fresh interview at its first step ("who") and commit it.

    `title`/`domain` are the one piece of context this interview does NOT
    ask about step-by-step — the brief's own five-step list has no
    dedicated "what is this course about" step. Asking it once, up front,
    the same way the tutor already names a course on the existing
    non-interview `POST /curricula/generate` form, is the smallest change
    that fits both that step list and the existing endpoint's request
    shape (`CurriculumGenerateRequest.title`/`.domain`) — this interview is
    a guided version of the SAME action, not a different one.
    """
    interview = CurriculumInterview(step="who", title=title, domain=domain, answers={})
    db.add(interview)
    db.commit()
    db.refresh(interview)
    return interview


def describe_step(db: Session, interview: CurriculumInterview) -> dict:
    """The `{question, options, findings}` for `interview`'s CURRENT step —
    used by every read path (the initial `POST /curricula/interview`, a
    non-final answer's response, and `GET .../{id}` for a refresh) so all
    three render an IDENTICAL envelope for a given step. Never mutates
    `interview` or touches the model.
    """
    step = interview.step

    if step == "who":
        students = db.scalars(select(Student).order_by(Student.name)).all()
        options = [
            {"value": str(s.id), "label": s.name + (f" ({s.level})" if s.level else "")}
            for s in students
        ]
        return {
            "question": (
                "Who is this curriculum for? Pick one of your students, "
                "or answer with a name for someone new."
            ),
            "options": options,
            "findings": None,
        }

    if step == "duration":
        return {
            "question": "How many weeks should this run, and how many minutes per session?",
            "options": None,
            "findings": None,
        }

    if step == "sources":
        sources = db.scalars(select(KnowledgeSource).order_by(KnowledgeSource.title)).all()
        options = [
            {
                "value": str(s.id),
                "label": s.title,
                "type": s.type,
                "char_count": s.char_count,
                # A suggestion only, never a silent exclusion — every
                # source is listed regardless of this flag; see the module
                # docstring on why the tutor, not this code, makes the real
                # call on his library's synthetic filler.
                "default_selected": (s.char_count or 0) >= LEN_FLOOR,
            }
            for s in sources
        ]
        return {
            "question": (
                "Which of your library sources should this draw on? "
                "(shown with type + character count so you can tell real "
                "material from filler — nothing is pre-excluded)"
            ),
            "options": options,
            "findings": None,
        }

    if step == "preview":
        return {
            "question": (
                "Here's the proposed outline and what your library "
                "actually supports for it. Proceed to confirm?"
            ),
            "options": None,
            "findings": interview.preview,
        }

    if step == "confirm":
        return {
            "question": (
                "Ready to generate? Approve to enqueue it, and say whether "
                "gap modules may be filled with labelled general knowledge."
            ),
            "options": None,
            "findings": interview.preview,
        }

    # step == "done"
    return {
        "question": "This curriculum has been enqueued for generation.",
        "options": None,
        "findings": interview.preview,
    }


def render_state(db: Session, interview: CurriculumInterview, *, error: str | None = None) -> dict:
    """The full `{interview_id, step, question, options, findings, error}`
    envelope every route returns (the brief's literal response shape, plus
    `error`). `error` is attached only by the caller when the answer just
    given was invalid and this is a re-ask.
    """
    info = describe_step(db, interview)
    return {"interview_id": interview.id, "step": interview.step, "error": error, **info}


def _reask(error: str) -> dict:
    return {"ok": False, "error": error, "done": False, "params": None}


def _ok(*, done: bool = False, params: dict | None = None) -> dict:
    return {"ok": True, "error": None, "done": done, "params": params}


def _parse_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _answer_who(db: Session, interview: CurriculumInterview, answer) -> dict:
    if not isinstance(answer, dict):
        return _reask("Tell me who this is for — pick a student or give a name.")

    student_id_raw = answer.get("student_id")
    if student_id_raw:
        student_id = _parse_uuid(student_id_raw)
        student = db.get(Student, student_id) if student_id is not None else None
        if student is None:
            return _reask("I don't recognize that student — pick one from the list.")
        who = {
            "student_id": str(student.id), "name": student.name,
            "level": student.level, "language": student.preferred_language,
        }
    else:
        name = answer.get("name")
        if not isinstance(name, str) or not name.strip():
            return _reask("Tell me who this is for — pick a student or give a name.")
        who = {
            "student_id": None, "name": name.strip(),
            "level": answer.get("level"), "language": answer.get("language") or "en",
        }

    interview.answers = {**interview.answers, "who": who}
    return _ok()


def _answer_duration(interview: CurriculumInterview, answer) -> dict:
    if not isinstance(answer, dict):
        return _reask("Give me weeks and minutes_per_session as numbers.")

    weeks, minutes = answer.get("weeks"), answer.get("minutes_per_session")
    # bool is an int subclass in Python — reject it explicitly so
    # {"weeks": true} doesn't sail through as weeks=1.
    if isinstance(weeks, bool) or isinstance(minutes, bool):
        return _reask("Give me weeks and minutes_per_session as numbers.")
    try:
        weeks_i, minutes_i = int(weeks), int(minutes)
    except (TypeError, ValueError):
        return _reask("Give me weeks and minutes_per_session as numbers.")
    if weeks_i <= 0 or minutes_i <= 0:
        return _reask("weeks and minutes_per_session must both be greater than 0.")

    interview.answers = {
        **interview.answers,
        "duration": {"weeks": weeks_i, "minutes_per_session": minutes_i},
    }
    return _ok()


def _answer_sources(db: Session, interview: CurriculumInterview, answer) -> dict:
    if not isinstance(answer, dict) or not isinstance(answer.get("source_ids"), list):
        return _reask(
            "Tell me which source_ids to draw on, as a list "
            "([] is a valid, deliberate choice to draw on none of them)."
        )

    parsed: list[uuid.UUID] = []
    for raw in answer["source_ids"]:
        pid = _parse_uuid(raw)
        if pid is None:
            return _reask(f"{raw!r} isn't a valid source id.")
        parsed.append(pid)

    if parsed:
        found = set(db.scalars(
            select(KnowledgeSource.id).where(KnowledgeSource.id.in_(parsed))
        ).all())
        missing = [str(p) for p in parsed if p not in found]
        if missing:
            return _reask(f"Unknown source id(s): {', '.join(missing)}.")

    interview.answers = {
        **interview.answers, "sources": {"source_ids": [str(p) for p in parsed]},
    }
    # The money step's data is computed HERE, the moment the sources are
    # locked in, and cached on the row — "preview" itself (describe_step)
    # never calls the model or the retriever, it only ever reads this back.
    interview.preview = _compute_preview(db, interview)
    return _ok()


def _profile_and_language(interview: CurriculumInterview) -> tuple[dict, str, int | None]:
    """The `profile`/`language`/`target_minutes_total` `generate_curriculum`
    kwargs, derived from the "who"/"duration" answers collected so far.
    Shared by `_compute_preview` (the plan call must use the SAME profile
    the real generation job will) and `_answer_confirm` (the job's actual
    params) so the preview the tutor reviewed and the job he approved are
    built from identical inputs.
    """
    who = interview.answers.get("who", {})
    duration = interview.answers.get("duration", {})
    profile = {
        "student_name": who.get("name"), "student_id": who.get("student_id"),
        "level": who.get("level"), "weeks": duration.get("weeks"),
        "minutes_per_session": duration.get("minutes_per_session"),
    }
    language = who.get("language") or "en"
    target_minutes_total = None
    if duration.get("weeks") and duration.get("minutes_per_session"):
        target_minutes_total = duration["weeks"] * duration["minutes_per_session"]
    return profile, language, target_minutes_total


def _compute_preview(db: Session, interview: CurriculumInterview) -> dict:
    """Phase-1-plan (reusing `generate.py`'s OWN schema/prompt-builder/
    provider call — not a second one invented for the interview), then a
    REAL per-module `ground_topic` retrieval — this is what makes the
    preview genuine rather than a stub: every module's `passages` below
    came from an actual `search()` over the tutor's chosen sources, and a
    module gets `"gap": True` precisely when that retrieval returned
    nothing above `app.curriculum.ground`'s relevance floor — the exact
    same floor `generate_curriculum` itself will apply when it actually
    drafts this module later.
    """
    profile, language, target_minutes_total = _profile_and_language(interview)
    source_ids_raw = interview.answers.get("sources", {}).get("source_ids") or []
    source_ids = [uuid.UUID(s) for s in source_ids_raw]

    plan_messages = _build_plan_messages(
        title=interview.title, language=language, profile=profile,
        domain=interview.domain, target_minutes_total=target_minutes_total,
    )
    plan = get_provider().guided_json(plan_messages, PLAN_SCHEMA)

    modules_out = []
    for module in plan.get("modules") or []:
        query = f"{module['title']} {module.get('objective', '')}"
        passages = ground_topic(db, query, source_ids=source_ids, k=5, domain=interview.domain)
        modules_out.append({
            "title": module["title"],
            "objective": module.get("objective", ""),
            "gap": not passages,
            "passages": [
                {
                    "source_title": p.source_title, "page_no": p.page_no,
                    "score": round(p.score, 3),
                }
                for p in passages
            ],
        })

    return {
        "course_title": plan.get("title", interview.title),
        "modules": modules_out,
        "gap_count": sum(1 for m in modules_out if m["gap"]),
    }


def _answer_preview(interview: CurriculumInterview, answer) -> dict:
    if not isinstance(answer, dict) or answer.get("proceed") is not True:
        return _reask('Reply with {"proceed": true} once you have reviewed the findings above.')
    return _ok()


def _answer_confirm(interview: CurriculumInterview, answer) -> dict:
    if not isinstance(answer, dict) or answer.get("approved") is not True:
        return _reask('Reply with {"approved": true} to enqueue generation.')

    allow_general = bool(answer.get("allow_general", False))
    profile, language, target_minutes_total = _profile_and_language(interview)
    source_ids = interview.answers.get("sources", {}).get("source_ids") or []

    # Byte-for-byte the same shape `generate_curriculum_endpoint` builds in
    # `routers/curriculum.py` — this is what "reuse the existing
    # GenerationJob + poll machinery" means concretely: the router creates
    # the row and schedules `run_curriculum_job` with THESE params, exactly
    # as it already does for the non-interview endpoint.
    params = {
        "title": interview.title, "language": language, "profile": profile,
        "domain": interview.domain, "target_minutes_total": target_minutes_total,
        "source_ids": source_ids, "allow_general": allow_general,
    }
    interview.answers = {
        **interview.answers, "confirm": {"approved": True, "allow_general": allow_general},
    }
    return _ok(done=True, params=params)


def answer_interview(db: Session, interview: CurriculumInterview, answer) -> dict:
    """Validate `answer` against `interview`'s CURRENT step and, on success,
    mutate `interview` (`answers`/`preview`/`step`) to the next step.
    Returns one of:

      {"ok": False, "error": "...", "done": False, "params": None}
        Validation failed. `interview.step` is UNCHANGED — the caller
        re-asks the SAME question (via `render_state(..., error=...)`).
        This function NEVER raises on a bad/blank/malformed answer.

      {"ok": True, "error": None, "done": False, "params": None}
        Advanced to the next (non-final) step.

      {"ok": True, "error": None, "done": True, "params": {...}}
        The "confirm" step was just approved. `params` is the exact kwargs
        `generate_curriculum(db, **params)` expects. This function does
        NOT create the `GenerationJob` row or schedule the runner itself —
        it has no `BackgroundTasks` to schedule with, and creating that
        row is the router's existing, already-tested responsibility
        (mirrors `generate_curriculum_endpoint` exactly).

    Does not commit — the caller commits once, after inspecting the result.
    """
    step = interview.step
    if step == "who":
        result = _answer_who(db, interview, answer)
    elif step == "duration":
        result = _answer_duration(interview, answer)
    elif step == "sources":
        result = _answer_sources(db, interview, answer)
    elif step == "preview":
        result = _answer_preview(interview, answer)
    elif step == "confirm":
        result = _answer_confirm(interview, answer)
    else:
        return _reask(f"This interview is already at its final step ({step!r}).")

    if result["ok"]:
        interview.step = "done" if result["done"] else STEP_ORDER[STEP_ORDER.index(step) + 1]

    return result
