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

Plan 12 Task 5 (G5) adds tests for `app.agent.guards.
looks_like_named_song_request` — Chris's OTHER bug: asked for the "Smells
Like Teen Spirit" riff and got one note repeated seven times, because the
model cannot actually recall a specific copyrighted recording and invents
instead. `run_agent_turn` wires this in as a PRE-model short-circuit (mirrors
C1's forced-retrieval pre-hop shape): a named-song tab/riff/solo request
never even reaches the model — it gets an honest decline that names a real
alternative, straight away.
"""
import pytest

import app.agent.loop as agent_loop
from app.agent.guards import (
    NAMED_SONG_DECLINE_MESSAGE,
    looks_like_named_song_request,
    looks_like_tablature,
)
from app.agent.loop import AgentResult, run_agent_turn
from app.agent.tools import TOOLS
from app.llm.tools_types import AssistantTurn, ToolCall
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
# Plan 12 Task 5 (G5): looks_like_named_song_request
# ---------------------------------------------------------------------------
#
# THE DETECTOR: NOT a hardcoded song list (useless — misses every song not on
# it). Instead, an inverted, closed vocabulary of GENERIC music terms (note
# names, scale/mode names, chord/progression words, technique words, generic
# genre words, plus stopwords) — see `guards.py`'s own docstring for the
# full list and rationale. A request only trips this detector when it BOTH
# (a) names a riff/solo/lick/tab/intro/outro/chorus/bridge/verse — i.e. it's
# actually asking for a piece of music, not e.g. "an exercise" alone — AND
# (b) contains at least one word that ISN'T in that generic vocabulary — a
# word that can only be naming something specific (a song, a band, a title),
# since every legitimate generic music-theory word is already in the list.

_DECLINE_EXAMPLES = [
    "smells like teen spirit riff tabs",
    "tab out the intro to Stairway to Heaven",
    "the solo from Comfortably Numb",
    # Greek: a named song is a named song in the default locale too.
    "tab για το Smoke on the Water",
    "το σόλο από το Comfortably Numb",
]

_WORKS_EXAMPLES = [
    "give me a G major scale tab",
    "a blues shuffle in E",
    "a 12-bar blues progression",
    "an exercise for alternate picking",
    # Greek: generic requests in the product's own language. The first is the
    # exact Greek twin of the pinned English clause-split regression below —
    # the splitter ran on English conjunctions only, so «και» never split and
    # «μαθητές» (an unrelated clause's word) counted as song-title evidence.
    "δείξε μου τους μαθητές και φτιάξε μια ταμπλατούρα για την πεντατονική κλίμακα",
    "φτιάξε μια ταμπλατούρα για την πεντατονική κλίμακα",
    "ένα riff δωδεκάμετρου μπλουζ",
    "γράψε μου ένα tab για ζέσταμα",
]


@pytest.mark.parametrize("text", _DECLINE_EXAMPLES)
def test_looks_like_named_song_request_detects_named_song_asks(text):
    assert looks_like_named_song_request(text)


@pytest.mark.parametrize("text", _WORKS_EXAMPLES)
def test_looks_like_named_song_request_does_not_false_positive_on_generic_asks(text):
    assert not looks_like_named_song_request(text)


def test_looks_like_named_song_request_empty_and_none_are_falsy():
    assert not looks_like_named_song_request("")
    assert not looks_like_named_song_request(None)


def test_looks_like_named_song_request_does_not_flag_an_unrelated_clause():
    """Regression: a compound instruction can chain an unrelated command
    (containing words that are neither generic-music-term NOR song-name
    evidence, e.g. "students") onto a genuinely generic tab request in the
    SAME turn — `test_agent_hitl.py`'s own
    `test_read_and_mutation_in_the_same_turn_...` test drives exactly this
    phrase through the loop and expects the mutation to actually reach the
    model. Clause-splitting (this module's own docstring) is what keeps the
    word-check scoped to the clause that actually names the request.
    """
    assert not looks_like_named_song_request("list students and make a tab")


def test_named_song_decline_message_offers_a_real_alternative():
    """The brief's own explicit requirement: "do not just say no — a tutor
    asked for something and deserves a useful alternative." Pins that the
    decline names concrete things the agent CAN actually do (a chord
    progression, a scale, a technique exercise, the riff's rhythmic shape) —
    not a bare refusal.
    """
    lowered = NAMED_SONG_DECLINE_MESSAGE.lower()
    assert "chord progression" in lowered
    assert "scale" in lowered
    assert "exercise" in lowered
    # Also honest about WHY, not just WHAT it can't do.
    assert "copyright" in lowered or "recording" in lowered or "memorized" in lowered


# ---------------------------------------------------------------------------
# Plan 12 Task 5 (G5): the loop's pre-model short-circuit
# ---------------------------------------------------------------------------

class _ProviderThatMustNotBeCalled:
    """A named-song decline must short-circuit BEFORE the model is ever
    called — there is no reliable way to make the model itself decline
    (it's the very thing that fabricates), so the guard must intercept the
    user's own turn, the same "pre-hop, not a post-hoc check" shape C1's
    forced-retrieval already established. Any call here is a hard failure.
    """

    def chat_tools(self, messages, tools, *, tool_choice="auto", temperature=0.3):
        raise AssertionError("the model must never be called for a named-song request")


@pytest.mark.parametrize("text", _DECLINE_EXAMPLES)
def test_run_agent_turn_declines_a_named_song_request_without_calling_the_model(text, monkeypatch):
    monkeypatch.setattr(agent_loop, "get_provider", lambda: _ProviderThatMustNotBeCalled())

    messages = [{"role": "user", "content": text}]
    result = run_agent_turn(None, messages)

    assert result.status == "answer"
    assert result.content == NAMED_SONG_DECLINE_MESSAGE
    assert "chord progression" in result.content.lower()
    # The decline is recorded in history too (Task 4's router persists
    # `messages`, not just `content`).
    assert result.messages[-1]["content"] == NAMED_SONG_DECLINE_MESSAGE


@pytest.mark.parametrize("text", _WORKS_EXAMPLES)
def test_run_agent_turn_still_calls_generate_artifact_for_generic_requests(text, monkeypatch):
    """FALSE POSITIVES ARE THEIR OWN BUG (the brief's own words). Each of
    these generic musical-object requests must still reach the model and
    still be able to propose `generate_artifact` — proving the G5 guard
    doesn't collaterally suppress the exact legitimate case Plan 11 already
    fixed (a real scale/chord/exercise tab).
    """
    call = ToolCall(
        id="call_1", name="generate_artifact",
        arguments={"kind": "tab", "prompt": text},
    )
    turn = AssistantTurn(content=None, tool_calls=[call])
    fake_provider = _FakeProvider([turn])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    messages = [{"role": "user", "content": text}]
    result = run_agent_turn(None, messages)

    assert result.status == "awaiting_approval"
    assert result.pending_tool["name"] == "generate_artifact"
    assert len(fake_provider.calls) == 1  # the model WAS called — no short-circuit


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


@pytest.mark.integration
def test_live_model_asked_chris_exact_question_teen_spirit_riff(db):
    """Drives Chris's EXACT other bug through the real system: "generate me
    smells like teen spirit riff TABS". Unlike the G-major-scale-tab live
    check above, this one does NOT actually need to reach the live model to
    pass — the whole point of the G5 fix is that `run_agent_turn`'s new
    pre-model short-circuit (`looks_like_named_song_request`) intercepts a
    named-song ask BEFORE the model ever sees it, so no scripted/mocked
    provider is needed for this to be a genuine "what does the real system
    do now" check: `get_provider()` is deliberately left un-monkeypatched
    (same posture as the sibling test above) so that if the guard's
    detection regex is ever loosened to miss this exact phrase, this test
    would fall through to the REAL model and could then reproduce Chris's
    actual bug (a fabricated, degenerate tab) instead of silently passing.

    HARD-asserts the product-level invariant this task exists for: Chris's
    exact phrasing gets an honest decline that names a real alternative, and
    — belt and suspenders with the G-major-scale-tab check above — no
    assistant message anywhere in the transcript can read as a fabricated
    hand-typed tab.
    """
    messages = [{"role": "user", "content": "generate me smells like teen spirit riff TABS"}]
    result = run_agent_turn(db, messages)

    print(
        f"\n[live-llm teen-spirit-riff] status={result.status!r} "
        f"pending_tool={result.pending_tool!r} content={result.content!r}"
    )

    assert result.status == "answer"
    assert result.content == NAMED_SONG_DECLINE_MESSAGE
    print(
        "[live-llm teen-spirit-riff] FINDING: the G5 pre-model guard "
        "intercepted this request before the model was ever called — an "
        "honest, deterministic decline naming a real alternative, instead "
        "of the one-note-repeated fabrication Chris got live."
    )

    for m in result.messages:
        if m.get("role") == "assistant":
            assert not looks_like_tablature(m.get("content") or ""), (
                f"a hand-typed tab reached the transcript uncaught: {m!r}"
            )


# ---------------------------------------------------------------------------
# Task 8: search the library before declining a named song
# ---------------------------------------------------------------------------
#
# The guard is a pre-model short-circuit at (old) loop.py:601. The forced-
# retrieval pre-hop is at (old) loop.py:608. So it declined BEFORE the
# library was searched — and a transcription the tutor OWNS, on a real page,
# with a real citation available, was refused unread. That is the actual
# complaint behind "those books are copywrited, but i bought them and
# they're mine". It is an ordering bug, not a policy: reorder, don't remove.

def test_a_song_the_tutor_OWNS_is_answered_not_declined(db, monkeypatch):
    """His own book HAS this transcription (a mocked `search()` hit) — the
    decline must not fire, and the answer must carry a citation to the real
    page. `get_provider()` IS mocked here (unlike the sibling live-model
    checks above): once the reorder is in place this path falls through to
    an actual model call, and this suite must stay offline/deterministic —
    the live vLLM host is unreachable outside the compose network (see
    `conftest.py::_no_live_retrieval`). A scripted `_FakeProvider` mirrors
    this file's own established pattern.
    """
    hit = type("H", (), {
        "source_id": "s1", "source_title": "Real Book", "page": 42, "page_id": None,
        "text": "Intro riff: E5 G5 A5", "score": 0.9,
    })()
    monkeypatch.setattr("app.agent.loop.search", lambda *a, **kw: [hit])
    monkeypatch.setattr("app.agent.loop.NAMED_SONG_DECLINE_MESSAGE", "DECLINED")
    fake_provider = _FakeProvider(
        [AssistantTurn(content="Here's the intro riff from page 42: E5 G5 A5", tool_calls=[])]
    )
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(db, [{"role": "user", "content": "give me the tab for Sweet Child O' Mine"}], locale="el")
    assert result.content != "DECLINED", "his own book was refused unread"
    assert result.citations, "an answer from his library must cite the page"


def test_a_song_the_tutor_does_NOT_own_is_still_declined(db, monkeypatch):
    """The guard's reason survives the reorder. The model cannot recall a specific
    recording's tab; removing the guard yields a confidently wrong one handed to a
    teacher, handed to a student — the same harm as an invalid citation."""
    monkeypatch.setattr("app.agent.loop.search", lambda *a, **kw: [])
    result = run_agent_turn(db, [{"role": "user", "content": "give me the tab for Sweet Child O' Mine"}], locale="el")
    assert result.content == NAMED_SONG_DECLINE_MESSAGE


def test_the_decline_no_longer_cites_copyright():
    """For a private, single-user, non-commercial app over books the tutor owns,
    copyright is noise. Accuracy is the real and sufficient reason."""
    assert "copyright" not in NAMED_SONG_DECLINE_MESSAGE.lower()
    assert "memorized" in NAMED_SONG_DECLINE_MESSAGE


# ---------------------------------------------------------------------------
# Task 1: strip_curriculum_context helper + sentinel constant
# ---------------------------------------------------------------------------

from app.agent.guards import (  # extend the module's existing import if present
    CURRICULUM_CONTEXT_SENTINEL,
    strip_curriculum_context,
)

_CTX_TAIL = (
    "\n\n" + CURRICULUM_CONTEXT_SENTINEL + " — this conversation is about "
    'curriculum abc titled "Guitar Tone & Amps".\nCurrent structure:\n'
    "[uuid-1] Intro to Tone — what makes an amp sing\n"
    "[uuid-2] Solo riffs and sustain — Gilmour-style bends]"
)


def test_strip_curriculum_context_removes_injected_tail():
    raw = "μπορείς να αφαιρέσεις όλα τα inline citations από αυτό το curricula;"
    assert strip_curriculum_context(raw + _CTX_TAIL) == raw


def test_strip_curriculum_context_is_identity_without_tail():
    assert strip_curriculum_context("plain question") == "plain question"
    assert strip_curriculum_context("") == ""


def test_enriched_text_trips_guard_but_stripped_text_does_not():
    # Pins the EXACT production bug: the raw Greek request is innocent, the
    # injected course tree (full of "Intro"/"Solo" titles) is what triggers.
    raw = "μπορείς να αφαιρέσεις όλα τα inline citations από αυτό το curricula;"
    assert looks_like_named_song_request(raw + _CTX_TAIL) is True   # the bug
    assert looks_like_named_song_request(strip_curriculum_context(raw + _CTX_TAIL)) is False
