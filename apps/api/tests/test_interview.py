"""The curriculum-authoring interview, v2 (Plan 13, Stage 6.8).

REWRITTEN. The old five-step machine (who / duration / sources / preview /
confirm) is gone, and so is the `domain` it carried.

Chris, on the student: "this has to be optional dude.. the student part here has
to be TOTALLY optional."
Chris, on domain: "is domain playing any role? is it used somewhere or only for
tagging?" — it was one line in one prompt plus a retrieval filter that could no
longer exclude anything. A free-text COURSE BRIEF replaces it.
Chris, on shape: "im making a curriculum with 20 weeks, and only 4 modules are
here." — the duration step now ECHOES the derived shape back at him.

The state machine still lives in CODE, never in the model's head: every advance
below is an assertion on `interview.step`, and every bad-answer test proves the
SAME step re-asks rather than crashing or silently moving on. The model is reached
in exactly one place — the outline call.
"""
import uuid

import pytest

import app.curriculum.corpus as corpus_mod
import app.curriculum.interview as interview_mod
import app.curriculum.outline as outline_mod
import app.routers.curriculum as curriculum_router
from app.curriculum.interview import (
    answer_interview,
    describe_step,
    generate_interview_outline,
    render_state,
    start_interview,
)
from app.models.block import Block
from app.models.generation_job import GenerationJob
from app.models.interview import CurriculumInterview
from app.models.knowledge import KnowledgeSource, Page
from app.models.note import Note
from app.models.student import Student


class _FakeProvider:
    def __init__(self, outline=None):
        self.outline = outline or _OUTLINE
        self.calls: list[dict] = []

    def guided_json(self, messages, schema, *, temperature=0.2, role="spec", max_tokens=None):
        self.calls.append({"messages": messages, "role": role})
        return self.outline

    def count_tokens(self, text: str) -> int:
        return len(text) // 3 + 1


_OUTLINE = {
    "title": "Tone Fundamentals",
    "modules": [
        {
            "title": f"Module {i}", "objective": "o", "tier": "library",
            "coverage_note": "p.19",
            "lessons": [
                {"title": f"L{i}.{j}", "objective": "o", "est_minutes": 50}
                for j in range(4)
            ],
        }
        for i in range(2)
    ],
}


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    provider = _FakeProvider()
    monkeypatch.setattr(interview_mod, "generate_outline", _passthrough_outline(provider))
    monkeypatch.setattr(outline_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)
    return provider


def _passthrough_outline(provider):
    """Keep the REAL `generate_outline` — shape enforcement and tier clamping are
    part of what these tests are asserting — but let it reach the fake model."""
    from app.curriculum.outline import generate_outline as real

    return real


def _source(db, *, title="Getting Great Guitar Sounds") -> KnowledgeSource:
    source = KnowledgeSource(type="pdf", title=title, language="en",
                             char_count=62585, status="ready")
    db.add(source)
    db.flush()
    db.add(Page(source_id=source.id, page_no=19,
                text="A humbucker cancels hum by pairing opposed coils. " * 4,
                status="ready"))
    db.commit()
    return source


def _start(db, title="Tone Fundamentals") -> CurriculumInterview:
    return start_interview(db, title=title)


def _walk_to(db, interview, step, *, source_ids=None):
    """Drive the machine up to (not through) `step`."""
    if interview.step == "who":
        answer_interview(db, interview, {"student_id": None, "level": "all_levels"})
    if step == "duration":
        return
    if interview.step == "duration":
        answer_interview(db, interview, {"weeks": 8, "sessions_per_week": 1,
                                         "minutes_per_session": 50})
    if step == "scope":
        return
    if interview.step == "scope":
        answer_interview(db, interview, {"brief": "COURSE_BRIEF_MARKER",
                                         "gap_policy": "general_knowledge"})
    if step == "sources":
        return
    if interview.step == "sources":
        answer_interview(db, interview, {"source_ids": source_ids or []})
    if step == "outline":
        return
    if interview.step == "outline":
        r = answer_interview(db, interview, {"regenerate": True})
        # Regenerate now hands off to a background job (see run_outline_job) instead
        # of generating inline; do the job's work here so the walk still produces a
        # real outline to accept.
        if r.get("generate_outline"):
            interview.outline = generate_interview_outline(db, interview)
        answer_interview(db, interview, {"outline": interview.outline})


# ---------------------------------------------------------------------------
# The whole machine
# ---------------------------------------------------------------------------

def test_the_state_machine_advances_deterministically_through_every_step(db):
    source = _source(db)
    interview = _start(db)
    assert interview.step == "who"

    r = answer_interview(db, interview, {"student_id": None, "level": "beginner"})
    assert r["ok"] and not r["done"]
    assert interview.step == "duration"

    r = answer_interview(db, interview, {"weeks": 20, "sessions_per_week": 1,
                                         "minutes_per_session": 50})
    assert r["ok"]
    assert interview.step == "scope"

    r = answer_interview(db, interview, {"brief": "get him playing blues",
                                         "gap_policy": "general_knowledge"})
    assert r["ok"]
    assert interview.step == "sources"
    assert interview.brief == "get him playing blues"

    r = answer_interview(db, interview, {"source_ids": [str(source.id)]})
    assert r["ok"]
    assert interview.step == "outline"

    r = answer_interview(db, interview, {"regenerate": True})
    assert r["ok"] and r.get("stay") is True
    assert r.get("generate_outline") is True, "the outline is generated OFF the request now"
    assert interview.step == "outline", "generating the outline keeps him ON the step"
    assert interview.outline is None, "the service no longer generates synchronously — the job does"

    # Simulate the background job the router would have scheduled (run_outline_job).
    interview.outline = generate_interview_outline(db, interview)
    assert interview.outline is not None

    r = answer_interview(db, interview, {"outline": interview.outline})
    assert r["ok"]
    assert interview.step == "confirm"

    r = answer_interview(db, interview, {"approved": True})
    assert r["ok"] and r["done"]
    assert interview.step == "done"
    assert interview.root_id is not None, "confirm MATERIALIZES the tree"


# ---------------------------------------------------------------------------
# "who" — the student is TOTALLY optional
# ---------------------------------------------------------------------------

def test_no_student_is_a_first_class_answer_not_a_blank_field(db):
    """Chris, verbatim: "this has to be optional dude.. the student part here has to
    be TOTALLY optional"."""
    interview = _start(db)

    r = answer_interview(db, interview, {"student_id": None, "level": "all_levels"})

    assert r["ok"]
    assert interview.step == "duration"
    assert interview.answers["who"] == {
        "student_id": None, "name": None, "level": "all_levels", "language": "el",
    }


def test_the_who_step_offers_no_student_as_an_explicit_option_with_a_level_selector(db):
    db.add(Student(name="Elena", level="intermediate"))
    db.commit()

    state = render_state(db, _start(db))

    assert state["step"] == "who"
    assert {"value": "none", "label": "No particular student — a course for anyone",
            "kind": "none"} in state["options"]
    assert any(o["label"] == "Elena (intermediate)" for o in state["options"])
    assert "all_levels" in state["findings"]["levels"]


def test_picking_a_real_student_inherits_his_level_and_his_language(db):
    student = Student(name="Nikos", level="beginner", preferred_language="el")
    db.add(student)
    db.commit()

    interview = _start(db)
    answer_interview(db, interview, {"student_id": str(student.id)})

    who = interview.answers["who"]
    assert who["student_id"] == str(student.id)
    assert who["name"] == "Nikos"
    assert who["level"] == "beginner"
    assert who["language"] == "el"


def test_an_unknown_student_id_reasks(db):
    interview = _start(db)
    r = answer_interview(db, interview, {"student_id": str(uuid.uuid4())})
    assert r["ok"] is False
    assert interview.step == "who"


def test_an_unknown_level_reasks(db):
    interview = _start(db)
    r = answer_interview(db, interview, {"student_id": None, "level": "wizard"})
    assert r["ok"] is False
    assert interview.step == "who"


def test_who_step_with_garbage_answer_types_does_not_crash(db):
    interview = _start(db)
    for garbage in [None, "just a string", 42, ["a", "list"]]:
        r = answer_interview(db, interview, garbage)
        assert r["ok"] is False
        assert interview.step == "who"


# ---------------------------------------------------------------------------
# "duration" — the step that makes "20 weeks, 4 modules" impossible
# ---------------------------------------------------------------------------

def test_the_sources_step_echoes_the_derived_shape_back_at_him(db):
    """He agrees to a SIZE before we spend his money on it.

    Chris, verbatim: "im making a curriculum with 20 weeks, and only 4 modules
    are here. are those enough? i dont want us to be frugal here." 20 weeks now
    yields FIVE modules of four lessons, and he sees that before we spend
    anything.

    The wire carries NUMBERS, not a formatted sentence. It used to send
    `Shape.describe()` — an English string — which the (fully Greek) interview
    rendered verbatim, so a Greek tutor on a Greek page read "20 sessions -> 5
    modules". The API does not know his locale; the frontend does. Anything the
    server formats for a human is a Greek bug waiting to happen, so this test
    asserts on the counts and `interview-sources-step.tsx` owns the sentence.
    """
    interview = _start(db)
    answer_interview(db, interview, {"student_id": None})
    answer_interview(db, interview, {"weeks": 20, "sessions_per_week": 1,
                                     "minutes_per_session": 50})

    assert interview.step == "scope"
    _walk_to(db, interview, "sources")
    shape = describe_step(db, interview)["findings"]["shape"]

    assert shape["lessons_total"] == 20
    assert shape["modules"] == 5                      # never 4 again
    assert shape["lessons_per_module"] == [4, 4, 4, 4, 4]
    assert shape["target_words_per_lesson"] == 2200   # 40 taught min x 55 wpm
    assert shape["teaching_minutes"] == 40
    assert shape["qa_minutes"] == 10                  # the 10' he asked for

    # ...and nothing user-facing crosses the wire in English.
    assert not any(isinstance(v, str) for v in shape.values())


@pytest.mark.parametrize("bad", [
    {},
    {"weeks": 0, "minutes_per_session": 45},
    {"weeks": 8, "minutes_per_session": -1},
    {"weeks": "eight", "minutes_per_session": 45},
    {"weeks": True, "minutes_per_session": 45},          # bool is an int subclass
    {"weeks": 8, "sessions_per_week": 0, "minutes_per_session": 45},
])
def test_duration_step_rejects_a_nonsense_course(db, bad):
    interview = _start(db)
    answer_interview(db, interview, {"student_id": None})
    assert interview.step == "duration"

    r = answer_interview(db, interview, bad)

    assert r["ok"] is False, f"expected a re-ask for {bad!r}"
    assert interview.step == "duration"


# ---------------------------------------------------------------------------
# "scope" — where `domain` died
# ---------------------------------------------------------------------------

def test_the_scope_step_takes_a_free_text_brief_and_a_gap_policy(db):
    interview = _start(db)
    _walk_to(db, interview, "scope")

    r = answer_interview(db, interview, {
        "brief": "He wants to play 12-bar blues at his sister's wedding.",
        "gap_policy": "library_only",
    })

    assert r["ok"]
    assert interview.step == "sources"
    assert interview.brief == "He wants to play 12-bar blues at his sister's wedding."
    assert interview.gap_policy == "library_only"


def test_an_empty_brief_reasks_because_it_is_what_the_course_gets_written_from(db):
    interview = _start(db)
    _walk_to(db, interview, "scope")

    for bad in [{}, {"brief": ""}, {"brief": "   "}, {"brief": 42}, None]:
        r = answer_interview(db, interview, bad)
        assert r["ok"] is False
        assert interview.step == "scope"


def test_an_unknown_gap_policy_reasks(db):
    interview = _start(db)
    _walk_to(db, interview, "scope")

    r = answer_interview(db, interview, {"brief": "b", "gap_policy": "vibes"})

    assert r["ok"] is False
    assert interview.step == "scope"


def test_the_interview_no_longer_has_a_domain_anywhere(db):
    """`domain` is DEAD. It was one dead prompt line and a filter that couldn't
    filter."""
    interview = _start(db)
    assert not hasattr(interview, "domain")
    with pytest.raises(TypeError):
        start_interview(db, title="X", domain="tone")


# ---------------------------------------------------------------------------
# "sources" — measured, not guessed
# ---------------------------------------------------------------------------

def test_the_sources_step_measures_the_selection_and_tells_him_if_it_fits(db):
    """"3 sources · 92,400 tokens · fits whole" — because we MEASURE it with
    count_tokens, which is free and exact, rather than hoping."""
    source = _source(db)
    interview = _start(db)
    _walk_to(db, interview, "sources")

    r = answer_interview(db, interview, {"source_ids": [str(source.id)]})

    assert r["ok"]
    measured = interview.answers["sources"]["measured"]
    assert measured["token_count"] > 0
    assert measured["fits"] is True
    assert "fits whole" in measured["summary"]


def test_the_sources_step_lists_every_source_and_pre_selects_only_substantive_ones(db):
    _source(db, title="A Real Book")
    tiny = KnowledgeSource(type="text", title="A Stub", language="en", char_count=12)
    db.add(tiny)
    db.commit()

    interview = _start(db)
    _walk_to(db, interview, "sources")
    state = describe_step(db, interview)

    labels = {o["label"]: o["default_selected"] for o in state["options"]}
    assert labels["A Real Book"] is True
    assert labels["A Stub"] is False, "a suggestion — but it is still LISTED, never hidden"


@pytest.mark.parametrize("bad", [
    {}, {"source_ids": "not-a-list"}, {"source_ids": ["not-a-uuid"]},
])
def test_sources_step_rejects_a_malformed_selection(db, bad):
    interview = _start(db)
    _walk_to(db, interview, "sources")

    r = answer_interview(db, interview, bad)

    assert r["ok"] is False
    assert interview.step == "sources"


def test_sources_step_rejects_a_source_that_does_not_exist(db):
    interview = _start(db)
    _walk_to(db, interview, "sources")

    r = answer_interview(db, interview, {"source_ids": [str(uuid.uuid4())]})

    assert r["ok"] is False
    assert interview.step == "sources"


def test_an_explicit_empty_list_is_a_deliberate_choice_and_is_accepted(db):
    interview = _start(db)
    _walk_to(db, interview, "sources")

    r = answer_interview(db, interview, {"source_ids": []})

    assert r["ok"]
    assert interview.step == "outline"
    assert interview.answers["sources"]["source_ids"] == []


# ---------------------------------------------------------------------------
# "outline" — the step Chris said was missing: he EDITS it
# ---------------------------------------------------------------------------

def test_the_outline_is_generated_from_the_whole_library_and_the_brief(db, _provider):
    source = _source(db)
    interview = _start(db)
    _walk_to(db, interview, "outline", source_ids=[str(source.id)])

    # The regenerate answer only SIGNALS now; the generation itself is what
    # run_outline_job calls off the request path.
    r = answer_interview(db, interview, {"regenerate": True})
    assert r.get("generate_outline") is True
    interview.outline = generate_interview_outline(db, interview)

    assert len(_provider.calls) == 1
    sent = " ".join(m["content"] for m in _provider.calls[0]["messages"])
    assert "COURSE_BRIEF_MARKER" in sent
    assert "humbucker" in sent, "the WHOLE library reaches the outline call"
    assert interview.outline["modules"]


def test_THE_EDITED_OUTLINE_IS_THE_ONE_THAT_GETS_BUILT_not_the_models_original(db):
    """The whole point of the step. Chris's complaint was that the app gave him
    something to accept or reject, not something to work on."""
    source = _source(db)
    interview = _start(db)
    _walk_to(db, interview, "outline", source_ids=[str(source.id)])
    answer_interview(db, interview, {"regenerate": True})
    interview.outline = generate_interview_outline(db, interview)

    edited = {
        "title": "MY OWN TITLE",
        "modules": [{
            "title": "MY OWN MODULE", "objective": "mine", "tier": "library",
            "coverage_note": "", "lessons": [
                {"title": "MY OWN LESSON", "objective": "mine", "est_minutes": 50},
            ],
        }],
    }
    answer_interview(db, interview, {"outline": edited})
    assert interview.step == "confirm"

    answer_interview(db, interview, {"approved": True})

    course = db.get(Block, interview.root_id)
    assert course.title == "MY OWN TITLE"
    modules = [c for c in course.children if c.kind == "module"]
    assert [m.title for m in modules] == ["MY OWN MODULE"]
    lessons = [c for c in modules[0].children if c.kind == "lesson"]
    assert [l.title for l in lessons] == ["MY OWN LESSON"]


def test_a_refresh_never_re_runs_the_expensive_outline_call(db, _provider):
    source = _source(db)
    interview = _start(db)
    _walk_to(db, interview, "outline", source_ids=[str(source.id)])
    answer_interview(db, interview, {"regenerate": True})
    interview.outline = generate_interview_outline(db, interview)
    assert len(_provider.calls) == 1

    state = render_state(db, interview)

    assert state["step"] == "outline"
    assert state["findings"] == interview.outline
    assert len(_provider.calls) == 1, "a refresh must not re-run a 90K-token call"


# ---------------------------------------------------------------------------
# The outline call is ASYNC now — it outlives Cloudflare's ~100s edge cap, so it
# runs off the request path as a GenerationJob (Plan 13 follow-up). These pin the
# job runner and the endpoint's 202, the same way the draft job's own tests do.
# ---------------------------------------------------------------------------

def test_run_outline_job_generates_the_outline_and_marks_the_job_succeeded(db, _provider):
    from app.jobs.runner import run_outline_job

    source = _source(db)
    interview = _start(db)
    _walk_to(db, interview, "outline", source_ids=[str(source.id)])
    assert interview.step == "outline" and interview.outline is None
    job = GenerationJob(
        kind="curriculum_outline",
        status="pending",
        params={"interview_id": str(interview.id)},
    )
    db.add(job)
    # run_outline_job opens its OWN SessionLocal — the interview and the source must
    # be COMMITTED for it to see them, exactly like the draft-job tests.
    db.commit()

    run_outline_job(job.id)

    db.expire_all()
    job = db.get(GenerationJob, job.id)
    interview = db.get(CurriculumInterview, interview.id)
    assert job.status == "succeeded"
    assert job.error is None
    assert interview.outline is not None and interview.outline["modules"], (
        "the job writes the outline back onto the interview row"
    )


def test_a_failing_outline_job_records_the_reason_and_leaves_the_outline_empty(db, monkeypatch):
    from app.jobs.runner import run_outline_job
    from app.llm.errors import LLMNotConfigured

    def _boom(*a, **k):
        raise LLMNotConfigured("no key")

    monkeypatch.setattr("app.jobs.runner.generate_interview_outline", _boom)

    source = _source(db)
    interview = _start(db)
    _walk_to(db, interview, "outline", source_ids=[str(source.id)])
    job = GenerationJob(
        kind="curriculum_outline", status="pending",
        params={"interview_id": str(interview.id)},
    )
    db.add(job)
    db.commit()

    run_outline_job(job.id)

    db.expire_all()
    job = db.get(GenerationJob, job.id)
    interview = db.get(CurriculumInterview, interview.id)
    assert job.status == "failed"
    assert job.error_kind == "auth", "a missing key is a fix-it-in-Settings error, not 'our bug'"
    assert interview.outline is None, "a failed generation must not half-write the outline"


def test_the_outline_answer_endpoint_returns_202_without_a_root_id_and_schedules_the_job(
    db, client, monkeypatch
):
    """The 202 the FRONTEND must tell apart from confirm's: no `root_id` means
    'nothing is materialized — poll the job, then re-fetch the outline', NOT 'the
    tree exists, open the board'."""
    from app.llm.factory import require_llm_configured

    scheduled: list = []
    monkeypatch.setattr(curriculum_router, "run_outline_job", lambda job_id: scheduled.append(job_id))
    client.app.dependency_overrides[require_llm_configured] = lambda: None
    try:
        source = _source(db)
        interview = _start(db)
        _walk_to(db, interview, "outline", source_ids=[str(source.id)])
        db.commit()

        resp = client.post(
            f"/curricula/interview/{interview.id}/answer",
            json={"answer": {"regenerate": True}},
        )
    finally:
        client.app.dependency_overrides.clear()

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert body.get("root_id") is None, "an outline job must NOT carry a root_id"
    assert len(scheduled) == 1, "the background job was scheduled"

    db.expire_all()
    job = db.get(GenerationJob, uuid.UUID(body["job_id"]))
    assert job is not None and job.kind == "curriculum_outline"
    assert job.params["interview_id"] == str(interview.id)
    interview = db.get(CurriculumInterview, interview.id)
    assert str(interview.job_id) == body["job_id"], (
        "the outline job id is parked on the interview so a refresh can resume the poll"
    )


def test_an_outline_with_no_modules_is_rejected_rather_than_confirmed(db):
    source = _source(db)
    interview = _start(db)
    _walk_to(db, interview, "outline", source_ids=[str(source.id)])
    answer_interview(db, interview, {"regenerate": True})

    r = answer_interview(db, interview, {"outline": {"title": "X", "modules": []}})

    assert r["ok"] is False
    assert interview.step == "outline"


# ---------------------------------------------------------------------------
# "confirm" — materialize, then enqueue
# ---------------------------------------------------------------------------

def test_confirm_without_approval_reasks_and_materializes_nothing(db):
    source = _source(db)
    interview = _start(db)
    _walk_to(db, interview, "confirm", source_ids=[str(source.id)])
    assert interview.step == "confirm"

    for bad in [{}, {"approved": False}, {"approved": "true"}, None]:
        r = answer_interview(db, interview, bad)
        assert r["ok"] is False
        assert r["done"] is False
        assert interview.step == "confirm"
        assert interview.root_id is None


def test_answering_a_finished_interview_reasks_without_crashing(db):
    source = _source(db)
    interview = _start(db)
    _walk_to(db, interview, "confirm", source_ids=[str(source.id)])
    answer_interview(db, interview, {"approved": True})
    assert interview.step == "done"

    r = answer_interview(db, interview, {"approved": True})

    assert r["ok"] is False
    assert interview.step == "done"


def test_the_student_reaches_the_course_meta_so_every_lesson_draft_can_see_him(db):
    """The bug this fixes: `level`/`name` used to appear in the OUTLINE prompt and
    NOWHERE ELSE, so every lesson body ever generated by this app was written with
    zero knowledge of the learner."""
    student = Student(name="Nikos", level="beginner")
    db.add(student)
    db.add(Note(title="Barre chords", body="He cannot barre yet.", tags=["struggle"],
                student_id=student.id))
    db.commit()
    source = _source(db)

    interview = _start(db)
    answer_interview(db, interview, {"student_id": str(student.id)})
    answer_interview(db, interview, {"weeks": 8, "sessions_per_week": 1,
                                     "minutes_per_session": 50})
    answer_interview(db, interview, {"brief": "b", "gap_policy": "general_knowledge"})
    answer_interview(db, interview, {"source_ids": [str(source.id)]})
    answer_interview(db, interview, {"regenerate": True})
    interview.outline = generate_interview_outline(db, interview)  # the async job's work
    answer_interview(db, interview, {"outline": interview.outline})
    answer_interview(db, interview, {"approved": True})

    course = db.get(Block, interview.root_id)
    assert course.meta["student_id"] == str(student.id)


# ---------------------------------------------------------------------------
# The HTTP routes
# ---------------------------------------------------------------------------

def test_post_curricula_interview_starts_at_who(client, db):
    db.add(Student(name="Maria", level="advanced"))
    db.commit()

    r = client.post("/curricula/interview", json={"title": "Tone Fundamentals"})

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["step"] == "who"
    assert any(o["label"] == "Maria (advanced)" for o in body["options"])

    r2 = client.get(f"/curricula/interview/{body['interview_id']}")
    assert r2.status_code == 200
    assert r2.json()["step"] == "who"


def test_get_unknown_interview_404s(client):
    r = client.get(f"/curricula/interview/{uuid.uuid4()}")
    assert r.status_code == 404


def test_answer_route_reasks_on_a_bad_answer_without_advancing(client):
    r = client.post("/curricula/interview", json={"title": "Tone"})
    interview_id = r.json()["interview_id"]

    r2 = client.post(f"/curricula/interview/{interview_id}/answer",
                     json={"answer": {"level": "wizard"}})

    assert r2.status_code == 200, r2.text
    assert r2.json()["step"] == "who"
    assert r2.json()["error"]


def test_confirm_returns_202_with_BOTH_a_job_id_and_a_root_id(client, db, monkeypatch, _provider):
    """The tree already EXISTS by the time the 202 lands — so the board opens
    instantly on a real curriculum with a progress bar, instead of on a spinner
    waiting for a job that will not produce anything to look at for four minutes.

    `_provider` is here because `post({"regenerate": True})` now schedules the REAL
    `run_outline_job` as a BackgroundTask (TestClient runs it before the POST
    returns), so the outline is genuinely generated — with the fake provider — and
    the follow-up GET sees real findings to accept."""
    scheduled = []
    monkeypatch.setattr(curriculum_router, "run_curriculum_draft_job", scheduled.append)
    source = _source(db)

    r = client.post("/curricula/interview", json={"title": "Tone Fundamentals"})
    iid = r.json()["interview_id"]
    post = lambda answer: client.post(  # noqa: E731
        f"/curricula/interview/{iid}/answer", json={"answer": answer})

    post({"student_id": None, "level": "all_levels"})
    post({"weeks": 8, "sessions_per_week": 1, "minutes_per_session": 50})
    post({"brief": "get him playing blues", "gap_policy": "general_knowledge"})
    post({"source_ids": [str(source.id)]})
    outline_202 = post({"regenerate": True})
    assert outline_202.status_code == 202, outline_202.text
    assert outline_202.json().get("root_id") is None, "the outline 202 must not carry a root_id"
    state = client.get(f"/curricula/interview/{iid}").json()
    assert state["findings"] and state["findings"]["modules"], "the outline job populated it"
    post({"outline": state["findings"]})

    r = post({"approved": True})

    assert r.status_code == 202, r.text
    body = r.json()
    job_id = uuid.UUID(body["job_id"])
    root_id = uuid.UUID(body["root_id"])
    assert scheduled == [job_id]

    job = db.get(GenerationJob, job_id)
    assert job.kind == "curriculum_draft"
    assert job.params["root_id"] == str(root_id)

    # And the board can already be opened on it.
    tree = client.get(f"/curricula/{root_id}")
    assert tree.status_code == 200, tree.text
    assert tree.json()["children"], "the tree is materialized BEFORE the draft runs"

    db.expire_all()
    interview = db.get(CurriculumInterview, uuid.UUID(iid))
    assert interview.step == "done"
    assert interview.job_id == job_id
    assert interview.root_id == root_id
