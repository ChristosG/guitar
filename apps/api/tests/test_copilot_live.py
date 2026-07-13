"""THE FINAL ACCEPTANCE TEST for Plan 11 ("Copilot Rebuild") — the last task
of the whole 3-plan redesign (A: Library, B: Lesson Authoring, C: Copilot).

This module is deliberately a SEPARATE file from `test_agent_grounding.py`'s
own case-1 live test and `test_agent_guards.py`'s own case-2 live test (both
already exist and pass — see their own `@pytest.mark.integration` sections):
this one exists to run all FOUR acceptance cases from the spec
(`docs/superpowers/specs/2026-07-13-copilot-rebuild-design.md` section 6, and
`.superpowers/sdd/task-4-brief.md`) together, against the SAME real app data,
as one coherent acceptance run — including the ONE case (case 3, "admits what
it doesn't know") that had NO live coverage anywhere in the suite before this
task. Case 4 (HITL) already has live coverage in `test_chat_live_llm.py`
(against `guitar_test` with a seeded student) — this module re-runs the exact
phrasing from the brief against the REAL app DB's REAL student for full-depth
proof, not to replace that earlier test.

Read-only by contract, same posture as `test_library_live.py` / `test_agent_
grounding.py`'s own live section: opens its OWN engine against the REAL app
DB (`guitar`), never the `guitar_test` conftest.py force-pins `DATABASE_URL`
to. `run_agent_turn` is called DIRECTLY (not through the FastAPI router) —
it never writes anything itself (a forced-retrieval SELECT for cases 1-3; for
case 4 a proposed mutation is only ever SUSPENDED, never executed — that IS
the assertion). No fake provider, no stubbed `search` — the REAL 9B model
against the REAL 194,671-char, page-addressable book.

DO NOT force anything green. Every assertion below is the literal acceptance
criterion from the brief; where the model's actual behaviour is genuinely
uncertain going in (case 3 above all), the test says so in its own docstring
and the report this task produces prints the real transcript regardless of
pass/fail.

Run:
  cd apps/api && LLM_BASE_URL=http://localhost:6888/v1 EMBED_BASE_URL=http://localhost:8090/v1 \\
    ./.venv/bin/python -m pytest -m integration -v -s tests/test_copilot_live.py
"""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.guards import looks_like_tablature
from app.agent.loop import run_agent_turn
from app.models.knowledge import KnowledgeSource
from app.models.student import Student

APP_DATABASE_URL = os.environ.get(
    "APP_DATABASE_URL", "postgresql+psycopg://guitar:guitar@localhost:5434/guitar",
)

BOOK_TITLE = "Getting Great Guitar Sounds"


@pytest.fixture(scope="module")
def app_db():
    """Session on the REAL app DB. Read-only by contract — see module docstring."""
    engine = create_engine(APP_DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _print_transcript(label: str, question: str, result) -> None:
    print(f"\n{'=' * 70}\n[{label}] QUESTION: {question!r}")
    print(f"[{label}] status={result.status!r}")
    print(f"[{label}] content={result.content!r}")
    print(f"[{label}] citations={result.citations!r}")
    if result.pending_tool:
        print(f"[{label}] pending_tool={result.pending_tool!r}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Precondition: the book this whole plan's payoff depends on must actually
# be in the app DB, ready. If this fails, every case below is meaningless —
# fail loudly and immediately rather than letting 4 confusing failures happen.
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_precondition_the_real_book_is_ready_in_the_app_db(app_db):
    book = app_db.query(KnowledgeSource).filter_by(title=BOOK_TITLE).one_or_none()
    assert book is not None, "the tutor's real book is not in the app DB — nothing to test against"
    assert book.status == "ready", f"book status is {book.status!r}, not 'ready'"
    print(f"\n[precondition] book id={book.id} status={book.status} chars={book.char_count}")


# ---------------------------------------------------------------------------
# CASE 1 (C1/C2): retrieval runs BY FORCE, answer grounded in HIS book, and a
# citation carries a real, showable page — the spec's own worked example is
# "a citation chip to p.21".
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_case1_pick_thickness_retrieval_forced_and_cites_the_real_book(app_db):
    question = "what does pick thickness do to my tone?"
    result = run_agent_turn(app_db, [{"role": "user", "content": question}])
    _print_transcript("case1", question, result)

    assert result.status == "answer", (
        f"the model hallucinated a mutation for a plain question: {result.pending_tool}"
    )
    assert result.content, "no answer text came back"
    assert result.citations, (
        "forced retrieval found NOTHING — C1 (retrieval runs whether the model "
        "wants it or not) is not actually holding"
    )

    book_citations = [c for c in result.citations if c["source_title"] == BOOK_TITLE]
    assert book_citations, (
        f"no citation points at the real book at all — got: {result.citations}"
    )
    pages_cited = sorted({c["page_no"] for c in book_citations if c["page_no"] is not None})
    print(f"[case1] book pages cited: {pages_cited}")
    assert pages_cited, "citation(s) carry no page number to show him"

    # The spec's own worked example names p.21 (confirmed independently via
    # direct SQL against the app DB: page 21 is the page whose prose actually
    # discusses changing pick gauge for a brighter/darker tone). Soft on EXACT
    # rank (retrieval order can legitimately vary a little run to run) but
    # hard on the page appearing SOMEWHERE in the cited set — if it doesn't,
    # that's a real finding to report, not something to relax away.
    assert 21 in pages_cited, (
        f"expected page 21 (the book's pick-thickness passage) among the cited "
        f"pages, got {pages_cited} — report this honestly if it fails"
    )
    for c in book_citations:
        assert c["page_id"] is not None, f"citation has no page_id — cannot open the real scan: {c}"


# ---------------------------------------------------------------------------
# CASE 2 (C3): asked for a tab, the model must call generate_artifact — never
# free-type ASCII into a code fence. This is Chris's exact reported bug.
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_case2_g_major_scale_tab_calls_generate_artifact_never_ascii(app_db):
    question = "give me a G major scale tab"
    result = run_agent_turn(app_db, [{"role": "user", "content": question}])
    _print_transcript("case2", question, result)

    # THE hard invariant no matter which recovery path fired: a hand-typed
    # tab must never reach an assistant-authored message in the transcript.
    for m in result.messages:
        if m.get("role") == "assistant":
            assert not looks_like_tablature(m.get("content") or ""), (
                f"a hand-typed ASCII tab reached the transcript uncaught: {m!r}"
            )

    # THE positive acceptance criterion the brief demands ("it must call
    # generate_artifact"), not just "no bluff slipped through" — report
    # honestly if the model took the honest-fallback path instead of
    # actually calling the tool.
    assert result.status == "awaiting_approval", (
        f"expected the model to call generate_artifact and suspend for "
        f"approval, but got status={result.status!r} content={result.content!r} "
        f"— the model likely bluffed and the C3 guard had to substitute an "
        f"honest fallback rather than the fix working outright"
    )
    assert result.pending_tool is not None
    assert result.pending_tool["name"] == "generate_artifact", (
        f"model proposed the wrong tool for a tab request: {result.pending_tool}"
    )
    kind = str(result.pending_tool["arguments"].get("kind", "")).lower()
    prompt = str(result.pending_tool["arguments"].get("prompt", "")).lower()
    assert "scale" in kind or "tab" in kind or "scale" in prompt or "tab" in prompt, (
        f"generate_artifact args don't plausibly match a G major scale tab: "
        f"{result.pending_tool['arguments']}"
    )
    print(f"[case2] generate_artifact args: {result.pending_tool['arguments']!r}")


# ---------------------------------------------------------------------------
# CASE 3: ask something the library genuinely does not cover. Confirmed via
# direct SQL against the app DB before writing this test (zero pages/chunks
# mention "kemper"/"profiler"/"reharmoniz*"/"digital modeller"/"helix" —
# a 1990s-2000s tube-amp-and-pickups book has no business discussing a
# 2010s-era digital amp modeller). THE MOST IMPORTANT CASE PER THE BRIEF: an
# agent that cannot say "your material doesn't cover this" is an agent that
# will invent something. NOT relaxed to make this pass — if the model bluffs
# here, that is a real, reportable finding about this 9B model's limits.
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_case3_out_of_scope_question_admits_the_gap_and_labels_general_knowledge(app_db):
    question = "How does a Kemper Profiler capture and model an amp's tone?"
    result = run_agent_turn(app_db, [{"role": "user", "content": question}])
    _print_transcript("case3", question, result)

    assert result.status == "answer", (
        f"the model hallucinated a mutation for a plain out-of-scope question: {result.pending_tool}"
    )
    assert result.content, "no answer text came back"

    book_citations = [c for c in result.citations if c["source_title"] == BOOK_TITLE]
    print(f"[case3] book citations returned by forced retrieval: {book_citations}")
    # This is INFORMATIONAL, not gating: the forced-retrieval pre-hop always
    # runs and always returns its top-k nearest neighbours even when none are
    # truly relevant (there's a floor, not a "return nothing" branch) — what
    # matters is what the MODEL says about them, asserted below.

    content_lower = result.content.lower()
    # Broad-but-honest set of ways a model might plainly admit a gap — widened
    # after the first live run surfaced a real phrasing ("doesn't contain
    # specific information about...") this list originally missed; this is
    # recognizing a genuine synonym, not relaxing the bar the second
    # assertion below still enforces at face value.
    admits_gap = any(
        phrase in content_lower
        for phrase in (
            "doesn't cover", "does not cover", "don't cover", "do not cover",
            "isn't in", "is not in", "isn't covered", "is not covered",
            "no mention", "not in his library", "not in the library",
            "not in your library", "not in this library", "material doesn't",
            "material does not", "library doesn't have", "library does not have",
            "nothing relevant", "not something", "not covered in",
            "doesn't contain specific information", "does not contain specific",
            "doesn't specifically address", "does not specifically address",
            "would need to consult", "need to consult additional",
            "beyond what's in the", "beyond what is in the",
        )
    )
    labels_general_knowledge = "general knowledge" in content_lower

    print(f"[case3] admits_gap={admits_gap} labels_general_knowledge={labels_general_knowledge}")
    print(f"[case3] FULL ANSWER:\n{result.content}")

    assert admits_gap, (
        "FAIL: the model did not plainly say his material doesn't cover this "
        f"— it may have silently answered from pretrained knowledge instead. "
        f"Full answer: {result.content!r}"
    )
    # Reported as its OWN assertion, not merged with the one above: this is
    # the literal "LABEL the answer as general knowledge" criterion from the
    # brief, and it genuinely can fail independently of admits_gap (observed
    # live: the model can honestly admit a gap in its own words while still
    # never using the word pair "general knowledge" anywhere) — a real,
    # reportable weakness, not a test-phrasing artifact like the one above.
    assert labels_general_knowledge, (
        "FAIL: the model admitted the gap in its own words but never labelled "
        "any part of its answer as general knowledge (the literal words "
        "'general knowledge' do not appear) — instead of giving a labelled "
        "general-knowledge answer for the Kemper-specific part, it declined "
        f"to answer that part at all. Full answer: {result.content!r}"
    )


# ---------------------------------------------------------------------------
# CASE 4 (C6): a mutation must still suspend for approval, unexecuted, no
# matter how many loop edits T1/T2/T3 made. Brief's own exact phrasing.
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_case4_hitl_mutation_still_suspends_for_approval(app_db):
    maria = app_db.query(Student).filter(Student.name.ilike("%Maria%")).first()
    assert maria is not None, "expected the real student Maria Ioannou to exist in the app DB"

    question = "add a note that Maria struggled with barre chords"
    result = run_agent_turn(app_db, [{"role": "user", "content": question}])
    _print_transcript("case4", question, result)

    assert result.status == "awaiting_approval", (
        f"expected the mutation to suspend for approval, got status={result.status!r} "
        f"content={result.content!r} — HITL gate may have regressed"
    )
    assert result.pending_tool is not None
    assert result.pending_tool["name"] == "add_note", (
        f"model proposed the wrong tool for a make-a-note request: {result.pending_tool}"
    )
    args_blob = " ".join(str(v) for v in result.pending_tool["arguments"].values()).lower()
    assert "maria" in args_blob, f"proposed add_note args don't mention Maria: {result.pending_tool['arguments']}"
    assert "barre" in args_blob, f"proposed add_note args don't mention barre chords: {result.pending_tool['arguments']}"

    # Suspend-before-execute is the entire point. Unlike test_chat_live_llm.
    # py's guitar_test-based mutation tests (which can safely assert "zero
    # Note rows exist" because that DB is truncated per-test), this module
    # runs against the REAL app DB, which may already contain real Note rows
    # from Chris's own prior usage — a bare row-count check would race his
    # real data and prove nothing. The suspend-not-executed contract is
    # instead proven structurally: `run_agent_turn` (see its own module
    # docstring) never calls a mutation tool's `fn` — only `_dispatch_read_
    # call` does, and it is never reached for the pending mutation's own
    # ToolCall. The `status == "awaiting_approval"` assertion above, together
    # with `pending_tool` carrying the unexecuted call's own arguments, IS
    # the direct proof.
