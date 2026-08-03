"""THE ACCEPTANCE TEST for Lesson Authoring (Plan 10 Task 5).

Green unit tests (`test_lesson_draft.py`/`test_lesson_edit.py`/
`test_lesson_routes.py`) prove the code *runs*. This proves the payoff Plan
10 exists for: a passage the tutor actually selected in his actual book
becomes a REAL lesson, drafted by the REAL model, that a human can read and
recognize as being ABOUT that passage — and that the agent's `split_session`
tool (Plan 10 Task 3's HITL-gated wrapper) really mutates the DB when called,
not just when unit-tested against a throwaway fixture.

Same posture as `test_library_live.py` (Plan 9's acceptance test): runs
against the REAL APP DB (`guitar`) and the REAL local model, deliberately not
a fresh `guitar_test` fixture — conftest.py force-pins `DATABASE_URL` to
`guitar_test` and truncates every table after each test, so this module opens
its OWN engine/session against `guitar`; the autouse truncate fixture can
never reach it.

UNLIKE test_library_live.py, this module DOES write — drafting a lesson (and
splitting one of its sessions) is the whole point, and the task brief is
explicit that this is fine: "Creating a lesson in it is FINE and expected
(that's the demo)." What this module does NOT do: touch the book, its pages,
its collection, or the Course Spine source (see this file's own fixtures
below, which only ever `db.get`/read those). The ONE lesson this module
drafts is deliberately torn down at the end of the module (see
`_drafted_lesson`'s fixture teardown) — this run is a repeatable, automatable
proof of the code path, not the human-facing demo lesson; that one lives in
the app DB from the actual browser walk this task's brief also requires (see
`.superpowers/sdd/p10-task-5-report.md`), which this module's teardown never
touches (different `GenerationJob`/`Block` ids).

Run:
  cd apps/api && LLM_API_KEY=sk-ant-... \
    ./.venv/bin/python -m pytest -m integration -v -s tests/test_lesson_live.py
"""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.tools import TOOLS
from app.lessons.draft import draft_lesson_from_selection
from app.models.block import Block
from app.models.knowledge import KnowledgeSource

APP_DATABASE_URL = os.environ.get(
    "APP_DATABASE_URL", "postgresql+psycopg://guitar:guitar@localhost:5434/guitar"
)

# Module-level, because the expensive call happens in a MODULE-scoped fixture
# (`drafted_lesson`) — which is instantiated before conftest's function-scoped
# `_integration_needs_a_real_key` guard could skip anything, so without this
# the module ERRORs in fixture setup instead of skipping. Same rule as that
# guard: the conftest fallback key can only produce an auth failure, and a
# real key means real spend the operator must opt into.
if os.environ.get("LLM_API_KEY", "") in ("", "none", "sk-ant-test-suite-fallback-key"):
    pytest.skip(
        "live-model acceptance test — export a real LLM_API_KEY to run it "
        "(this drafts a real lesson and spends real tokens)",
        allow_module_level=True,
    )

BOOK_TITLE = "Getting Great Guitar Sounds"

# Page.page_no=21 of the real app DB's copy of this book — the PRINTED header
# on that page reads "23" (front-matter offset: the book's own numbered pages
# start after an unnumbered preface/TOC), which is why this is 21 here and
# not 23 (see this task's own brief for the same reconciliation).
PAGE_NO = 21

# The REAL pick-thickness/gauge passage from that exact page (verified via
# `GET /knowledge/sources/{id}/pages/21` against the live app DB before
# writing this test) — verbatim, not summarized/paraphrased, so grounding is
# actually being tested against the tutor's own book's wording, not a
# stand-in. Deliberately just the pick-related paragraphs (this page's later
# paragraphs pivot to strings, a different topic) — selecting a passage this
# focused makes the grounding assertion below meaningful: a lesson that
# ignores it and drifts to something else guitar-related is a genuine
# grounding failure, not a false positive from a page-sized passage that
# happens to mention several topics.
PICK_PASSAGE = (
    "Quite often players who are happy with the basic feel and tone of their "
    "instruments may still prefer the tone a little brighter. Changing from a "
    "heavy, to a medium or thin pick could give just the tonal change they are "
    "looking for. Conversely if you seek a thicker tone and have been using a "
    "thin pick, you may want to try switching to a heavier one. Changing pick "
    "gauge may involve some modification of your picking technique, but the "
    "resulting sound could be well worth it.\n\n"
    "Picks are offered in a wide variety of materials - different plastics, "
    "nylon, graphite, tortex, stone and metal. The type of material can "
    "affect the sound as much as the gauge and both can make as much "
    "difference a pickup change or a new body."
)

# Terms this exact passage actually supports — a genuinely grounded lesson
# must hit several of these; a generic guitar-tone lesson that ignores the
# passage (e.g. drifting to amps, pedals, tuning) would not.
GROUNDING_TERMS = ["pick", "gauge", "thick", "thin", "material", "tone"]


@pytest.fixture(scope="module")
def app_db():
    """Session on the REAL app DB. See module docstring for why this is its
    own engine, untouched by conftest.py's `guitar_test` truncation."""
    engine = create_engine(APP_DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(scope="module")
def book_source(app_db):
    """The tutor's real, already-OCR'd book — read-only lookup, never
    mutated by this module (mirrors `test_library_live.py`'s `app_db`
    fixture's own read-only contract for the book itself)."""
    book = app_db.query(KnowledgeSource).filter_by(title=BOOK_TITLE).one_or_none()
    assert book is not None, "the tutor's real book is not in the app DB — is this pointed at `guitar`?"
    return book


@pytest.fixture(scope="module")
def drafted_lesson(app_db, book_source):
    """Drafts ONE lesson from the REAL passage via the REAL model — shared by
    both tests below (grounding, then split) so the live LLM call (~30-60s)
    happens exactly once. Torn down at module end (see module docstring:
    this run's own artifact is disposable, unlike the browser walk's demo
    lesson)."""
    lesson_id = draft_lesson_from_selection(
        app_db, source_id=book_source.id, page_no=PAGE_NO, text=PICK_PASSAGE, language="en",
    )
    lesson = app_db.get(Block, lesson_id)
    assert lesson is not None
    yield lesson

    # Teardown: cascades onto every session/item Block via the FK's
    # `ondelete="CASCADE"` + the ORM's `cascade="all, delete-orphan"`
    # `children` relationship (see `app.models.block.Block`) — the book, its
    # pages, its collection, and the Course Spine source are never touched by
    # this delete (a lesson root has no FK from any of those).
    app_db.delete(lesson)
    app_db.commit()


@pytest.mark.integration
def test_the_drafted_lesson_is_grounded_in_the_real_passage(drafted_lesson, book_source):
    """THE ACCEPTANCE CRITERION (grounding half): draft a lesson from the
    REAL passage on the REAL page using the REAL model, then actually READ
    the result rather than asserting a shape and calling it done. If this
    reads as generic guitar filler that ignores the passage, that is a real
    finding to report honestly (see this task's own report), not something
    to force green.
    """
    lesson = drafted_lesson
    assert lesson.kind == "lesson"
    assert lesson.plane == "content"
    assert lesson.target_profile is not None
    provenance = lesson.target_profile.get("provenance")
    assert provenance == {"source_id": str(book_source.id), "page_no": PAGE_NO}

    sessions = sorted(lesson.children, key=lambda b: b.order)
    assert sessions, "drafted lesson has no sessions at all"
    for s in sessions:
        assert s.kind == "session"
        assert s.est_minutes and s.est_minutes > 0

    print(f"\n=== DRAFTED LESSON ===\nTitle: {lesson.title}")
    all_words: list[str] = [lesson.title]
    for s in sessions:
        print(f"\n  SESSION: {s.title}  ({s.est_minutes} min)")
        all_words.append(s.title)
        items = sorted(s.children, key=lambda b: b.order)
        assert items, f"session {s.title!r} was drafted with no items"
        for i in items:
            assert i.kind == "item"
            print(f"    - {i.title}\n      {i.body}")
            all_words.append(i.title or "")
            all_words.append(i.body or "")

    haystack = " ".join(all_words).lower()
    hits = [term for term in GROUNDING_TERMS if term in haystack]
    print(f"\nGROUNDING TERMS MATCHED: {hits} (of {GROUNDING_TERMS})")
    assert len(hits) >= 3, (
        "the drafted lesson does not read as grounded in the pick-thickness "
        f"passage — only matched {hits}. This is a genuine grounding failure, "
        "not a fixture problem; see the printed lesson above."
    )


@pytest.mark.integration
def test_split_session_via_the_agent_tool_path_actually_splits(app_db, drafted_lesson):
    """THE ACCEPTANCE CRITERION (split half): call the REGISTERED agent tool
    (`TOOLS["split_session"].fn` — the exact callable `routers/chat.py`'s
    resolve step dispatches to once a tutor approves the HITL card, Plan 10
    Task 3) against a REAL session of the just-drafted lesson, then verify
    the split actually happened by re-querying the DB — not by trusting the
    tool's own return value alone.
    """
    lesson = drafted_lesson
    sessions_before = sorted(lesson.children, key=lambda b: b.order)
    # Split whichever session has the most items — guarantees something
    # meaningful to partition regardless of how many sessions/items the live
    # model happened to draft this run (unlike the browser walk, which
    # targets literally "session 2" against one specific live draft).
    target = max(sessions_before, key=lambda s: len(s.children))
    items_before = sorted(target.children, key=lambda b: b.order)
    assert len(items_before) >= 1
    item_titles_before = [i.title for i in items_before]

    split_tool = TOOLS["split_session"].fn
    # `session_minutes=1`: a drafted item Block carries no `est_minutes` of
    # its own (`_persist_tree` never sets one — only sessions get one from
    # the model), so `split_session` treats each item as a 1-minute `Leaf`
    # (`Leaf(minutes=item.est_minutes or 1, ...)`). Asking for 1-minute
    # sessions forces `partition_by_minutes` to put each item in its own bin
    # regardless of how many items the live model happened to draft this
    # run — the only value that reliably produces >1 resulting session
    # whether this session has 2 items or 5.
    result = split_tool(app_db, session_id=str(target.id), session_minutes=1)

    assert "error" not in result, f"split_session tool returned an error: {result}"
    print(f"\nSPLIT TOOL RESULT: {result}")
    assert len(result["sessions"]) >= 2, "split_session did not actually produce multiple sessions"

    # Verify against the DB itself, independently of the tool's own return —
    # the old session id must be GONE, and the new session ids must be REAL,
    # REAL children of the SAME lesson, together carrying every original item.
    app_db.expire_all()
    old_session_still_there = app_db.get(Block, target.id)
    assert old_session_still_there is None, "the original over-long session was not actually removed"

    lesson_reloaded = app_db.get(Block, lesson.id)
    new_sessions = sorted(
        (s for s in lesson_reloaded.children if s.id in {r["id"] for r in result["sessions"]}),
        key=lambda b: b.order,
    )
    assert len(new_sessions) == len(result["sessions"])
    recombined_titles = [
        i.title for s in new_sessions for i in sorted(s.children, key=lambda b: b.order)
    ]
    assert recombined_titles == item_titles_before, (
        "the split lost, duplicated, or reordered an item — "
        f"before={item_titles_before} after={recombined_titles}"
    )
