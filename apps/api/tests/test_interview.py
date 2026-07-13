"""Tests for the guided curriculum-authoring interview (Plan 12 Task 3, G2):
`app.curriculum.interview`'s state machine, and the three
`/curricula/interview...` routes in `app.routers.curriculum`.

Chris: "maybe llm can act as an assistant there bro, guiding him, and asking
him questions or corrections throughout the process." This proves the
state machine actually lives in CODE (never the model): every advance is a
plain assertion on `interview.step`/`.answers`, and every "bad answer"
test proves the SAME step re-asks rather than crashing or silently moving
on — the model is only ever reached via the monkeypatched `get_provider`
(the plan call) and `ground_topic` (the real retrieval, exercised for
real in the tests that matter most, see below).

`get_provider`/`ground_topic` are patched on `app.curriculum.interview`'s
OWN namespace (`interview_mod.get_provider`/`interview_mod.ground_topic`),
NOT their origin modules (`app.llm.factory`/`app.curriculum.ground`) —
`from ... import X` binds a new name into the importing module's namespace,
so patching the origin wouldn't affect interview.py's already-bound
reference (mirrors `test_curriculum_grounding.py`'s identical reasoning for
`generate_mod.get_provider`).

KEY TEST GOTCHA (brief, verbatim): Starlette's `TestClient` runs
`BackgroundTasks` AFTER the response, in-process — the "confirm" step
enqueues a REAL `GenerationJob` and schedules `run_curriculum_job`, so every
test that reaches "confirm" monkeypatches `app.routers.curriculum.
run_curriculum_job` to a capturing no-op (mirrors
`test_curriculum_generate_enqueue.py` exactly), or a real ~90s LLM
generation would fire during the test run.
"""
import uuid

import pytest
from sqlalchemy import select

import app.curriculum.interview as interview_mod
import app.routers.curriculum as curriculum_router
from app.curriculum.interview import (
    answer_interview,
    describe_step,
    render_state,
    start_interview,
)
from app.models.generation_job import GenerationJob
from app.models.interview import CurriculumInterview
from app.models.knowledge import KnowledgeSource
from app.models.student import Student

_PLAN_ONE_MODULE = {
    "title": "Tone Fundamentals",
    "modules": [{"title": "Pickups and Tone", "objective": "Understand pickup types."}],
}

_PLAN_TWO_MODULES = {
    "title": "Tone Fundamentals",
    "modules": [
        {"title": "Pickups and Tone", "objective": "Understand pickup types."},
        {"title": "Amp Gain Staging", "objective": "Understand amp gain."},
    ],
}


class _FakePlanProvider:
    """Only ever asked for the Phase-1 plan (`_compute_preview` never drafts
    module content — that's `generate_curriculum`'s own job, later, once
    the real job runs) — a single scripted response is enough.
    """

    def __init__(self, plan):
        self.plan = plan
        self.calls: list[dict] = []

    def guided_json(self, messages, schema, *, temperature: float = 0.2):
        self.calls.append({"messages": messages, "schema": schema})
        return self.plan


def _passage(*, source_title="Getting Great Guitar Sounds", page_no=56, score=0.7):
    from app.curriculum.ground import Passage
    return Passage(
        text="UNIQUE_PASSAGE_MARKER: a real, substantive passage." * 3,
        source_id=uuid.uuid4(), source_title=source_title, page_no=page_no,
        page_id=uuid.uuid4(), score=score,
    )


def _make_source(db, *, title="Getting Great Guitar Sounds", char_count=62585, type_="pdf") -> KnowledgeSource:
    source = KnowledgeSource(type=type_, title=title, language="en", char_count=char_count)
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _start(db, *, title="Tone Fundamentals") -> CurriculumInterview:
    return start_interview(db, title=title, domain=None)


# ---------------------------------------------------------------------------
# The state machine advances deterministically through every step.
# ---------------------------------------------------------------------------

def test_the_state_machine_advances_deterministically_through_every_step(db, monkeypatch):
    monkeypatch.setattr(interview_mod, "get_provider", lambda: _FakePlanProvider(_PLAN_ONE_MODULE))
    monkeypatch.setattr(interview_mod, "ground_topic", lambda *a, **k: [_passage()])

    student = Student(name="Nikos", level="beginner")
    db.add(student); db.commit()
    source = _make_source(db)

    interview = _start(db)
    assert interview.step == "who"

    r = answer_interview(db, interview, {"student_id": str(student.id)})
    assert r == {"ok": True, "error": None, "done": False, "params": None}
    assert interview.step == "duration"
    assert interview.answers["who"]["name"] == "Nikos"

    r = answer_interview(db, interview, {"weeks": 8, "minutes_per_session": 45})
    assert r["ok"] and not r["done"]
    assert interview.step == "sources"
    assert interview.answers["duration"] == {"weeks": 8, "minutes_per_session": 45}

    r = answer_interview(db, interview, {"source_ids": [str(source.id)]})
    assert r["ok"] and not r["done"]
    assert interview.step == "preview"
    assert interview.preview is not None, "sources->preview must compute the preview"

    r = answer_interview(db, interview, {"proceed": True})
    assert r["ok"] and not r["done"]
    assert interview.step == "confirm"

    r = answer_interview(db, interview, {"approved": True, "allow_general": False})
    assert r["ok"] and r["done"]
    assert interview.step == "done"
    assert r["params"] == {
        "title": "Tone Fundamentals", "language": "el", "domain": None,
        "target_minutes_total": 8 * 45,
        "profile": {
            "student_name": "Nikos", "student_id": str(student.id), "level": "beginner",
            "weeks": 8, "minutes_per_session": 45,
        },
        "source_ids": [str(source.id)], "allow_general": False,
    }


# ---------------------------------------------------------------------------
# "who": real students offered as options; free-text name also accepted.
# ---------------------------------------------------------------------------

def test_who_step_offers_his_real_students_as_options(db):
    student = Student(name="Elena", level="intermediate")
    db.add(student); db.commit()

    interview = _start(db)
    state = render_state(db, interview)

    assert state["step"] == "who"
    assert {"value": str(student.id), "label": "Elena (intermediate)"} in state["options"]


def test_who_step_accepts_a_free_text_name_for_someone_new(db):
    interview = _start(db)
    r = answer_interview(db, interview, {"name": "Someone New", "level": "beginner"})
    assert r["ok"]
    assert interview.answers["who"] == {
        "student_id": None, "name": "Someone New", "level": "beginner", "language": "en",
    }


# ---------------------------------------------------------------------------
# Validation — a bad/blank answer RE-ASKS, never crashes, never advances.
# ---------------------------------------------------------------------------

def test_a_blank_who_answer_reasks_and_does_not_advance(db):
    interview = _start(db)
    r = answer_interview(db, interview, {})
    assert r["ok"] is False
    assert r["error"]
    assert interview.step == "who", "step must not advance on a blank answer"


def test_who_step_with_garbage_answer_types_does_not_crash(db):
    interview = _start(db)
    for garbage in [None, "just a string", 42, ["a", "list"]]:
        r = answer_interview(db, interview, garbage)
        assert r["ok"] is False
        assert interview.step == "who"


def test_an_unknown_student_id_reasks(db):
    interview = _start(db)
    r = answer_interview(db, interview, {"student_id": str(uuid.uuid4())})
    assert r["ok"] is False
    assert interview.step == "who"


def test_duration_step_rejects_non_positive_or_missing_values(db):
    interview = _start(db)
    answer_interview(db, interview, {"name": "X"})
    assert interview.step == "duration"

    for bad in [{}, {"weeks": 0, "minutes_per_session": 45}, {"weeks": 8, "minutes_per_session": -1},
                {"weeks": "eight", "minutes_per_session": 45}, {"weeks": True, "minutes_per_session": 45}]:
        r = answer_interview(db, interview, bad)
        assert r["ok"] is False, f"expected reask for {bad!r}"
        assert interview.step == "duration"

    r = answer_interview(db, interview, {"weeks": 6, "minutes_per_session": 30})
    assert r["ok"]
    assert interview.step == "sources"


def test_sources_step_rejects_an_unknown_or_malformed_source_id(db):
    interview = _start(db)
    answer_interview(db, interview, {"name": "X"})
    answer_interview(db, interview, {"weeks": 4, "minutes_per_session": 30})
    assert interview.step == "sources"

    for bad in [{}, {"source_ids": "not-a-list"}, {"source_ids": ["not-a-uuid"]},
                {"source_ids": [str(uuid.uuid4())]}]:
        r = answer_interview(db, interview, bad)
        assert r["ok"] is False, f"expected reask for {bad!r}"
        assert interview.step == "sources"


def test_sources_step_accepts_an_explicit_empty_list_as_a_deliberate_choice(db, monkeypatch):
    monkeypatch.setattr(interview_mod, "get_provider", lambda: _FakePlanProvider(_PLAN_ONE_MODULE))
    monkeypatch.setattr(interview_mod, "ground_topic", lambda *a, **k: [])

    interview = _start(db)
    answer_interview(db, interview, {"name": "X"})
    answer_interview(db, interview, {"weeks": 4, "minutes_per_session": 30})
    r = answer_interview(db, interview, {"source_ids": []})
    assert r["ok"]
    assert interview.step == "preview"
    assert interview.answers["sources"] == {"source_ids": []}


def test_preview_step_rejects_anything_but_an_explicit_proceed(db, monkeypatch):
    monkeypatch.setattr(interview_mod, "get_provider", lambda: _FakePlanProvider(_PLAN_ONE_MODULE))
    monkeypatch.setattr(interview_mod, "ground_topic", lambda *a, **k: [_passage()])

    interview = _start(db)
    answer_interview(db, interview, {"name": "X"})
    answer_interview(db, interview, {"weeks": 4, "minutes_per_session": 30})
    answer_interview(db, interview, {"source_ids": []})
    assert interview.step == "preview"

    for bad in [{}, {"proceed": False}, {"proceed": "yes"}, None]:
        r = answer_interview(db, interview, bad)
        assert r["ok"] is False
        assert interview.step == "preview"

    r = answer_interview(db, interview, {"proceed": True})
    assert r["ok"]
    assert interview.step == "confirm"


def test_confirm_without_approval_reasks_and_does_not_enqueue(db, monkeypatch):
    monkeypatch.setattr(interview_mod, "get_provider", lambda: _FakePlanProvider(_PLAN_ONE_MODULE))
    monkeypatch.setattr(interview_mod, "ground_topic", lambda *a, **k: [_passage()])

    interview = _start(db)
    answer_interview(db, interview, {"name": "X"})
    answer_interview(db, interview, {"weeks": 4, "minutes_per_session": 30})
    answer_interview(db, interview, {"source_ids": []})
    answer_interview(db, interview, {"proceed": True})
    assert interview.step == "confirm"

    for bad in [{}, {"approved": False}, {"approved": "true"}, None]:
        r = answer_interview(db, interview, bad)
        assert r["ok"] is False
        assert r["done"] is False
        assert interview.step == "confirm"


def test_answering_an_already_finished_interview_reasks_without_crashing(db, monkeypatch):
    monkeypatch.setattr(interview_mod, "get_provider", lambda: _FakePlanProvider(_PLAN_ONE_MODULE))
    monkeypatch.setattr(interview_mod, "ground_topic", lambda *a, **k: [_passage()])

    interview = _start(db)
    answer_interview(db, interview, {"name": "X"})
    answer_interview(db, interview, {"weeks": 4, "minutes_per_session": 30})
    answer_interview(db, interview, {"source_ids": []})
    answer_interview(db, interview, {"proceed": True})
    r = answer_interview(db, interview, {"approved": True})
    assert r["done"]
    assert interview.step == "done"

    r2 = answer_interview(db, interview, {"approved": True})
    assert r2["ok"] is False
    assert interview.step == "done"


# ---------------------------------------------------------------------------
# THE step that matters: preview really calls ground_topic and returns REAL
# passages with source+page; a module with nothing above the floor is a GAP.
# ---------------------------------------------------------------------------

def test_preview_step_really_calls_ground_topic_and_returns_real_passages_with_source_and_page(
    db, monkeypatch,
):
    from app.brain.ingest import IngestPayload, ingest_source

    monkeypatch.setattr(interview_mod, "get_provider", lambda: _FakePlanProvider(_PLAN_ONE_MODULE))

    source = KnowledgeSource(type="text", title="Getting Great Guitar Sounds", language="en")
    db.add(source); db.commit()
    ingest_source(db, source.id, IngestPayload(
        kind="text",
        text=(
            "Single-coil pickups sound bright and glassy, while humbuckers "
            "sound thicker and warmer because they cancel hum by pairing two "
            "coils wound in opposite directions. Pickup height and magnet "
            "type both shape the resulting tone as well."
        ) * 4,
    ))
    db.commit()

    interview = _start(db)
    answer_interview(db, interview, {"name": "X"})
    answer_interview(db, interview, {"weeks": 4, "minutes_per_session": 30})
    answer_interview(db, interview, {"source_ids": [str(source.id)]})

    assert interview.step == "preview"
    modules = interview.preview["modules"]
    assert len(modules) == 1
    module = modules[0]
    assert module["gap"] is False, f"expected a real grounded hit, got: {module}"
    assert module["passages"], "expected at least one real retrieved passage"
    passage = module["passages"][0]
    assert passage["source_title"] == "Getting Great Guitar Sounds"
    assert passage["page_no"] == 1  # D2: a non-paginated text source gets exactly one Page(page_no=1)
    assert isinstance(passage["score"], float)


def test_a_module_with_no_passages_above_the_floor_is_flagged_as_a_gap(db, monkeypatch):
    monkeypatch.setattr(interview_mod, "get_provider", lambda: _FakePlanProvider(_PLAN_TWO_MODULES))
    # Real ground_topic, real (empty) library — nothing above the floor for
    # EITHER module, since nothing has been ingested for this fresh db.
    interview = _start(db=db)
    answer_interview(db, interview, {"name": "X"})
    answer_interview(db, interview, {"weeks": 4, "minutes_per_session": 30})
    answer_interview(db, interview, {"source_ids": []})

    preview = interview.preview
    assert preview["gap_count"] == 2
    assert all(m["gap"] for m in preview["modules"])
    assert all(m["passages"] == [] for m in preview["modules"])


# ---------------------------------------------------------------------------
# GET is refresh-safe: it returns the current state and never recomputes
# the (cached) preview.
# ---------------------------------------------------------------------------

def test_get_interview_returns_current_state_without_recomputing_preview(db, monkeypatch):
    provider = _FakePlanProvider(_PLAN_ONE_MODULE)
    monkeypatch.setattr(interview_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(interview_mod, "ground_topic", lambda *a, **k: [_passage()])

    interview = _start(db)
    answer_interview(db, interview, {"name": "X"})
    answer_interview(db, interview, {"weeks": 4, "minutes_per_session": 30})
    answer_interview(db, interview, {"source_ids": []})
    assert len(provider.calls) == 1, "the plan call should have run exactly once, at the sources step"

    # A GET-equivalent read (describe_step/render_state) must not call the
    # model again — it only reads the cached `interview.preview` column.
    state = render_state(db, interview)
    assert state["step"] == "preview"
    assert state["findings"] == interview.preview
    assert len(provider.calls) == 1, "a refresh must not re-run the plan LLM call"


# ---------------------------------------------------------------------------
# The HTTP routes.
# ---------------------------------------------------------------------------

def test_post_curricula_interview_starts_at_who_with_options(client, db):
    student = Student(name="Maria", level="advanced")
    db.add(student); db.commit()

    r = client.post("/curricula/interview", json={"title": "Tone Fundamentals"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["step"] == "who"
    assert body["question"]
    assert {"value": str(student.id), "label": "Maria (advanced)"} in body["options"]
    interview_id = body["interview_id"]

    r2 = client.get(f"/curricula/interview/{interview_id}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["step"] == "who"


def test_get_unknown_interview_404s(client):
    r = client.get(f"/curricula/interview/{uuid.uuid4()}")
    assert r.status_code == 404, r.text


def test_answer_route_reasks_on_a_bad_answer_without_advancing(client):
    r = client.post("/curricula/interview", json={"title": "Tone Fundamentals"})
    interview_id = r.json()["interview_id"]

    r2 = client.post(f"/curricula/interview/{interview_id}/answer", json={"answer": {}})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["step"] == "who"
    assert body["error"]


def test_answer_route_advances_through_sources_and_computes_a_real_preview(client, db, monkeypatch):
    monkeypatch.setattr(interview_mod, "get_provider", lambda: _FakePlanProvider(_PLAN_ONE_MODULE))
    monkeypatch.setattr(interview_mod, "ground_topic", lambda *a, **k: [_passage()])

    r = client.post("/curricula/interview", json={"title": "Tone Fundamentals"})
    interview_id = r.json()["interview_id"]

    client.post(f"/curricula/interview/{interview_id}/answer", json={"answer": {"name": "X"}})
    client.post(
        f"/curricula/interview/{interview_id}/answer",
        json={"answer": {"weeks": 4, "minutes_per_session": 30}},
    )
    r = client.post(
        f"/curricula/interview/{interview_id}/answer", json={"answer": {"source_ids": []}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["step"] == "preview"
    assert body["findings"]["modules"][0]["passages"][0]["source_title"] == "Getting Great Guitar Sounds"


def test_confirm_step_enqueues_the_job_with_chosen_source_ids_and_allow_general_and_returns_202(
    client, db, monkeypatch,
):
    """THE test the brief calls out by name: confirm must enqueue a REAL
    `GenerationJob` carrying the tutor's chosen `source_ids`/`allow_general`,
    and return 202 — reusing the EXISTING enqueue/poll machinery untouched.
    `run_curriculum_job` is monkeypatched on `app.routers.curriculum` (the
    KEY TEST GOTCHA, see this module's docstring) so no real ~90s LLM
    generation fires during this test.
    """
    scheduled_job_ids = []
    monkeypatch.setattr(curriculum_router, "run_curriculum_job", scheduled_job_ids.append)
    monkeypatch.setattr(interview_mod, "get_provider", lambda: _FakePlanProvider(_PLAN_ONE_MODULE))
    monkeypatch.setattr(interview_mod, "ground_topic", lambda *a, **k: [_passage()])

    source = _make_source(db)

    r = client.post("/curricula/interview", json={"title": "Tone Fundamentals"})
    interview_id = r.json()["interview_id"]
    client.post(f"/curricula/interview/{interview_id}/answer", json={"answer": {"name": "X"}})
    client.post(
        f"/curricula/interview/{interview_id}/answer",
        json={"answer": {"weeks": 4, "minutes_per_session": 30}},
    )
    client.post(
        f"/curricula/interview/{interview_id}/answer",
        json={"answer": {"source_ids": [str(source.id)]}},
    )
    client.post(f"/curricula/interview/{interview_id}/answer", json={"answer": {"proceed": True}})

    r = client.post(
        f"/curricula/interview/{interview_id}/answer",
        json={"answer": {"approved": True, "allow_general": True}},
    )
    assert r.status_code == 202, r.text
    job_id = uuid.UUID(r.json()["job_id"])
    assert scheduled_job_ids == [job_id]

    job = db.get(GenerationJob, job_id)
    assert job is not None
    assert job.kind == "curriculum"
    assert job.params["source_ids"] == [str(source.id)]
    assert job.params["allow_general"] is True

    db.expire_all()
    interview = db.get(CurriculumInterview, uuid.UUID(interview_id))
    assert interview.step == "done"
    assert interview.job_id == job_id


# ---------------------------------------------------------------------------
# THE LIVE ACCEPTANCE TEST — walks the interview's grounding step against the
# REAL library (the `guitar` app db) with the REAL model. READ-ONLY BY
# CONTRACT: `_compute_preview` only ever calls `ground_topic` (a SELECT) and
# `get_provider().guided_json` (an LLM call) — it never `db.add()`s/
# `db.commit()`s anything, so this test constructs its `CurriculumInterview`
# purely in memory (never `db.add()`ed) and never persists it — mirrors
# `test_curriculum_grounding.py`'s own live test's reasoning for why it calls
# the building blocks directly rather than the full (write-performing)
# `generate_curriculum`/interview-creation path against the real app db.
# ---------------------------------------------------------------------------
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

APP_DATABASE_URL = os.environ.get(
    "APP_DATABASE_URL", "postgresql+psycopg://guitar:guitar@localhost:5434/guitar",
)


@pytest.fixture(scope="module")
def app_db():
    engine = create_engine(APP_DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.mark.integration
def test_a_real_interview_preview_is_genuinely_grounded_in_his_library(app_db):
    substantive_ids = app_db.scalars(
        select(KnowledgeSource.id).where(
            KnowledgeSource.char_count.is_not(None),
            KnowledgeSource.char_count >= interview_mod.LEN_FLOOR,
        )
    ).all()
    assert substantive_ids, "expected at least one substantive source in the real library"

    interview = CurriculumInterview(
        title="Getting a Great Guitar Tone", domain=None, step="preview",
        answers={
            "who": {"student_id": None, "name": "Live Test Student", "level": "beginner", "language": "en"},
            "duration": {"weeks": 10, "minutes_per_session": 45},
            "sources": {"source_ids": [str(s) for s in substantive_ids]},
        },
    )

    preview = interview_mod._compute_preview(app_db, interview)

    print(f"\nCOURSE TITLE: {preview['course_title']}")
    print(f"{len(preview['modules'])} modules; {preview['gap_count']} gap(s).\n")
    for m in preview["modules"]:
        tag = "GAP" if m["gap"] else "GROUNDED"
        print(f"MODULE: {m['title']!r}  ({tag})")
        print(f"  objective: {m['objective']}")
        for p in m["passages"]:
            print(f"  CITE: {p['source_title']!r} p.{p['page_no']} score={p['score']}")
        print()

    grounded = [m for m in preview["modules"] if not m["gap"]]
    gaps = [m for m in preview["modules"] if m["gap"]]
    assert grounded, "expected at least one module to genuinely ground in the real library"
    for m in grounded:
        assert all(p["source_title"] for p in m["passages"])
    assert preview["gap_count"] == len(gaps)
