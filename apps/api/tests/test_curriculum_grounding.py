"""Grounded curriculum authoring, v2 (Plan 13, Stage 6.5-6.7).

THIS FILE WAS REWRITTEN, AND THE OLD SYMBOLS IT TESTED ARE GONE. It used to import
`PLAN_SCHEMA`, `MODULE_SCHEMA`, `_build_plan_messages` and
`_build_module_draft_messages` from `curriculum/generate.py` — the two-phase
retrieval generator. All four are deleted. What replaced them, and why:

  * The planner had NEVER READ THE BOOK. It outlined from general knowledge and
    only afterwards asked retrieval whether his library had anything to say. Now
    the model reads all ~90K tokens of it and outlines FROM it.

  * "Gap" was a COSINE FLOOR. On this corpus the measured margin between the
    lowest COVERED topic (0.881) and the highest UNCOVERED one (0.860) is 0.021 —
    a coin flip with a decimal point, deciding unattended whether 20 lessons come
    from his book or the model's memory. Now the model reads the book and SAYS.

  * One call per MODULE could not physically produce a real lesson: 4-5 x 2,200
    words in Greek is 30-40k output tokens, straight through `max_tokens`, and a
    truncated response surfaces as a JSON PARSE error — debugged in the wrong file.
    The fan-out unit is the LESSON.

What is tested here: the outline call sees the whole library; tiers are the
model's and are clamped by the tutor's gap policy; the materialized tree is queued
and carries its provenance on `meta`; and EVERY CITATION IS CHECKED — a page the
model was never shown is a fabrication, and it renders as a chip the tutor CLICKS.
"""
import uuid

import pytest
from sqlalchemy import select

import app.curriculum.corpus as corpus_mod
import app.curriculum.draft as draft_mod
import app.curriculum.outline as outline_mod
import app.jobs.runner as runner_mod
from app.curriculum.corpus import LibraryContext, build_library_context
from app.curriculum.depth import SECTIONS, SECTION_WEIGHTS
from app.curriculum.draft import (
    LessonContext,
    draft_lesson,
    invalid_citations,
    persist_lesson,
)
from app.curriculum.outline import (
    TIER_GAP,
    TIER_GENERAL,
    TIER_LIBRARY,
    clamp_tier,
    generate_outline,
    materialize_outline,
)
from app.curriculum.shape import plan_shape
from app.llm.errors import GuidedJSONError
from app.models.block import Block
from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource, Page

SHAPE = plan_shape(8, 1, 50)   # 8 lessons / 2 modules x 4 — small enough to script


class _FakeProvider:
    """Scripted `guided_json`, recording every call. Also serves `count_tokens`,
    since `corpus.py` reaches for the same seam."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def guided_json(self, messages, schema, *, temperature=0.2, role="spec", max_tokens=None):
        self.calls.append({"messages": messages, "schema": schema, "role": role})
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        resp = self._responses[idx]
        if isinstance(resp, Exception):
            raise resp
        return resp

    def count_tokens(self, text: str) -> int:
        return len(text) // 3 + 1


@pytest.fixture(autouse=True)
def _tokens(monkeypatch):
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: _FakeProvider([]))


def _book(db, title="Getting Great Guitar Sounds") -> KnowledgeSource:
    source = KnowledgeSource(type="pdf", title=title, language="en", status="ready")
    db.add(source)
    db.flush()
    for page_no, text in [
        (19, "UNIQUE_PAGE_19: the Tube Screamer is the most copied overdrive ever. " * 3),
        (20, "UNIQUE_PAGE_20: a humbucker cancels hum by pairing opposed coils. " * 3),
    ]:
        db.add(Page(source_id=source.id, page_no=page_no, text=text, status="ready"))
    db.commit()
    return source


def _outline_payload(tier=TIER_LIBRARY) -> dict:
    return {
        "title": "Tone Fundamentals",
        "modules": [
            {
                "title": f"Module {i}",
                "objective": "Understand tone.",
                "tier": tier,
                "coverage_note": "Covered on p.19-20.",
                "lessons": [
                    {"title": f"Lesson {i}.{j}", "objective": "o", "est_minutes": 50}
                    for j in range(4)
                ],
            }
            for i in range(2)
        ],
    }


def _lesson_payload(*, words=400, citations=None) -> dict:
    body = " ".join(["word"] * words)
    lesson = {"title": "Single-coil vs humbucker", "summary": "Two sentences."}
    for name in SECTIONS:
        section = {"body": body, "citations": list(citations or [])}
        if name == "exercises":
            section["items"] = [{"title": "E1", "instructions": body, "est_minutes": 5}]
        if name == "qa_prompts":
            section["items"] = [{"question": "why?", "answer_key": body}]
        lesson[name] = section
    return lesson


def _full_lesson(citations=None) -> dict:
    """A lesson that clears the 1,760-word floor, so no deepen pass fires."""
    lesson = {"title": "Single-coil vs humbucker", "summary": "Two sentences."}
    for name in SECTIONS:
        n = int(SECTION_WEIGHTS[name] * 2200)
        section = {"body": " ".join(["word"] * n), "citations": list(citations or [])}
        if name == "exercises":
            section["items"] = []
        if name == "qa_prompts":
            section["items"] = [{"question": "why?", "answer_key": "because"}]
        lesson[name] = section
    return lesson


def _modules(db, root_id) -> list[Block]:
    return db.scalars(
        select(Block).where(Block.parent_id == root_id, Block.kind == "module")
        .order_by(Block.order)
    ).all()


def _lessons(db, module_id) -> list[Block]:
    return db.scalars(
        select(Block).where(Block.parent_id == module_id, Block.kind == "lesson")
        .order_by(Block.order)
    ).all()


# ---------------------------------------------------------------------------
# `fits` actually gates `prefix_messages` (Task 7)
# ---------------------------------------------------------------------------

def test_oversized_library_is_NOT_shipped_whole():
    """corpus.py's docstring promises: "Above `full_context_budget` `fits` goes
    False and the caller degrades to per-module retrieval — WITH AN HONEST
    BANNER, never silently."

    It does not. `fits` is computed at corpus.py:208 and consulted by nobody:
    outline.py:148 and extend.py:160 call prefix_messages() unconditionally, and
    draft.py:268 ADDS retrieved passages on top of the still-complete library —
    strictly worse than either path alone. Meanwhile outline.py:361 sets
    `full_context: false` and tree-board.tsx:189 renders a banner reporting a
    degrade that never happened.

    Unreachable at 82K. The four new books put the corpus at 592,841 tokens
    against a 600,000 budget — 11 book pages of headroom.
    """
    from app.curriculum.corpus import LibraryContext, prefix_messages

    oversized = LibraryContext(
        text="<source id='S1' title='t'>[p.1] " + ("x" * 100) + "</source>",
        token_count=700_000, fits=False,
        page_index={"S1": {1}}, ref_to_source_id={},
        sources=[{"ref": "S1", "id": "x", "title": "t", "pages": 1, "chars": 100}],
    )
    messages = prefix_messages(oversized)
    body = "\n".join(m["content"] for m in messages)

    assert "[p.1]" not in body, "the library shipped whole despite fits=False"
    assert not any(m.get("cache") for m in messages), \
        "a 700K block must not be written to the cache at 1.25x"
    assert "too large" in body.lower() or "retrieval" in body.lower(), \
        "the model must be told why it is not seeing the library"


def test_fitting_library_is_unchanged():
    """The 82K path today, and the <=300K path after the canon lands. This is the
    regression guard: the fix must not alter the working case."""
    from app.curriculum.corpus import LibraryContext, prefix_messages

    fits = LibraryContext(
        text="<source id='S1' title='t'>[p.1] hello</source>",
        token_count=90_000, fits=True,
        page_index={"S1": {1}}, ref_to_source_id={}, sources=[],
    )
    messages = prefix_messages(fits)
    assert any(m.get("cache") for m in messages), "the stable prefix must still cache"
    assert "[p.1] hello" in "\n".join(m["content"] for m in messages)


# ---------------------------------------------------------------------------
# The outline reads the WHOLE library — this is the change
# ---------------------------------------------------------------------------

def test_the_outline_call_is_given_the_entire_library_not_a_retrieved_excerpt(db, monkeypatch):
    """THE test that matters. The old planner never saw the library at all; the old
    module drafter saw 5 retrieved passages. This one gets the book."""
    source = _book(db)
    provider = _FakeProvider([_outline_payload()])
    monkeypatch.setattr(outline_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    library = build_library_context(db, [source.id])
    generate_outline(
        db, title="Tone", brief=None, language="en", shape=SHAPE, library=library,
        student_brief=None, gap_policy="general_knowledge",
    )

    assert len(provider.calls) == 1, "the outline is ONE call over the whole library"
    sent = " ".join(m["content"] for m in provider.calls[0]["messages"])
    assert "UNIQUE_PAGE_19" in sent
    assert "UNIQUE_PAGE_20" in sent
    assert "[p.19]" in sent, "the page markers are what make a citation checkable"
    assert provider.calls[0]["role"] == "plan"


def test_the_outline_is_shape_enforced_whatever_the_model_returns(db, monkeypatch):
    """The model is TOLD the exact counts and then MADE to have them. The schema
    cannot enforce them — Claude strips `minItems`."""
    source = _book(db)
    stingy = {
        "title": "Tone",
        "modules": [{
            "title": "Only one", "objective": "o", "tier": TIER_LIBRARY,
            "coverage_note": "", "lessons": [{"title": "L", "objective": "o", "est_minutes": 30}],
        }],
    }
    provider = _FakeProvider([stingy])
    monkeypatch.setattr(outline_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    outline = generate_outline(
        db, title="Tone", brief=None, language="en", shape=plan_shape(20, 1, 50),
        library=build_library_context(db, [source.id]),
        student_brief=None, gap_policy="general_knowledge",
    )

    assert len(outline["modules"]) == 5
    assert [len(m["lessons"]) for m in outline["modules"]] == [4, 4, 4, 4, 4]


def test_the_course_brief_and_the_student_brief_both_reach_the_outline_prompt(db, monkeypatch):
    source = _book(db)
    provider = _FakeProvider([_outline_payload()])
    monkeypatch.setattr(outline_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    generate_outline(
        db, title="Tone", brief="COURSE_BRIEF_MARKER", language="en", shape=SHAPE,
        library=build_library_context(db, [source.id]),
        student_brief="STUDENT_BRIEF_MARKER", gap_policy="general_knowledge",
    )

    sent = " ".join(m["content"] for m in provider.calls[0]["messages"])
    assert "COURSE_BRIEF_MARKER" in sent
    assert "STUDENT_BRIEF_MARKER" in sent


def test_an_outline_with_no_modules_raises_rather_than_persisting_an_empty_course(db, monkeypatch):
    provider = _FakeProvider([{"title": "Tone", "modules": []}])
    monkeypatch.setattr(outline_mod, "get_provider", lambda: provider)

    with pytest.raises(GuidedJSONError):
        generate_outline(
            db, title="Tone", brief=None, language="en", shape=SHAPE,
            library=LibraryContext(text="", token_count=0, fits=True),
            student_brief=None, gap_policy="general_knowledge",
        )


# ---------------------------------------------------------------------------
# Tiers: the model's honest judgement, clamped by the tutor's policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "requested,policy,expect",
    [
        (TIER_LIBRARY, "library_only", TIER_LIBRARY),
        (TIER_GENERAL, "library_only", TIER_GAP),      # NOT downgraded to "library"
        ("web", "library_only", TIER_GAP),
        (TIER_LIBRARY, "general_knowledge", TIER_LIBRARY),
        (TIER_GENERAL, "general_knowledge", TIER_GENERAL),
        ("web", "general_knowledge", TIER_GENERAL),
        ("web", "web", "web"),
        (None, "general_knowledge", TIER_GENERAL),     # unknown is never "library"
        ("nonsense", "general_knowledge", TIER_GENERAL),
    ],
)
def test_a_tier_the_tutor_did_not_allow_becomes_an_honest_gap_never_a_fake_citation(
    requested, policy, expect,
):
    """G3, in one table. A module the model tiered `general_knowledge` under a
    `library_only` policy does NOT become `library` — it becomes a GAP. "Your
    library doesn't cover this" is a true and useful thing to tell a tutor;
    "here is some content, from somewhere, unlabelled" is the bug he reported."""
    assert clamp_tier(requested, policy) == expect


def test_a_gap_module_is_persisted_unfilled_with_an_honest_body(db, monkeypatch):
    source = _book(db)
    provider = _FakeProvider([_outline_payload(tier=TIER_GENERAL)])
    monkeypatch.setattr(outline_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    library = build_library_context(db, [source.id])
    outline = generate_outline(
        db, title="Tone", brief=None, language="en", shape=SHAPE, library=library,
        student_brief=None, gap_policy="library_only",
    )
    root_id = materialize_outline(
        db, outline, title="Tone", language="en", shape=SHAPE, library=library,
        gap_policy="library_only",
    )

    for module in _modules(db, root_id):
        assert module.meta["tier"] == TIER_GAP
        assert "doesn't cover" in module.body.lower()
        assert _lessons(db, module.id) == [], (
            "a gap module must have NO lessons — nothing to draft, no call made, "
            "no content invented"
        )


# ---------------------------------------------------------------------------
# materialize_outline: the tree exists BEFORE any lesson is drafted
# ---------------------------------------------------------------------------

def test_the_whole_tree_is_persisted_queued_so_the_board_opens_instantly(db, monkeypatch):
    source = _book(db)
    provider = _FakeProvider([_outline_payload()])
    monkeypatch.setattr(outline_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    library = build_library_context(db, [source.id])
    outline = generate_outline(
        db, title="Tone", brief=None, language="en", shape=SHAPE, library=library,
        student_brief=None, gap_policy="general_knowledge",
    )
    root_id = materialize_outline(
        db, outline, title="Tone", language="en", shape=SHAPE, library=library,
        source_ids=[source.id], brief="a brief",
    )

    course = db.get(Block, root_id)
    assert course.kind == "course"
    assert course.meta["shape"]["lessons_total"] == 8
    assert course.meta["brief"] == "a brief"
    assert course.meta["library"]["full_context"] is True

    modules = _modules(db, root_id)
    assert len(modules) == 2
    for module in modules:
        assert module.meta["tier"] == TIER_LIBRARY
        assert module.meta["coverage_note"]
        lessons = _lessons(db, module.id)
        assert len(lessons) == 4
        for lesson in lessons:
            assert lesson.meta["draft_status"] == "queued"
            assert lesson.est_minutes == 50


def test_an_oversized_library_sets_the_honest_banner_flag_on_the_course(db, monkeypatch):
    """`full_context: false` is what the board renders the banner from. Without it
    the tutor's course is silently drafted from retrieval and he is never told —
    which is the bug this entire stage exists to remove."""
    monkeypatch.setattr(corpus_mod.settings, "full_context_budget", 10)
    source = _book(db)
    provider = _FakeProvider([_outline_payload()])
    monkeypatch.setattr(outline_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    library = build_library_context(db, [source.id])
    assert not library.fits
    root_id = materialize_outline(
        db, _outline_payload(), title="Tone", language="en", shape=SHAPE, library=library,
    )

    assert db.get(Block, root_id).meta["library"]["full_context"] is False


def test_meta_is_written_as_a_whole_dict_and_actually_survives_a_refetch(db, monkeypatch):
    """`Block.meta` is plain `sa.JSON` with NO `MutableDict`. In-place mutation
    (`block.meta["k"] = v`) appears to work in dev — the identity map hands you the
    same dict back — and is SILENTLY NOT PERSISTED in production. The entire
    live-progress model rests on this, so it gets a test that goes through the DB.
    """
    source = _book(db)
    library = build_library_context(db, [source.id])
    root_id = materialize_outline(
        db, _outline_payload(), title="Tone", language="en", shape=SHAPE, library=library,
    )

    db.expire_all()   # nothing may be served from the identity map
    course = db.get(Block, root_id)
    assert course.meta["shape"]["modules"] == 2
    lesson = _lessons(db, _modules(db, root_id)[0].id)[0]
    assert lesson.meta["draft_status"] == "queued"


# ---------------------------------------------------------------------------
# CITATIONS — a fabricated page is worse than no page
# ---------------------------------------------------------------------------

def test_a_citation_to_a_page_the_model_was_never_shown_is_caught(db):
    source = _book(db)     # pages 19 and 20 only
    library = build_library_context(db, [source.id])

    lesson = _full_lesson(citations=[{"source_id": "S1", "page": 412}])
    bad = invalid_citations(lesson, library)

    assert bad, "p.412 of a 2-page book must not pass"
    assert all(page == 412 for _section, _ref, page in bad)


def test_a_citation_to_a_real_page_passes(db):
    source = _book(db)
    library = build_library_context(db, [source.id])

    lesson = _full_lesson(citations=[{"source_id": "S1", "page": 19}])

    assert invalid_citations(lesson, library) == []


def test_a_hallucinated_citation_triggers_exactly_one_repair_retry(db, monkeypatch):
    """The tutor CLICKS these chips. A wrong page number lands him on a page that
    does not say what the lesson claims it says — and he would be right to stop
    trusting every other chip on the screen after that, including the true ones."""
    source = _book(db)
    library = build_library_context(db, [source.id])

    provider = _FakeProvider([
        _full_lesson(citations=[{"source_id": "S1", "page": 412}]),   # fabricated
        _full_lesson(citations=[{"source_id": "S1", "page": 19}]),    # repaired
    ])
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    lesson, _m = draft_lesson(
        db, ctx=_ctx(), library=library, language="en",
    )

    assert len(provider.calls) == 2, "one draft + one repair"
    repair_text = provider.calls[1]["messages"][-1]["content"]
    assert "412" in repair_text, "the repair must NAME the offending page"
    assert "p.19" in repair_text or "19-20" in repair_text, "and say what is actually available"
    assert invalid_citations(lesson, library) == []


def test_a_citation_that_survives_the_repair_is_DROPPED_and_the_prose_is_kept(db, monkeypatch):
    """After one repair, a false citation is the only part worth destroying. The
    prose is almost certainly fine, and an uncited paragraph is an honest thing
    while a false citation is not."""
    source = _book(db)
    library = build_library_context(db, [source.id])

    provider = _FakeProvider([
        _full_lesson(citations=[{"source_id": "S1", "page": 412}]),
        _full_lesson(citations=[{"source_id": "S1", "page": 999}]),   # still wrong
    ])
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    lesson, _m = draft_lesson(db, ctx=_ctx(), library=library, language="en")

    assert len(provider.calls) == 2, "still only ONE repair — not a retry loop"
    assert invalid_citations(lesson, library) == []
    assert lesson["theory"]["citations"] == []
    assert lesson["theory"]["body"], "the prose survives"


# ---------------------------------------------------------------------------
# The deepen pass
# ---------------------------------------------------------------------------

def _ctx(tier=TIER_LIBRARY) -> LessonContext:
    return LessonContext(
        lesson_title="Single-coil vs humbucker", lesson_objective="hear the difference",
        module_title="Pickups and Tone", module_objective="understand pickups",
        course_title="Tone Fundamentals", tier=tier,
        position="lesson 1 of 4 in module 1 of 2",
        minutes=50, teaching_minutes=40, target_words=2200, floor_words=1760,
    )


def test_a_thin_lesson_gets_exactly_one_deepen_pass_and_then_clears_the_floor(db, monkeypatch):
    source = _book(db)
    library = build_library_context(db, [source.id])

    provider = _FakeProvider([_lesson_payload(words=20), _full_lesson()])
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    _lesson, m = draft_lesson(db, ctx=_ctx(), library=library, language="en")

    assert len(provider.calls) == 2, "one draft + exactly one deepen"
    assert m.meets_floor, (m.total_words, m.floor)

    deepen_prompt = provider.calls[1]["messages"][-1]["content"]
    assert "theory" in deepen_prompt, "the deepen pass must NAME the thin sections"
    assert "PREVIOUS DRAFT" in deepen_prompt


def test_deepening_stops_after_one_pass_even_if_it_is_still_thin(db, monkeypatch):
    """He is paying per token with his own card. A model that missed the floor twice
    will pad, not improve. The lesson is persisted as it is, with its word count
    visible and a Deepen button — not silently retried into a bill."""
    source = _book(db)
    library = build_library_context(db, [source.id])

    provider = _FakeProvider([_lesson_payload(words=20), _lesson_payload(words=30)])
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    _lesson, m = draft_lesson(db, ctx=_ctx(), library=library, language="en")

    assert len(provider.calls) == 2
    assert not m.meets_floor
    assert m.total_words > 0


def test_a_deepen_pass_that_came_back_SHORTER_is_discarded(db, monkeypatch):
    """A "deepen" that shrinks the lesson has not deepened anything, and accepting
    it would make the tutor's Deepen button able to make his lesson smaller."""
    source = _book(db)
    library = build_library_context(db, [source.id])

    provider = _FakeProvider([_lesson_payload(words=100), _lesson_payload(words=5)])
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    _lesson, m = draft_lesson(db, ctx=_ctx(), library=library, language="en")

    first = _lesson_payload(words=100)
    from app.curriculum.depth import measure
    assert m.total_words == measure(first, teaching_minutes=40).total_words


# ---------------------------------------------------------------------------
# The lesson prompt
# ---------------------------------------------------------------------------

def test_a_general_knowledge_lesson_is_forbidden_from_citing_his_library(db, monkeypatch):
    """The one thing that would make an honestly-labelled general-knowledge module
    dishonest: a fabricated page number attached to it."""
    source = _book(db)
    library = build_library_context(db, [source.id])
    provider = _FakeProvider([_full_lesson()])
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    draft_lesson(db, ctx=_ctx(tier=TIER_GENERAL), library=library, language="en")

    # The tier directive lives in the volatile TAIL now, not the system message
    # — a per-tier system string was silently minting a fresh 90K-token cache
    # entry per variant (see corpus.CURRICULUM_SYSTEM).
    tail = provider.calls[0]["messages"][-1]["content"]
    assert "cite" in tail.lower()
    assert "empty" in tail.lower()
    assert "LABELLED" in tail or "labelled" in tail


def test_the_lesson_prompt_states_the_word_floor_because_the_schema_cannot(db, monkeypatch):
    source = _book(db)
    library = build_library_context(db, [source.id])
    provider = _FakeProvider([_full_lesson()])
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    draft_lesson(db, ctx=_ctx(), library=library, language="en")

    # Length enforcement rides the volatile tail (recency-strongest position),
    # never the shared cached system message.
    tail = provider.calls[0]["messages"][-1]["content"]
    assert "2,200" in tail
    assert "1,760" in tail
    assert provider.calls[0]["role"] == "draft"


def test_the_lesson_knows_where_it_sits_in_the_course(db, monkeypatch):
    """The model drafts one lesson with no sight of the other 19. Without its
    position it re-teaches the basics in lesson 12 — it has no way to know lesson 1
    already did."""
    source = _book(db)
    library = build_library_context(db, [source.id])
    provider = _FakeProvider([_full_lesson()])
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)

    draft_lesson(db, ctx=_ctx(), library=library, language="en")

    sent = provider.calls[0]["messages"][-1]["content"]
    assert "lesson 1 of 4 in module 1 of 2" in sent


# ---------------------------------------------------------------------------
# Persisting a drafted lesson
# ---------------------------------------------------------------------------

def test_a_drafted_lesson_becomes_segments_with_resolvable_citations(db):
    source = _book(db)
    library = build_library_context(db, [source.id])
    root_id = materialize_outline(
        db, _outline_payload(), title="Tone", language="en", shape=SHAPE, library=library,
        source_ids=[source.id],
    )
    lesson_block = _lessons(db, _modules(db, root_id)[0].id)[0]

    lesson = _full_lesson(citations=[{"source_id": "S1", "page": 19}])
    from app.curriculum.depth import measure
    persist_lesson(
        db, lesson_block, lesson, measure(lesson, teaching_minutes=40), library,
        qa_minutes=10, teaching_minutes=40,
    )
    db.commit()
    db.expire_all()

    lesson_block = db.get(Block, lesson_block.id)
    assert lesson_block.meta["draft_status"] == "ready"
    assert lesson_block.meta["meets_floor"] is True
    assert lesson_block.meta["word_count"] > 1760

    segments = sorted(lesson_block.children, key=lambda b: b.order)
    assert [s.meta["section"] for s in segments] == list(SECTIONS)

    theory = next(s for s in segments if s.meta["section"] == "theory")
    [cite] = theory.meta["citations"]
    assert cite["page"] == 19
    # The REAL source id, not the "S1" prompt ref — the Reader deep-links off this.
    assert cite["source_id"] == str(source.id)
    assert cite["source_title"] == "Getting Great Guitar Sounds"


def test_persisting_twice_replaces_the_segments_rather_than_duplicating_them(db):
    """Idempotence is what makes Deepen, a re-draft, and a Resume that re-runs a
    lesson whose worker died after the model call all safe."""
    source = _book(db)
    library = build_library_context(db, [source.id])
    root_id = materialize_outline(
        db, _outline_payload(), title="Tone", language="en", shape=SHAPE, library=library,
    )
    lesson_block = _lessons(db, _modules(db, root_id)[0].id)[0]

    from app.curriculum.depth import measure
    lesson = _full_lesson()
    for _ in range(2):
        persist_lesson(
            db, lesson_block, lesson, measure(lesson, teaching_minutes=40), library,
            qa_minutes=10, teaching_minutes=40,
        )
        db.commit()

    db.expire_all()
    segments = db.scalars(
        select(Block).where(Block.parent_id == lesson_block.id)
    ).all()
    assert len(segments) == len(SECTIONS), "no duplicate theory sections"


def test_the_qa_answer_keys_survive_into_the_persisted_segment(db):
    """The tutor is holding this page while the student answers. A Q&A prompt
    separated from its answer key is a prompt he cannot use."""
    source = _book(db)
    library = build_library_context(db, [source.id])
    root_id = materialize_outline(
        db, _outline_payload(), title="Tone", language="en", shape=SHAPE, library=library,
    )
    lesson_block = _lessons(db, _modules(db, root_id)[0].id)[0]

    lesson = _full_lesson()
    lesson["qa_prompts"]["items"] = [
        {"question": "Why does a humbucker cancel hum?", "answer_key": "Opposed coils."},
    ]
    from app.curriculum.depth import measure
    persist_lesson(
        db, lesson_block, lesson, measure(lesson, teaching_minutes=40), library,
        qa_minutes=10, teaching_minutes=40,
    )
    db.commit()

    qa = next(s for s in lesson_block.children if s.meta["section"] == "qa_prompts")
    assert "Why does a humbucker cancel hum?" in qa.body
    assert "Opposed coils." in qa.body
    assert qa.est_minutes == 10


# ---------------------------------------------------------------------------
# The job path still works end to end
# ---------------------------------------------------------------------------

def test_the_curriculum_job_outlines_materializes_and_hands_off_to_the_fan_out(
    db, monkeypatch,
):
    source = _book(db)
    provider = _FakeProvider([_outline_payload()])
    monkeypatch.setattr(outline_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)
    handed_off = []
    monkeypatch.setattr(runner_mod, "run_curriculum_draft_job", handed_off.append)

    job = GenerationJob(
        kind="curriculum", status="pending",
        params={
            "title": "Tone Fundamentals", "language": "en", "profile": {"level": "beginner"},
            "weeks": 8, "minutes_per_session": 50, "source_ids": [str(source.id)],
        },
    )
    db.add(job)
    db.commit()
    job_id = job.id

    runner_mod.run_curriculum_job(job_id)

    db.expire_all()
    finished = db.get(GenerationJob, job_id)
    assert finished.result_root_id is not None, (finished.error_kind, finished.error)
    assert handed_off == [job_id]

    root = db.get(Block, finished.result_root_id)
    assert root.kind == "course"
    assert len(_modules(db, root.id)) == 2
    assert all(
        lesson.meta["draft_status"] == "queued"
        for module in _modules(db, root.id)
        for lesson in _lessons(db, module.id)
    )


def test_a_failed_outline_leaves_no_curriculum_visible_to_the_tutor(db, monkeypatch, client):
    """A failed job must leave NOTHING listed. The old generator flushed each
    module's Blocks as it went, so a failure on module 2 left a committed course
    with one module attached — listed as a real, selectable curriculum with no
    indication it was the wreckage of a failed run."""
    source = _book(db)
    provider = _FakeProvider([GuidedJSONError("truncated")])
    monkeypatch.setattr(outline_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    job = GenerationJob(
        kind="curriculum", status="pending",
        params={"title": "Tone", "language": "en", "weeks": 8,
                "source_ids": [str(source.id)]},
    )
    db.add(job)
    db.commit()
    job_id = job.id

    runner_mod.run_curriculum_job(job_id)

    db.expire_all()
    finished = db.get(GenerationJob, job_id)
    assert finished.status == "failed"
    assert finished.error_kind == "upstream"

    assert db.scalars(select(Block).where(Block.kind == "course")).all() == []
    r = client.get("/curricula")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# THE LIVE ACCEPTANCE TESTS — the real library, the real model.
#
# READ-ONLY BY CONTRACT against the app db. They call the same building blocks
# production calls (`build_library_context` is a SELECT; `generate_outline` and
# `draft_lesson` are LLM calls that touch no write path) — never
# `materialize_outline`, which persists a tree and must not run against his live,
# deployed database while he is not watching it.
#
# Mirrors `test_library_live.py`'s `app_db` pattern: its own engine against the
# REAL `guitar` db, because conftest force-pins DATABASE_URL to `guitar_test`.
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
def test_his_real_library_fits_in_one_prompt(app_db):
    """THE MEASUREMENT THE WHOLE ARCHITECTURE RESTS ON. ~359,000 chars ~= 90K
    tokens = 9% of Sonnet 5's window. If this ever stops being true the app does
    not break — it degrades to retrieval, honestly, with a banner — but the cost
    model and the "no false gaps" guarantee both change, and we would want to know.
    """
    library = build_library_context(app_db, None)

    print(f"\nLIBRARY: {library.summary()}")
    for source in library.sources:
        print(f"  {source['ref']}: {source['title']!r} — "
              f"{source['pages']} pages, {source['chars']:,} chars")

    assert not library.is_empty, "the real library is empty — is the app db seeded?"
    assert library.fits, (
        f"his library no longer fits whole ({library.token_count:,} tokens > "
        f"{corpus_mod.settings.full_context_budget:,}) — authoring will now degrade "
        f"to retrieval, which is exactly what Stage 6 exists to avoid"
    )
    assert library.page_index, "no page markers — citations cannot be validated"


@pytest.mark.integration
def test_a_real_greek_curriculum_is_outlined_from_his_book_and_tiered_honestly(app_db):
    """THE PROOF THAT THIS REDESIGN WAS WORTH IT.

    A Greek outline, over his English book, with the model having READ it — and
    tiering each module by what it found rather than by a cosine floor with a 0.021
    separation margin. Under the old code a Greek query scored 0.10-0.22 lower than
    its English twin, cleared no floor, and every module came back a false GAP. That
    is, verbatim, the bug he reported.
    """
    from app.llm.factory import get_provider

    library = build_library_context(app_db, None)
    assert library.fits

    shape = plan_shape(20, 1, 50)
    outline = generate_outline(
        app_db,
        title="Πώς να βγάλω καλό ήχο από την κιθάρα μου",
        brief="Θέλω να μάθει ο μαθητής να στήνει τον ήχο του: μικρόφωνα, ενισχυτής, πετάλια.",
        language="el",
        shape=shape,
        library=library,
        student_brief=None,
        gap_policy="general_knowledge",
    )

    print(f"\nCOURSE: {outline['title']!r}")
    for module in outline["modules"]:
        print(f"  [{module['tier']}] {module['title']!r} — {module['coverage_note']}")
        for lesson in module["lessons"]:
            print(f"      - {lesson['title']}")

    assert len(outline["modules"]) == 5
    assert sum(len(m["lessons"]) for m in outline["modules"]) == 20

    # It is GREEK...
    text = outline["title"] + " ".join(m["title"] for m in outline["modules"])
    greek = sum(1 for ch in text if "Ͱ" <= ch <= "Ͽ" or "ἀ" <= ch <= "῿")
    assert greek / max(1, len(text)) > 0.3, f"the outline came back in the wrong language: {text!r}"

    # ...and at least one module is genuinely GROUNDED IN HIS BOOK, in Greek, which
    # is precisely what does not happen today.
    grounded = [m for m in outline["modules"] if m["tier"] == TIER_LIBRARY]
    assert grounded, (
        "every module came back as a gap on a topic his book is ABOUT — this is the "
        "original bug, and the whole point of full-context authoring is that it "
        "cannot happen"
    )


@pytest.mark.integration
def test_a_real_lesson_is_drafted_to_length_in_greek_with_citations_that_resolve(app_db):
    """~2,200 words of Greek, and every page it cites is a page it was actually
    shown. The second call also proves the CACHE is being read — if
    `cache_read_input_tokens` is zero, the prefix is not stable, every lesson is
    re-writing 90K tokens at 1.25x, and nothing else looks any different.
    """
    from app.llm.factory import get_provider

    library = build_library_context(app_db, None)
    assert library.fits

    provider = get_provider()
    ctx = LessonContext(
        lesson_title="Μικρόφωνα: single-coil και humbucker",
        lesson_objective="Να ακούει ο μαθητής τη διαφορά και να ξέρει πότε να διαλέξει το καθένα.",
        module_title="Ο ήχος της κιθάρας",
        module_objective="Πώς διαμορφώνεται ο ήχος πριν φτάσει στον ενισχυτή.",
        course_title="Πώς να βγάλω καλό ήχο από την κιθάρα μου",
        tier=TIER_LIBRARY,
        position="lesson 1 of 4 in module 1 of 5",
        minutes=50, teaching_minutes=40, target_words=2200, floor_words=1760,
    )

    lesson, m = draft_lesson(app_db, ctx=ctx, library=library, language="el")

    print(f"\nLESSON: {lesson['title']!r}")
    print(f"  {m.total_words} words (target {m.target}, floor {m.floor}) — "
          f"meets_floor={m.meets_floor}")
    for name in SECTIONS:
        cites = lesson[name].get("citations") or []
        print(f"  {name}: {m.per_section[name]} words, "
              f"{len(cites)} citation(s) {[(c['source_id'], c['page']) for c in cites]}")
    usage = getattr(provider, "last_usage", {})
    print(f"  usage: {usage}")

    assert m.meets_floor, f"{m.total_words} words is under the {m.floor}-word floor"

    body = " ".join(lesson[name].get("body") or "" for name in SECTIONS)
    greek = sum(1 for ch in body if "Ͱ" <= ch <= "Ͽ" or "ἀ" <= ch <= "῿")
    assert greek / max(1, len(body)) > 0.5, "the lesson did not come back in Greek"

    assert invalid_citations(lesson, library) == [], "a cited page must actually exist"
    assert any(lesson[name].get("citations") for name in SECTIONS), (
        "a library-tier lesson that cites NOTHING is not grounded in his book"
    )

    # THE CACHE. This is the second call of the run (the draft's own retry aside),
    # so the 90K-token prefix must be READ, not re-written. If this is 0 we are
    # paying 10x and the only symptom is the invoice.
    assert usage.get("cache_read_input_tokens", 0) > 0, (
        "cache_read_input_tokens is ZERO — the library prefix is not stable, so "
        "every one of the 20 lesson drafts is re-writing the whole book at 1.25x "
        "instead of reading it at 0.1x. Nothing looks broken. It just costs ~$7 "
        "instead of ~$2.72."
    )
