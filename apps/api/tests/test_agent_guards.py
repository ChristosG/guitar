"""Unit tests for Plan 11 Task 2:
  - `app.agent.guards.looks_like_tablature` (C3) — the exact detector spec'd
    in `.superpowers/sdd/task-2-brief.md`, plus a false-positive sweep.
  - the post-turn guard wired into `app.agent.loop.run_agent_turn`: a
    provider that free-types Chris's exact bluff must never let that ASCII
    reach `AgentResult.content` or `AgentResult.messages`.
  - `app.agent.tools.TOOLS["find_lesson"]` (C5) — resolve a lesson by a
    partial, case-insensitive title match.

The loop test mirrors `test_agent_loop.py`'s own `_FakeProvider`/`_stub_tool`
pattern (file-private there, duplicated here rather than cross-imported —
this codebase's own established precedent, e.g. `test_agent_hitl.py`'s
identical duplication and `routers/artifacts.py`'s `_get_block_or_404`
docstring for why).
  - a live-LLM check (`@pytest.mark.integration`): what the REAL model
    actually does now when asked Chris's exact question.
"""
import pytest

import app.agent.loop as agent_loop
from app.agent.guards import looks_like_tablature
from app.agent.loop import AgentResult, run_agent_turn
from app.agent.tools import TOOLS
from app.llm.tools_types import AssistantTurn
from app.models.block import Block

# Chris's exact bluff (from the live chat, pasted into the brief verbatim) —
# a fenced block of six string lines, each `letter|dashes-and-digits`.
_CHRIS_BLUFF = (
    "Here's a G major scale:\n"
    "```\n"
    "e|-----0-2-4-5-7-8-10-\n"
    "B|-----0-2-4-5-7-8-10-\n"
    "G|-----0-2-4-5-7-8-10-\n"
    "D|-----0-2-4-5-7-8-10-\n"
    "A|-----0-2-4-5-7-8-10-\n"
    "E|3-5-7-8-10-12-14-15-\n"
    "```"
)


# ---------------------------------------------------------------------------
# looks_like_tablature
# ---------------------------------------------------------------------------

def test_detects_the_exact_bluff_chris_got():
    bluff = "Here's a G major scale:\n```\ne|-----0-2-4-5-7-8-10-\nB|-----0-2-4-5-7-8-10-\n```"
    assert looks_like_tablature(bluff)


def test_detects_the_full_six_line_bluff():
    assert looks_like_tablature(_CHRIS_BLUFF)


def test_detects_string_lines_outside_a_fence_too():
    # A model could just as easily type the tab as bare prose, no fence.
    bare = "Here you go:\ne|-----0-2-4-5-7-8-10-\nB|-----0-2-4-5-7-8-10-\nEnjoy!"
    assert looks_like_tablature(bare)


def test_a_single_string_line_alone_is_not_enough():
    # One matching line could be coincidence — the detector requires >=2.
    assert not looks_like_tablature("e|-----0-2-4-5-7-8-10-")


def test_does_not_false_positive_on_ordinary_prose_or_code():
    assert not looks_like_tablature("Use a heavier pick for a darker tone.")
    assert not looks_like_tablature("```python\nx = [0, 2, 4]\n```")


def test_does_not_false_positive_on_a_markdown_table():
    table = (
        "| String | Fret |\n"
        "|---|---|\n"
        "| E | 3 |\n"
        "| A | 5 |\n"
    )
    assert not looks_like_tablature(table)


def test_does_not_false_positive_on_a_bare_dash_divider():
    # No digits at all — a horizontal rule, not a tab.
    assert not looks_like_tablature("above\n---\nbelow")


def test_empty_and_none_are_falsy():
    assert not looks_like_tablature("")
    assert not looks_like_tablature(None)


# ---------------------------------------------------------------------------
# the loop's post-turn guard — the bluff must never reach the tutor
# ---------------------------------------------------------------------------

class _FakeProvider:
    """Mirrors `test_agent_loop.py`'s `_FakeProvider` exactly — see that
    module's own docstring. `chat_tools` returns the next scripted `turns`
    entry each call, repeating the last one once exhausted (so a provider
    that keeps bluffing forever is easy to script with one entry).
    """

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls: list[list[dict]] = []

    def chat_tools(self, messages, tools, *, tool_choice="auto", temperature=0.3):
        self.calls.append([dict(m) for m in messages])
        turn = self._turns[min(len(self.calls) - 1, len(self._turns) - 1)]
        if isinstance(turn, Exception):
            raise turn
        return turn


def test_a_free_typed_tab_never_reaches_the_tutor(db, monkeypatch):
    """The model types a tab instead of calling generate_artifact, and keeps
    doing it every time it's re-prompted (the fake provider repeats its last
    scripted turn forever). The guard must intercept it — the tutor must NOT
    be shown the hand-typed (and wrong) tab, in `content` or anywhere in
    `messages`, no matter how many internal retries the loop makes.
    """
    fake_provider = _FakeProvider([AssistantTurn(content=_CHRIS_BLUFF, tool_calls=[])])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    # "give me a G major scale tab" matches `_ENTITY_OR_ARTIFACT_RE`
    # ("scales?") so C1's forced-retrieval pre-hop does NOT fire — no need
    # to stub `search` here (mirrors `test_agent_grounding.py`'s own
    # documented split between content-bearing questions and artifact/entity
    # instructions).
    messages = [{"role": "user", "content": "give me a G major scale tab"}]
    result = run_agent_turn(db, messages)

    assert isinstance(result, AgentResult)
    assert result.status == "answer"
    assert "e|-----0-2-4-5-7-8-10-" not in (result.content or "")
    for m in result.messages:
        assert "e|-----0-2-4-5-7-8-10-" not in str(m.get("content") or "")
    assert "E|3-5-7-8-10-12-14-15-" not in str(result.messages)


def test_the_bluff_gets_a_bounded_re_prompt_not_an_unbounded_retry(monkeypatch):
    """The guard's recovery must be BOUNDED, same "cap it" spirit as the
    ToolArgsError repair loop — a provider that bluffs forever must not spin
    `max_steps` times without ever returning; it should give up with an
    honest fallback well before the step cap.
    """
    fake_provider = _FakeProvider([AssistantTurn(content=_CHRIS_BLUFF, tool_calls=[])])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    messages = [{"role": "user", "content": "give me a G major scale tab"}]
    result = run_agent_turn(None, messages, max_steps=6)

    assert result.status == "answer"
    assert result.content  # some honest fallback was returned, not empty
    assert "e|" not in result.content.lower()
    # Gave up well short of exhausting max_steps (proves it's a small bounded
    # retry, not "kept trying until the step cap saved it").
    assert len(fake_provider.calls) < 6


# ---------------------------------------------------------------------------
# find_lesson (C5)
# ---------------------------------------------------------------------------

def test_find_lesson_resolves_by_title(db):
    lesson = Block(kind="lesson", title="Pick Gauge and Tone", language="en")
    db.add(lesson)
    db.commit()
    session1 = Block(kind="session", title="Session 1", parent_id=lesson.id, order=0, language="en")
    session2 = Block(kind="session", title="Session 2", parent_id=lesson.id, order=1, language="en")
    db.add_all([session1, session2])
    db.commit()

    results = TOOLS["find_lesson"].fn(db, title_query="pick gauge")

    assert any(r["id"] == lesson.id and r["title"] == "Pick Gauge and Tone" for r in results)
    match = next(r for r in results if r["id"] == lesson.id)
    assert [s["title"] for s in match["sessions"]] == ["Session 1", "Session 2"]
    assert [s["order"] for s in match["sessions"]] == [0, 1]


def test_find_lesson_is_case_insensitive_and_partial(db):
    lesson = Block(kind="lesson", title="Pick Gauge and Tone", language="en")
    db.add(lesson)
    db.commit()

    results = TOOLS["find_lesson"].fn(db, title_query="PICK GAUGE")

    assert any(r["id"] == lesson.id for r in results)


def test_find_lesson_no_match_returns_empty_list(db):
    results = TOOLS["find_lesson"].fn(db, title_query="does not exist anywhere")
    assert results == []


def test_find_lesson_is_registered_as_a_read_tool():
    entry = TOOLS["find_lesson"]
    assert entry.kind == "read"
    assert entry.schema["type"] == "function"
    fn_schema = entry.schema["function"]
    assert fn_schema["name"] == "find_lesson"
    assert fn_schema["description"]
    assert fn_schema["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# THE moment-of-truth check: what does the REAL model do now?
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_live_model_asked_chris_exact_question_g_major_scale_tab(db):
    """Drives the REAL model (no scripted/mocked provider — same
    "deliberately does NOT monkeypatch get_provider" posture
    `test_chat_live_llm.py` states for its own live checks) through Chris's
    EXACT question: "give me a G major scale tab". Reports (does not gate
    on) which path the model actually took:
      - `status == "awaiting_approval"`, `pending_tool["name"] ==
        "generate_artifact"` — the fix worked outright, the model calls the
        real, playable renderer now.
      - `status == "answer"` with an ASCII-free fallback — the model still
        tried to free-type a tab and the guard caught it; the product is
        saved by the guard rather than the prompt change alone.

    HARD-asserts only the one invariant this whole task exists for: no
    matter which path the model took, no assistant-authored message in the
    result can possibly read as a hand-typed tab (`looks_like_tablature`) —
    the guard's own detector re-used here as the checker, so this assertion
    fails if the guard ever let a bluff slip through live, not just against
    the scripted fakes above.
    """
    messages = [{"role": "user", "content": "give me a G major scale tab"}]
    result = run_agent_turn(db, messages)

    print(
        f"\n[live-llm G-major-scale-tab] status={result.status!r} "
        f"pending_tool={result.pending_tool!r} content={result.content!r}"
    )

    if result.status == "awaiting_approval":
        print(
            "[live-llm G-major-scale-tab] FINDING: model called "
            f"{result.pending_tool['name']!r} — generate_artifact means the "
            "fix worked outright (no bluff to catch)."
            if result.pending_tool and result.pending_tool.get("name") == "generate_artifact"
            else "[live-llm G-major-scale-tab] FINDING: model proposed an "
            "UNEXPECTED mutation for a tab request."
        )
    else:
        print(
            "[live-llm G-major-scale-tab] FINDING: model did not call a "
            "tool (status='answer') — check above whether guards.py's "
            "reprompt got a real generate_artifact call on retry, or the "
            "honest fallback had to be substituted."
        )

    for m in result.messages:
        if m.get("role") == "assistant":
            assert not looks_like_tablature(m.get("content") or ""), (
                f"a hand-typed tab reached the transcript uncaught: {m!r}"
            )
