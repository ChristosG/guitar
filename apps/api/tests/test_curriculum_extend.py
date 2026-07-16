"""AI ADD-MODULE (`curriculum/extend.py` + `jobs/module_generate.py`).

What has to be true:

  * THE PREFIX IS THE PREFIX. The outline call, every lesson draft, and the
    add-module call build byte-identical `[system, library]` prefixes — that
    identity IS the prompt cache, and it is the invariant that broke silently
    once before (each call kind carried its own system message, so every one of
    them re-wrote the 90K-token library at 1.25x).
  * The generated module lands APPENDED, its lessons `queued` — the same state
    `materialize_outline` births lessons in, so Resume/draft/progress need no
    new cases.
  * The tier the model asks for is CLAMPED by the course's own gap policy —
    a `library_only` course that the model wants to extend from general
    knowledge gets an honest GAP module with no lessons, not invented content.
  * The job chain drafts the new lessons immediately; a planning failure leaves
    the course EXACTLY as it was (no half-added module), with the error on the
    job row.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.curriculum.corpus as corpus_mod
import app.curriculum.draft as draft_mod
import app.curriculum.extend as extend_mod
from app.curriculum.corpus import build_library_context
from app.curriculum.draft import LessonContext, build_lesson_messages
from app.curriculum.extend import build_module_messages, generate_module
from app.curriculum.outline import build_outline_messages, materialize_outline
from app.curriculum.shape import plan_shape
from app.jobs.module_generate import run_module_generate_job
from app.llm.errors import LLMError
from app.main import app
from app.models.block import Block
from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource, Page

from test_curriculum_draft_job import (
    _book,
    _full_lesson,
    _outline,
)

client = TestClient(app)
SHAPE = plan_shape(8, 1, 50)


def _module_json(title="Pedals & Effects", tier="library", lessons=3) -> dict:
    return {
        "title": title,
        "objective": "Shape tone with pedals.",
        "tier": tier,
        "coverage_note": "Getting Great Guitar Sounds, p.19",
        "lessons": [
            {"title": f"P{i}", "objective": "o", "est_minutes": 50}
            for i in range(lessons)
        ],
    }


class _Provider:
    """Plans a module on the add-module prompt, drafts a full lesson otherwise."""

    def __init__(self, module=None, *, rate_limit_planning=False):
        self.module = module or _module_json()
        self.rate_limit_planning = rate_limit_planning
        self.calls: list[str] = []

    def guided_json(self, messages, schema, *, temperature=0.2, role="spec", max_tokens=None):
        sent = messages[-1]["content"]
        self.calls.append(sent)
        if "design ONE new module" in sent:
            if self.rate_limit_planning:
                raise LLMError("rate_limit", "429")
            return dict(self.module)
        return _full_lesson()

    def count_tokens(self, text: str) -> int:
        return len(text) // 3 + 1


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    provider = _Provider()
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(extend_mod, "get_provider", lambda: provider)
    return provider


def _course(db, *, gap_policy="general_knowledge") -> uuid.UUID:
    source = _book(db)
    return materialize_outline(
        db, _outline(), title="Tone Fundamentals", language="en", shape=SHAPE,
        library=build_library_context(db, [source.id]), source_ids=[source.id],
        gap_policy=gap_policy,
    )


def _modules(db, root_id) -> list[Block]:
    return db.scalars(
        select(Block)
        .where(Block.parent_id == root_id, Block.kind == "module")
        .order_by(Block.order)
    ).all()


# ---------------------------------------------------------------------------
# The cache contract
# ---------------------------------------------------------------------------

def test_outline_draft_and_module_prompts_share_one_byte_identical_prefix(db):
    source = _book(db)
    library = build_library_context(db, [source.id])

    outline_msgs = build_outline_messages(
        title="T", brief=None, language="el", shape=SHAPE, library=library,
        student_brief=None, gap_policy="general_knowledge",
    )
    draft_msgs = build_lesson_messages(
        ctx=LessonContext(
            lesson_title="L", lesson_objective="o", module_title="M",
            module_objective="mo", course_title="T", tier="library",
            position="lesson 1 of 4 in module 1 of 2", minutes=50,
            teaching_minutes=40, target_words=2200, floor_words=1760,
        ),
        library=library, language="el", student_brief=None, course_brief=None,
    )
    module_msgs = build_module_messages(
        course_title="T", brief=None, language="el", existing="1. M [library] — mo",
        topic="pedals", lesson_count=4, minutes_per_lesson=50, target_words=2200,
        library=library, gap_policy="general_knowledge",
    )

    # The first two entries — system + cached library — are THE cache key.
    assert outline_msgs[:2] == draft_msgs[:2] == module_msgs[:2]
    assert draft_msgs[1].get("cache") is True

    # And nothing volatile leaked above the breakpoint: the three calls differ
    # only AFTER it.
    assert outline_msgs[2] != draft_msgs[2] != module_msgs[2]

    # A different tier/word-target must NOT change the prefix (the old bug:
    # per-lesson system messages minted a cache entry per variant).
    deepen_msgs = build_lesson_messages(
        ctx=LessonContext(
            lesson_title="L2", lesson_objective="o", module_title="M",
            module_objective="mo", course_title="T", tier="general_knowledge",
            position="lesson 2 of 4 in module 1 of 2", minutes=90,
            teaching_minutes=80, target_words=4400, floor_words=3520,
        ),
        library=library, language="el", student_brief=None, course_brief=None,
    )
    assert deepen_msgs[:2] == draft_msgs[:2]


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------

def test_generated_module_is_appended_with_queued_lessons(db, _provider):
    root_id = _course(db)
    before = _modules(db, root_id)

    module = generate_module(db, root_id, topic="pedals")

    after = _modules(db, root_id)
    assert len(after) == len(before) + 1
    assert after[-1].id == module.id
    assert after[-1].order == len(before)
    assert (module.meta or {}).get("added_by") == "ai"
    assert (module.meta or {}).get("tier") == "library"

    lessons = db.scalars(
        select(Block).where(Block.parent_id == module.id).order_by(Block.order)
    ).all()
    assert [l.title for l in lessons] == ["P0", "P1", "P2"]
    assert all((l.meta or {}).get("draft_status") == "queued" for l in lessons)

    # The topic the tutor typed reached the model.
    planning = next(c for c in _provider.calls if "design ONE new module" in c)
    assert "pedals" in planning
    # And so did the existing course, lessons included — the anti-duplication input.
    assert "Module 0" in planning and "L0.0" in planning


def test_gap_policy_clamps_the_generated_module_to_an_honest_gap(db, _provider):
    _provider.module = _module_json(tier="general_knowledge")
    root_id = _course(db, gap_policy="library_only")

    module = generate_module(db, root_id, topic=None)

    assert (module.meta or {}).get("tier") == "gap"
    assert (module.meta or {}).get("tier_requested") == "general_knowledge"
    # A gap module gets NO lessons — nothing to draft, nothing invented.
    children = db.scalars(select(Block).where(Block.parent_id == module.id)).all()
    assert children == []


def test_generate_module_rejects_a_non_course_block(db):
    root_id = _course(db)
    module = _modules(db, root_id)[0]
    with pytest.raises(extend_mod.ExtendError):
        generate_module(db, module.id, topic=None)


# ---------------------------------------------------------------------------
# The job chain, through the endpoint
# ---------------------------------------------------------------------------

def test_endpoint_plans_the_module_and_chains_the_draft(db, monkeypatch):
    root_id = _course(db)

    r = client.post(f"/curricula/{root_id}/modules/generate", json={"topic": "pedals"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    # TestClient runs BackgroundTasks before returning, so the chain is done.
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["progress"]["module_id"]

    module_id = uuid.UUID(body["progress"]["module_id"])
    lessons = db.scalars(select(Block).where(Block.parent_id == module_id)).all()
    assert lessons, "the planned lessons must exist"
    # The CHAIN ran: every new lesson was drafted by the fan-out, not left queued.
    assert all((l.meta or {}).get("draft_status") == "ready" for l in lessons)


def test_a_planning_rate_limit_fails_the_job_and_adds_nothing(db, _provider):
    _provider.rate_limit_planning = True
    root_id = _course(db)
    before = len(_modules(db, root_id))

    job = GenerationJob(kind="module_generate", status="pending",
                        params={"root_id": str(root_id), "topic": None})
    db.add(job)
    db.commit()

    run_module_generate_job(job.id)

    db.expire_all()
    job = db.get(GenerationJob, job.id)
    assert job.status == "failed"
    assert job.error_kind == "rate_limit"
    assert "nothing was added" in job.error
    assert len(_modules(db, root_id)) == before
