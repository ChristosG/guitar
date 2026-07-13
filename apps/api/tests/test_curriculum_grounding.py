"""Tests for Plan 12 Task 2 (G1/G3): retrieval-grounded, two-phase curriculum
generation. Chris's central complaint about the deployed app was that
"Generate a curriculum" only ever used the model's general knowledge —
`generate_curriculum` never touched the library. This file proves the fix:

  - `app.curriculum.ground.ground_topic`'s relevance floor actually excludes
    junk (low score OR short text) and respects `source_ids` scoping.
  - `generate_curriculum` is genuinely two-phase (plan, then ground+draft
    PER MODULE), records real provenance on a grounded module, and the
    retrieved passage text actually reaches the drafting call's prompt (a
    "grounded" draft that never saw the passage is not grounded).
  - a module with nothing above the floor is an honest, UNFILLED gap by
    default, and is filled-but-labelled only when `allow_general=True`.
  - the async job path (`run_curriculum_job`) still works end to end against
    the real two-phase function, with only the LLM/`ground_topic` faked.
  - THE LIVE ACCEPTANCE TEST at the bottom: a real curriculum plan, grounded
    per-module against the REAL library (the `guitar` app db), with the real
    model — read-only by contract, see that test's own docstring.

Pure unit tests: fake provider (`get_provider`) + fake `ground_topic`/
`search`, real `guitar_test` DB session — no live LLM, no live embed server.
Mirrors `test_lesson_draft.py`/`test_agent_grounding.py`'s established
pattern for this codebase (scripted fake provider recording every call's
messages, monkeypatched module-level names).
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.curriculum.generate as generate_mod
import app.curriculum.ground as ground_mod
import app.jobs.runner as runner_mod
from app.curriculum.generate import generate_curriculum
from app.curriculum.ground import Passage, ground_topic
from app.llm.errors import GuidedJSONError
from app.models.block import Block
from app.models.generation_job import GenerationJob


class _FakeProvider:
    """`guided_json` returns the next scripted response each call (by call
    order), and records every call's `messages`/`schema` — mirrors
    `test_lesson_draft.py`'s `_FakeProvider` exactly, generalized to a
    sequence since curriculum generation is now N+1 calls (1 plan + 1 per
    module), not 1.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def guided_json(self, messages, schema, *, temperature: float = 0.2):
        self.calls.append({"messages": messages, "schema": schema})
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        resp = self._responses[idx]
        if isinstance(resp, Exception):
            raise resp
        return resp


_PLAN_ONE_MODULE = {
    "title": "Tone Fundamentals",
    "modules": [{"title": "Pickups and Tone", "objective": "Understand pickup types."}],
}

_MODULE_DRAFT = {
    "lessons": [
        {
            "title": "Single-coil vs humbucker",
            "objectives": ["Describe the tonal difference"],
            "est_minutes": 30,
            "segments": [
                {
                    "kind": "explanation",
                    "title": "What the book says",
                    "body": "UNIQUE_PASSAGE_MARKER: single-coils sound brighter.",
                    "est_minutes": 15,
                },
            ],
        },
    ],
}


def _passage(*, text="UNIQUE_PASSAGE_MARKER: single-coils sound brighter, humbuckers thicker.",
             source_title="Getting Great Guitar Sounds", page_no=25, score=0.7) -> Passage:
    return Passage(
        text=text, source_id=uuid.uuid4(), source_title=source_title,
        page_no=page_no, page_id=uuid.uuid4(), score=score,
    )


def _modules(db, course_id) -> list[Block]:
    return db.scalars(
        select(Block).where(Block.parent_id == course_id, Block.kind == "module")
    ).all()


def _lessons(db, module_id) -> list[Block]:
    return db.scalars(
        select(Block).where(Block.parent_id == module_id, Block.kind == "lesson")
    ).all()


# ---------------------------------------------------------------------------
# generate_curriculum: provenance + "the passage actually reaches the model"
# ---------------------------------------------------------------------------

def test_a_grounded_module_records_its_passages_in_provenance(db, monkeypatch):
    fake_provider = _FakeProvider([_PLAN_ONE_MODULE, _MODULE_DRAFT])
    monkeypatch.setattr(generate_mod, "get_provider", lambda: fake_provider)

    passage = _passage()
    monkeypatch.setattr(generate_mod, "ground_topic", lambda *a, **k: [passage])

    root_id = generate_curriculum(
        db, title="Tone Fundamentals", language="en", profile={"level": "beginner"},
    )

    module = _modules(db, root_id)[0]
    prov = module.target_profile["provenance"]["passages"]
    assert prov == [{
        "source_id": str(passage.source_id),
        "source_title": passage.source_title,
        "page_no": passage.page_no,
    }]
    assert "gap" not in module.target_profile


def test_the_retrieved_passage_text_actually_reaches_the_module_draft_prompt(db, monkeypatch):
    # A "grounded" draft that never saw the passage is not grounded — this
    # is THE test that matters (brief, verbatim).
    fake_provider = _FakeProvider([_PLAN_ONE_MODULE, _MODULE_DRAFT])
    monkeypatch.setattr(generate_mod, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(generate_mod, "ground_topic", lambda *a, **k: [_passage()])

    generate_curriculum(db, title="Tone Fundamentals", language="en", profile={"level": "beginner"})

    assert len(fake_provider.calls) == 2, "expected exactly 1 plan call + 1 module-draft call"
    module_draft_call = fake_provider.calls[1]
    sent = " ".join(m["content"] for m in module_draft_call["messages"])
    assert "UNIQUE_PASSAGE_MARKER" in sent


def test_phase_1_plan_call_carries_no_passage_context(db, monkeypatch):
    # Phase 1 is titles-only planning — it must not itself be handed
    # per-module CONTEXT (that's Phase 2's job, one retrieval per module).
    fake_provider = _FakeProvider([_PLAN_ONE_MODULE, _MODULE_DRAFT])
    monkeypatch.setattr(generate_mod, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(generate_mod, "ground_topic", lambda *a, **k: [_passage()])

    generate_curriculum(db, title="Tone Fundamentals", language="en", profile={"level": "beginner"})

    plan_call = fake_provider.calls[0]
    sent = " ".join(m["content"] for m in plan_call["messages"])
    assert "UNIQUE_PASSAGE_MARKER" not in sent


# ---------------------------------------------------------------------------
# PERFORMANCE — module drafts are embarrassingly parallel; must actually run
# concurrently, not one-at-a-time behind a single shared DB Session.
# ---------------------------------------------------------------------------

def test_module_drafts_run_concurrently_not_sequentially(db, monkeypatch):
    """Measured baseline: 267.8s for a 5-module curriculum, 6 sequential
    model calls (~45s each) — the tutor watches a 4.5-minute spinner. Module
    drafts (Phase 2, one guided_json call per planned module) have no
    dependency on each other and must run in parallel.

    Distinguishes the Phase 1 plan call from a Phase 2 module-draft call by
    inspecting the system message text (`_build_plan_messages` says "module
    outline"; `_build_module_draft_messages`/`_build_general_module_messages`
    both say "module content") rather than counting calls — counting would
    race against the very concurrency this test is proving exists.
    """
    import time

    plan_four_modules = {
        "title": "Tone Fundamentals",
        "modules": [{"title": f"Module {i}", "objective": "obj"} for i in range(4)],
    }
    sleep_seconds = 0.3

    class _SlowFakeProvider:
        def guided_json(self, messages, schema, *, temperature=0.2):
            system = messages[0]["content"]
            if "module outline" in system:
                return plan_four_modules
            time.sleep(sleep_seconds)
            return _MODULE_DRAFT

    monkeypatch.setattr(generate_mod, "get_provider", lambda: _SlowFakeProvider())
    monkeypatch.setattr(generate_mod, "ground_topic", lambda *a, **k: [_passage()])

    start = time.monotonic()
    generate_curriculum(db, title="Tone Fundamentals", language="en", profile={"level": "beginner"})
    elapsed = time.monotonic() - start

    # 4 module drafts run sequentially would take >= 4 * 0.3s = 1.2s; run
    # concurrently (bounded pool, width >= 4) they should all overlap and
    # finish close to ONE sleep, comfortably under 3x one sleep.
    assert elapsed < 3 * sleep_seconds, (
        f"4 module drafts took {elapsed:.2f}s — expected them to run concurrently, "
        f"not sequentially (sequential would be >= {4 * sleep_seconds:.2f}s)"
    )


# ---------------------------------------------------------------------------
# G3 — gaps are never silently filled
# ---------------------------------------------------------------------------

def test_a_topic_with_no_hits_above_the_floor_is_a_gap_and_is_not_filled(db, monkeypatch):
    fake_provider = _FakeProvider([_PLAN_ONE_MODULE, _MODULE_DRAFT])
    monkeypatch.setattr(generate_mod, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(generate_mod, "ground_topic", lambda *a, **k: [])  # nothing above floor

    root_id = generate_curriculum(
        db, title="Tone Fundamentals", language="en", profile={"level": "beginner"},
        allow_general=False,
    )

    module = _modules(db, root_id)[0]
    assert module.target_profile == {"gap": True}
    assert _lessons(db, module.id) == [], "a gap module must NOT be filled with invented content"
    assert "doesn't cover" in module.body.lower()
    # Only the plan call happened — no module-draft call for an unfilled gap.
    assert len(fake_provider.calls) == 1


def test_allow_general_fills_the_gap_but_labels_it_as_not_from_his_material(db, monkeypatch):
    fake_provider = _FakeProvider([_PLAN_ONE_MODULE, _MODULE_DRAFT])
    monkeypatch.setattr(generate_mod, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(generate_mod, "ground_topic", lambda *a, **k: [])

    root_id = generate_curriculum(
        db, title="Tone Fundamentals", language="en", profile={"level": "beginner"},
        allow_general=True,
    )

    module = _modules(db, root_id)[0]
    assert module.target_profile == {"gap": True, "general_knowledge": True}
    lessons = _lessons(db, module.id)
    assert lessons, "allow_general=True must fill the gap with content"
    assert lessons[0].title == "Single-coil vs humbucker"
    assert "general knowledge" in module.body.lower()
    assert "not from your library" in module.body.lower()
    # The general-knowledge draft call happened (unlike the unfilled case).
    assert len(fake_provider.calls) == 2
    general_call = fake_provider.calls[1]
    sent = " ".join(m["content"] for m in general_call["messages"])
    assert "CONTEXT" not in sent, "the general-knowledge fill must not fabricate a CONTEXT block"


# ---------------------------------------------------------------------------
# ground_topic: the relevance floor itself
# ---------------------------------------------------------------------------

def _hit(text, *, score, source_id=None, source_title="Some Source", page=1):
    from app.brain.retrieve import Hit
    return Hit(
        chunk_id=uuid.uuid4(), source_id=source_id or uuid.uuid4(),
        source_title=source_title, text=text, section_path=None,
        page=page, score=score, page_id=uuid.uuid4(),
    )


def test_low_score_hits_are_excluded_even_if_long(db, monkeypatch):
    junk = _hit("x" * 1000, score=0.1)  # long, but far below the score floor
    monkeypatch.setattr(ground_mod, "search", lambda *a, **k: [junk])

    assert ground_topic(db, "anything") == []


def test_short_hits_are_excluded_even_if_high_scoring(db, monkeypatch):
    # Mirrors the real junk found calibrating against the live library: an
    # 86-char unrendered page-title chunk scored as high as 0.68 on a
    # genuine tone-topic query — score alone does not exclude it, length does.
    junk = _hit("Guitar Effects Survival Guide: Introduction - TrueFire", score=0.95)
    monkeypatch.setattr(ground_mod, "search", lambda *a, **k: [junk])

    assert ground_topic(db, "anything") == []


def test_a_real_passage_above_both_floors_is_kept(db, monkeypatch):
    good = _hit("A real, substantive passage about pickups and tone." * 8, score=0.65)
    monkeypatch.setattr(ground_mod, "search", lambda *a, **k: [good])

    result = ground_topic(db, "pickups and tone")
    assert len(result) == 1
    assert result[0].text == good.text
    assert result[0].score == 0.65


def test_domain_scopes_ground_topic_to_the_hard_sql_filter_not_a_soft_hint(db, monkeypatch):
    """Review finding (IMPORTANT): the OLD generator called
    `search(db, query, k=12, domain=domain)` — a HARD SQL filter on
    `KnowledgeSource.domain`. `ground_topic` must restore that behaviour by
    passing `domain` straight through to `search()` as a hard filter, not by
    leaving `generate.py` to mash it into the free-text query as a soft
    semantic hint (which lets an off-domain passage that merely scores well
    slip through). Proven here against the REAL `search()` (not a fake), via
    a fake `KnowledgeSource.domain` filter's own SQL WHERE clause: seed one
    "theory"-domain chunk and one "tone"-domain chunk, both about the exact
    same content, and confirm a domain="theory" grounding call NEVER returns
    the "tone" chunk, however well it would otherwise score.
    """
    from app.brain.ingest import IngestPayload, ingest_source
    from app.models.knowledge import KnowledgeSource

    text = (
        "Chord voicings and scale theory: the major scale, its intervals, "
        "and how triads are built from stacked thirds within it." * 4
    )
    theory_source = KnowledgeSource(type="text", title="Theory Source", language="en", domain="theory")
    tone_source = KnowledgeSource(type="text", title="Tone Source", language="en", domain="tone")
    db.add_all([theory_source, tone_source])
    db.commit()
    ingest_source(db, theory_source.id, IngestPayload(kind="text", text=text))
    ingest_source(db, tone_source.id, IngestPayload(kind="text", text=text))
    db.commit()

    passages = ground_topic(db, "chord voicings and scale theory", domain="theory", k=10)

    assert passages, "expected at least the theory-domain passage to ground"
    assert all(p.source_title == "Theory Source" for p in passages), (
        f"domain='theory' must hard-filter out the tone-domain source, got: "
        f"{[p.source_title for p in passages]}"
    )


def test_source_ids_scopes_retrieval_to_the_tutors_chosen_sources(db, monkeypatch):
    wanted_source = uuid.uuid4()
    other_source = uuid.uuid4()
    good_text = "A real, substantive passage about pickups and tone." * 8
    hits = [
        _hit(good_text, score=0.7, source_id=wanted_source, source_title="Wanted"),
        _hit(good_text, score=0.8, source_id=other_source, source_title="Not chosen"),
    ]
    monkeypatch.setattr(ground_mod, "search", lambda *a, **k: hits)

    result = ground_topic(db, "pickups and tone", source_ids=[wanted_source])

    assert len(result) == 1
    assert result[0].source_id == wanted_source
    assert result[0].source_title == "Wanted"


# ---------------------------------------------------------------------------
# Regression: the async job path still works end to end with the REAL
# (now two-phase) generate_curriculum — only the LLM and ground_topic are
# faked, run_curriculum_job itself is untouched.
# ---------------------------------------------------------------------------

def test_the_async_curriculum_job_still_succeeds_with_the_two_phase_generator(db, monkeypatch):
    fake_provider = _FakeProvider([_PLAN_ONE_MODULE, _MODULE_DRAFT])
    monkeypatch.setattr(generate_mod, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(generate_mod, "ground_topic", lambda *a, **k: [_passage()])

    job = GenerationJob(
        kind="curriculum", status="pending",
        params={"title": "Tone Fundamentals", "language": "en", "profile": {"level": "beginner"}},
    )
    db.add(job)
    db.commit()
    job_id = job.id

    runner_mod.run_curriculum_job(job_id)

    db.expire_all()
    finished = db.get(GenerationJob, job_id)
    assert finished.status == "succeeded", (finished.error_kind, finished.error)
    assert finished.result_root_id is not None

    root = db.get(Block, finished.result_root_id)
    assert root.kind == "course"
    module = _modules(db, root.id)[0]
    assert module.target_profile["provenance"]["passages"]


_PLAN_TWO_MODULES = {
    "title": "Tone Fundamentals",
    "modules": [
        {"title": "Pickups and Tone", "objective": "Understand pickup types."},
        {"title": "Amp Gain Staging", "objective": "Understand amp gain."},
    ],
}


def test_a_failed_second_module_draft_leaves_no_curriculum_visible_to_the_tutor(
    db, monkeypatch, client,
):
    """Reviewer-reproduced CRITICAL finding: a failure on module 2's draft
    call used to leave a committed course Block with only module 1 attached
    — `GET /curricula` listed it as a real, selectable curriculum with no
    indication it was a truncated fragment of a failed run. The invariant: a
    failed curriculum job leaves NO curriculum visible to the tutor at all,
    same as every other failure path in this job.
    """
    fake_provider = _FakeProvider([
        _PLAN_TWO_MODULES,
        _MODULE_DRAFT,  # module 1's draft succeeds and gets persisted
        GuidedJSONError("guided_json: response truncated (finish_reason='length')"),
    ])
    monkeypatch.setattr(generate_mod, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(generate_mod, "ground_topic", lambda *a, **k: [_passage()])

    job = GenerationJob(
        kind="curriculum", status="pending",
        params={"title": "Tone Fundamentals", "language": "en", "profile": {"level": "beginner"}},
    )
    db.add(job)
    db.commit()
    job_id = job.id

    runner_mod.run_curriculum_job(job_id)

    db.expire_all()
    finished = db.get(GenerationJob, job_id)
    assert finished.status == "failed"
    assert finished.error_kind == "upstream"

    # (b) NO course Block exists at all — not even the half-built one from
    # module 1, which was already flushed to the DB before module 2 blew up.
    courses = db.scalars(select(Block).where(Block.kind == "course")).all()
    assert courses == [], f"expected no course Block, found {[c.title for c in courses]}"

    r = client.get("/curricula")
    assert r.status_code == 200, r.text
    assert r.json() == [], "GET /curricula must not list a fragment of a failed run"


# ---------------------------------------------------------------------------
# THE LIVE ACCEPTANCE TEST — a REAL curriculum, planned and drafted against
# the REAL library (Chris's book + his ingested course pages), with the real
# model. Answers this task's own central question honestly: is a generated
# curriculum actually built from his material, with real page citations, or
# still generic?
#
# Mirrors `test_library_live.py`/`test_agent_grounding.py`'s `app_db` pattern
# EXACTLY (own engine against the REAL app db `guitar`, never `guitar_test` —
# conftest.py force-pins `DATABASE_URL` to `guitar_test` process-wide).
# READ-ONLY BY CONTRACT, deliberately NOT calling `generate_curriculum`
# itself (which unconditionally `db.add()`s/`db.commit()`s a persisted Block
# tree — exactly what must never happen against his live, deployed db while
# he's not watching it). Instead this calls the SAME building blocks
# `generate_curriculum` calls internally — `_build_plan_messages`,
# `ground_topic` (itself just a `search()`, a SELECT), `_build_module_draft_
# messages`, `get_provider().guided_json` — directly, so the result is
# genuinely what production code would produce, without ever touching a
# write path.
# ---------------------------------------------------------------------------
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
def test_a_real_tone_curriculum_is_genuinely_grounded_in_his_library(app_db):
    from app.curriculum.generate import (
        MODULE_SCHEMA,
        PLAN_SCHEMA,
        _build_module_draft_messages,
        _build_plan_messages,
    )
    from app.llm.factory import get_provider

    provider = get_provider()
    title = "Getting a Great Guitar Tone"
    profile = {"level": "beginner"}

    plan_messages = _build_plan_messages(
        title=title, language="en", profile=profile, domain=None, target_minutes_total=600,
    )
    plan = provider.guided_json(plan_messages, PLAN_SCHEMA)

    modules_report = []
    for module in plan["modules"]:
        query = f"{module['title']} {module['objective']}"
        passages = ground_topic(app_db, query, k=5)
        entry = {"title": module["title"], "objective": module["objective"], "passages": passages}
        if passages:
            draft_messages = _build_module_draft_messages(
                module_title=module["title"], objective=module["objective"],
                language="en", passages=passages,
            )
            entry["draft"] = provider.guided_json(draft_messages, MODULE_SCHEMA)
        modules_report.append(entry)

    grounded = [m for m in modules_report if m["passages"]]
    gaps = [m for m in modules_report if not m["passages"]]

    print(f"\nPLAN TITLE: {plan['title']}")
    print(f"{len(modules_report)} modules planned; {len(grounded)} grounded, {len(gaps)} gaps.\n")
    for m in modules_report:
        print(f"MODULE: {m['title']!r}  ({'GROUNDED' if m['passages'] else 'GAP'})")
        print(f"  objective: {m['objective']}")
        for p in m["passages"]:
            print(f"  CITE: {p.source_title!r} p.{p.page_no} score={p.score:.3f}")
            print(f"        {p.text[:160]!r}")
        if "draft" in m:
            for lesson in m["draft"]["lessons"]:
                print(f"  lesson: {lesson['title']}  ({lesson['est_minutes']} min)")
                for seg in lesson.get("segments") or []:
                    print(f"    - {seg['title']}: {seg['body'][:160]!r}")
        print()

    assert grounded, "expected at least one module to genuinely ground in the real library"
    for m in grounded:
        assert all(p.source_title for p in m["passages"])
        assert all(p.score >= ground_mod.SCORE_FLOOR for p in m["passages"])
