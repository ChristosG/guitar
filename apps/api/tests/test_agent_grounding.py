"""Tests for the forced-retrieval pre-hop (Plan 11 Task 1, C1/C2):
`run_agent_turn` (`app/agent/loop.py`) must run `app.brain.retrieve.search`
itself, BEFORE the model's first call on a content-bearing turn, so the model
never gets the chance to skip retrieval the way `tool_choice="auto"` always
let it. Every hit gets injected as a GROUNDING context block, and
`AgentResult.citations` carries `{source_id, source_title, page_no, page_id,
snippet}` for each — persisted onto the `Message` row by the chat router
(`app/routers/chat.py`) via `app.agent.transcript.persist_new_messages`.

Same "pure unit test, scripted fake provider" pattern as `test_agent_loop.py`
/ `test_agent_hitl.py` (duplicated `_FakeProvider`/`_stub_tool` rather than
cross-imported, per this codebase's established precedent) — `db` here is
the REAL `guitar_test` session fixture (not `None`) since `search` is a real
function over real tables even though its result is stubbed per test. Every
`search`/`get_provider` call in these unit tests is monkeypatched — EXCEPT
the one `@pytest.mark.integration` test at the bottom of this file (mirrors
`test_library_live.py`'s `app_db` pattern exactly: its own module docstring
explains why a live test opens its OWN engine against the REAL app DB
(`guitar`) rather than the `guitar_test` this module's other tests use, and
why that's still read-only-safe — `run_agent_turn` itself never writes
anything; every mutation tool always suspends instead of executing).

THE CONTENT-BEARING RULE (`app.agent.loop._is_content_bearing`): a turn
forces a library search only when it plausibly asks a guitar technique/
theory/gear/tone QUESTION. Two categories are excluded even though they are
not small talk:
  1. small talk / pleasantries ("hi", "hello", "thanks") -- nothing to look up.
  2. an INSTRUCTION about an entity in the tutor's OWN data (a student,
     curriculum, lesson, session, note, or a request for a generated
     artifact/tab/diagram/scale) -- these route to a read/mutation tool
     (`app/agent/tools.py`), never to a library search ("split session 2 of
     that lesson" is about session #2 of a specific row, not something his
     book could ever answer).
A plain regex/keyword rule, not an LLM call: the whole point of C1 is
removing an unreliable judgment call FROM the model; asking a model to
classify the turn first would just move the unreliability one hop earlier.
The section below marked "(rule)" tests both sides directly.
"""
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.agent.loop as agent_loop
from app.agent.loop import AgentResult, run_agent_turn
from app.agent.tools import TOOLS, ToolEntry
from app.brain.retrieve import Hit
from app.llm.tools_types import AssistantTurn, ToolCall
from app.main import app


class _FakeProvider:
    """Mirrors `test_agent_loop.py`/`test_agent_hitl.py`'s `_FakeProvider`
    exactly: `chat_tools` returns the next scripted `turns` entry each call
    (repeating the last one once exhausted), and records a snapshot of every
    call's `messages` so a test can assert on exactly what the model saw --
    the crux of "the retrieved passage actually reached the model" below.
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


def _stub_tool(monkeypatch, name: str, fn):
    """Mirrors `test_agent_hitl.py`'s `_stub_tool` exactly."""
    original = TOOLS[name]
    monkeypatch.setitem(
        TOOLS, name,
        ToolEntry(schema=original.schema, fn=fn, kind=original.kind, async_job=original.async_job),
    )


def _hit(text: str, *, page: int = 21, score: float = 0.8, source_title: str = "Getting Great Guitar Sounds") -> Hit:
    return Hit(
        chunk_id=uuid4(), source_id=uuid4(), source_title=source_title,
        text=text, section_path=None, page=page, score=score, page_id=uuid4(),
    )


# ---------------------------------------------------------------------------
# C1: retrieval is FORCED, not requested
# ---------------------------------------------------------------------------

def test_retrieval_runs_even_when_the_model_would_not_have_asked(db, monkeypatch):
    """The model must not get the chance to skip retrieval. A fake provider
    that calls NO tools at all must STILL produce a grounded, cited answer --
    the loop itself ran `search()`, the model never had to ask for it.
    """
    searched = []

    def _fake_search(db_, q, k=5):
        assert db_ is db
        searched.append(q)
        return [_hit("A heavy pick reads as a darker, fuller tone.", page=21)]

    monkeypatch.setattr(agent_loop, "search", _fake_search)
    turn = AssistantTurn(content="The book says a heavy pick sounds darker.", tool_calls=[])
    fake_provider = _FakeProvider([turn])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(db, [{"role": "user", "content": "what does pick thickness do to tone?"}])

    assert searched == ["what does pick thickness do to tone?"], "retrieval did not run — the model was allowed to skip it"
    assert result.status == "answer"
    assert result.citations
    citation = result.citations[0]
    assert citation["page_no"] == 21
    assert citation["source_title"] == "Getting Great Guitar Sounds"
    assert citation["snippet"]
    assert citation["source_id"] and citation["page_id"]


def test_the_retrieved_passage_actually_reaches_the_model(db, monkeypatch):
    """A 'grounded' answer whose grounding never reached the prompt is not
    grounded — assert the hit's own text appears verbatim in what was
    actually sent to `chat_tools`.
    """
    passage = "A heavier pick produces a darker, fuller tone with less pick attack noise."

    def _fake_search(db_, q, k=5):
        return [_hit(passage, page=21)]

    monkeypatch.setattr(agent_loop, "search", _fake_search)
    turn = AssistantTurn(content="Per the book, heavier picks sound darker.", tool_calls=[])
    fake_provider = _FakeProvider([turn])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    run_agent_turn(db, [{"role": "user", "content": "what does pick thickness do to tone?"}])

    assert fake_provider.calls, "the provider was never called"
    sent = fake_provider.calls[0]
    assert any(passage in (m.get("content") or "") for m in sent), \
        "the retrieved passage never reached the messages sent to the model"

    # Regression pin: the grounding text must NOT be a second system message
    # — the real vLLM chat template 400s ("System message must be at the
    # beginning") on exactly that shape, caught by this task's own live
    # acceptance test. Exactly one system message, still at index 0.
    assert sent[0]["role"] == "system"
    assert sum(1 for m in sent if m["role"] == "system") == 1
    assert passage in sent[-1].get("content", "")
    assert sent[-1]["role"] == "user"


def test_the_pre_hop_no_longer_owns_a_relevance_floor(db, monkeypatch):
    """The floor MOVED (Plan 13, Stage 4.4). `loop.py` used to own
    `_RELEVANCE_FLOOR = 0.15` and filter `search()`'s hits itself — the third
    of three floors in the codebase, disagreeing with the other two, and a raw
    cosine threshold that would have become meaningless the moment `Hit.score`
    turned into an RRF fusion score topping out near 0.033.

    So the pre-hop now cites exactly what `search()` returns, and `search()` is
    the one place that decides what is worth returning (`retrieve._passes_floor`,
    which has the measured table). This test pins that: an irrelevant passage is
    NOT filtered here — if it ever reaches this point, that is a `search()` bug,
    and it must be fixed there rather than papered over in the agent loop.
    """
    junk = _hit("completely unrelated passage about capos", page=99, score=0.0)
    monkeypatch.setattr(agent_loop, "search", lambda db_, q, k=5: [junk])
    turn = AssistantTurn(content="General answer.", tool_calls=[])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: _FakeProvider([turn]))

    result = run_agent_turn(db, [{"role": "user", "content": "what does pick thickness do to tone?"}])

    assert len(result.citations) == 1, (
        "the pre-hop must cite what search() returned — it no longer re-filters, "
        "and a floor re-introduced here would be the fourth one"
    )


# ---------------------------------------------------------------------------
# C2: an empty library is stated plainly, not papered over
# ---------------------------------------------------------------------------

def test_an_empty_library_is_stated_plainly_not_papered_over(db, monkeypatch):
    """No hits -> the model is TOLD (in the injected grounding block) to say
    his material doesn't cover this and to label the answer as general
    knowledge, and `citations == []` (nothing to show a chip for).
    """
    monkeypatch.setattr(agent_loop, "search", lambda *a, **k: [])
    turn = AssistantTurn(
        content="His library doesn't cover pickup height; generally, lower is brighter (general knowledge).",
        tool_calls=[],
    )
    fake_provider = _FakeProvider([turn])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(db, [{"role": "user", "content": "what pickup height gives the most sustain?"}])

    assert result.citations == []
    sent = fake_provider.calls[0]
    # The grounding instruction is appended onto the USER turn's own content
    # — NOT a second system message (the live vLLM chat template 400s on a
    # system message that isn't the very first one; see `_grounding_block`'s
    # docstring). Pin BOTH: exactly one system message overall (still just
    # `SYSTEM_PROMPT`, at index 0), and the grounding text on the user turn.
    system_msgs = [m for m in sent if m["role"] == "system"]
    assert len(system_msgs) == 1
    user_msgs = [m for m in sent if m["role"] == "user" and "GROUNDING" in (m.get("content") or "")]
    assert user_msgs, "no grounding instruction injected for the empty-hit case"
    text = user_msgs[0]["content"].lower()
    assert "doesn't cover" in text or "does not cover" in text or "nothing" in text
    assert "general knowledge" in text


# ---------------------------------------------------------------------------
# The content-bearing rule itself — TEST BOTH SIDES
# ---------------------------------------------------------------------------

def test_small_talk_is_not_content_bearing():
    assert agent_loop._is_content_bearing("hello") is False
    assert agent_loop._is_content_bearing("hi there") is False
    assert agent_loop._is_content_bearing("thanks!") is False
    assert agent_loop._is_content_bearing("") is False


def test_entity_instructions_are_not_content_bearing():
    """An instruction ABOUT an entity in the tutor's own data is not a
    knowledge question, even though it is not small talk either.
    """
    assert agent_loop._is_content_bearing("split session 2 of that lesson") is False
    assert agent_loop._is_content_bearing("add a student named Ada") is False
    assert agent_loop._is_content_bearing("log progress for that student") is False
    assert agent_loop._is_content_bearing("make me a G chord diagram") is False
    assert agent_loop._is_content_bearing("give me a G major scale tab") is False


def test_guitar_knowledge_questions_are_content_bearing():
    assert agent_loop._is_content_bearing("what does pick thickness do to tone?") is True
    assert agent_loop._is_content_bearing("why does my low E buzz on the 5th fret?") is True
    assert agent_loop._is_content_bearing("how do I improve my vibrato?") is True


def test_hello_does_not_trigger_a_library_search(db, monkeypatch):
    """End-to-end version of the small-talk side: "hello" must not call
    `search` at all — if it did, this test would crash (search isn't
    stubbed) since a real DB session has no matching real embedding
    provider wired up here.
    """
    def _must_not_be_called(db_, q, k=5):
        raise AssertionError("search() must not run on small talk")

    monkeypatch.setattr(agent_loop, "search", _must_not_be_called)
    turn = AssistantTurn(content="Hey! What are we working on today?", tool_calls=[])
    fake_provider = _FakeProvider([turn])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(db, [{"role": "user", "content": "hello"}])

    assert result.status == "answer"
    assert result.citations == []


# ---------------------------------------------------------------------------
# C6 regression: forced retrieval must not disturb the HITL approval gate
# ---------------------------------------------------------------------------

def test_hitl_still_suspends_on_a_mutation(db, monkeypatch):
    """A mutation-proposing turn still suspends — the mutation fn is never
    invoked, nothing is written. Uses an entity-instruction message (not
    content-bearing per the rule above), mirroring `test_agent_hitl.py`'s
    own suspend tests, now against a real `db` session instead of `None`.
    """
    def _spy(db_, **kwargs):
        raise AssertionError("mutation fn must never be called by run_agent_turn")

    _stub_tool(monkeypatch, "update_block", _spy)

    call = ToolCall(id="call_1", name="update_block", arguments={"block_id": "b1", "title": "Ada"})
    turn = AssistantTurn(content="I'll rename that lesson.", tool_calls=[call])
    fake_provider = _FakeProvider([turn])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    result = run_agent_turn(db, [{"role": "user", "content": "rename that lesson to Ada"}])

    assert isinstance(result, AgentResult)
    assert result.status == "awaiting_approval"
    assert result.pending_tool == {
        "tool_call_id": "call_1", "name": "update_block",
        "arguments": {"block_id": "b1", "title": "Ada"},
    }
    assert not any(m["role"] == "tool" for m in result.messages)


def test_hitl_still_suspends_when_the_same_turn_is_also_content_bearing(db, monkeypatch):
    """The stronger version of the regression above: a turn that IS
    content-bearing (so C1's forced retrieval DOES run) and ALSO proposes a
    mutation in the same model reply. The suspend must still work exactly as
    before, AND the citations from the forced retrieval must still come
    through — C1 and C6 must compose, not fight each other.
    """
    def _fake_search(db_, q, k=5):
        return [_hit("A capo raises pitch without retuning.", page=5)]

    monkeypatch.setattr(agent_loop, "search", _fake_search)

    def _spy(db_, **kwargs):
        raise AssertionError("mutation fn must never be called by run_agent_turn")

    _stub_tool(monkeypatch, "update_block", _spy)

    call = ToolCall(
        id="call_1", name="update_block",
        arguments={"block_id": "b1", "body": "A capo raises pitch without retuning."},
    )
    turn = AssistantTurn(
        content="A capo raises pitch without retuning — I'll save that for you.",
        tool_calls=[call],
    )
    fake_provider = _FakeProvider([turn])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    # Phrasing deliberately avoids `_ENTITY_OR_ARTIFACT_RE`'s entity words
    # (lesson/session/note/...) so the turn stays content-bearing and the
    # forced retrieval genuinely runs alongside the proposed mutation.
    result = run_agent_turn(
        db, [{"role": "user", "content": "what does a capo do to my tone? save that for me"}],
    )

    assert result.status == "awaiting_approval"
    assert result.pending_tool["name"] == "update_block"
    assert not any(m["role"] == "tool" for m in result.messages)
    assert result.citations and result.citations[0]["page_no"] == 5


# ---------------------------------------------------------------------------
# C2 end-to-end: citations actually reach the persisted `Message` row via the
# real chat router — not just `AgentResult`'s own dataclass field.
# ---------------------------------------------------------------------------

client = TestClient(app)


def test_citations_persist_onto_the_message_row_via_the_chat_router(monkeypatch):
    def _fake_search(db_, q, k=5):
        return [_hit("A heavy pick reads as a darker, fuller tone.", page=21)]

    monkeypatch.setattr(agent_loop, "search", _fake_search)
    turn = AssistantTurn(content="The book says a heavy pick sounds darker.", tool_calls=[])
    fake_provider = _FakeProvider([turn])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)

    r = client.post("/chat", json={})
    assert r.status_code == 200, r.text
    session_id = r.json()["session_id"]

    r = client.post(f"/chat/{session_id}/messages", json={"content": "what does pick thickness do to tone?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["citations"] and body["citations"][0]["page_no"] == 21

    history = client.get(f"/chat/{session_id}")
    assert history.status_code == 200, history.text
    assistant_rows = [m for m in history.json() if m["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["citations"] and assistant_rows[0]["citations"][0]["page_no"] == 21


# ---------------------------------------------------------------------------
# THE ACCEPTANCE TEST — real model, real app data, no fakes anywhere.
# ---------------------------------------------------------------------------
#
# Mirrors `test_library_live.py`'s `app_db` pattern exactly (see that
# module's own docstring for the full rationale): opens its OWN engine
# against the REAL app DB (`guitar`), never the `guitar_test` this module's
# other tests use — conftest.py force-pins `DATABASE_URL` to `guitar_test`
# process-wide, so the only way to reach the tutor's actual 77-page book is
# a separate connection, exactly like that module already does. Read-only by
# contract: `run_agent_turn` is called DIRECTLY (not through the FastAPI
# router/`get_db`), and it never writes anything itself — every mutation
# tool always suspends instead of executing (C6), and the forced-retrieval
# pre-hop this task adds is a SELECT (`app.brain.retrieve.search`). Neither
# `get_provider` nor `search` is monkeypatched here — that IS the point:
# this is the one test in the whole suite proving the REAL model, given the
# REAL forced-retrieval context, answers from the REAL book and cites a REAL
# page, where before this task it silently answered from pretrained
# knowledge and cited nothing (Plan 5's own live test: 0/2 read turns
# invoked a retrieval tool).
APP_DATABASE_URL = os.environ.get(
    "APP_DATABASE_URL", "postgresql+psycopg://guitar:guitar@localhost:5434/guitar",
)

# Same book-only question `test_library_live.py` uses (its own comment there
# explains why it is book-only: the Course Spine, the library's only other
# content-bearing source, never discusses pick gauge) — phrased the way
# Chris's own acceptance criterion in the spec puts it.
PICK_THICKNESS_QUESTION = "what does pick thickness do to my tone?"


@pytest.fixture(scope="module")
def app_db():
    """Session on the REAL app DB. Read-only by contract — see this
    section's own comment above.
    """
    engine = create_engine(APP_DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.mark.integration
def test_pick_thickness_question_is_answered_grounded_in_the_real_book(app_db):
    """THE ACCEPTANCE CRITERION for Plan 11 Task 1.

    Ask the exact question from the spec's own acceptance test through the
    REAL `run_agent_turn` — no fake provider, no stubbed `search` — and get
    back an answer that cites the real book at a real, showable page.
    """
    result = run_agent_turn(app_db, [{"role": "user", "content": PICK_THICKNESS_QUESTION}])

    print(f"\nQUESTION: {PICK_THICKNESS_QUESTION}")
    print(f"STATUS: {result.status}")
    print(f"ANSWER:\n{result.content}\n")
    print(f"CITATIONS: {result.citations}")

    assert result.status == "answer", "the model hallucinated a mutation for a plain question"
    assert result.content, "no answer text came back"
    assert result.citations, "the forced retrieval found nothing — not grounded"

    top = result.citations[0]
    assert top["source_title"] == "Getting Great Guitar Sounds", (
        f"grounded in {top['source_title']!r} instead of the book"
    )
    assert top["page_no"] is not None, "citation carries no page number to show him"
    assert top["page_id"] is not None, "citation carries no page id — cannot fetch the scan"
    print(f"CITED: {top['source_title']!r} p.{top['page_no']} (page_id={top['page_id']})")
