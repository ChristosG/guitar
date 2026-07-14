"""`app.llm.anthropic_wire` — the translation that lets the DB keep speaking
OpenAI while the model speaks Anthropic.

These are not toy transcripts. Each mirrors a shape `app/agent/loop.py`
genuinely produces, because the whole risk here is a shape that only occurs in
production: the HITL suspend leaves a deliberately-unanswered tool call, the
forced-retrieval pre-hop injects a second consecutive user message, and a
multi-read turn emits several `{"role": "tool"}` rows in a row. Each of those is
a 400 if translated naively, and each 400 lands on a path the tutor uses.
"""
import json

import pytest

from app.llm.anthropic_wire import (
    DanglingToolUseError,
    from_anthropic,
    to_anthropic,
    to_anthropic_tools,
)


def _oai_call(cid: str, name: str, args: dict) -> dict:
    """Exactly what `loop.py::_wire_assistant_message` persists."""
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


# ---------------------------------------------------------------------------
# 1. Tool results must COALESCE into one user message.
# ---------------------------------------------------------------------------

def test_three_consecutive_tool_messages_become_ONE_user_message():
    """Anthropic requires every tool_result for an assistant turn in a SINGLE
    user message. Emitting three consecutive user messages is a 400 — and even
    where it isn't, it teaches the model (from its own transcript) to stop
    making parallel calls."""
    system, msgs = to_anthropic([
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "what do you have on pickups?"},
        {"role": "assistant", "content": None, "tool_calls": [
            _oai_call("c1", "search_knowledge", {"query": "pickups"}),
            _oai_call("c2", "list_curricula", {}),
            _oai_call("c3", "list_students", {}),
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "hit A"},
        {"role": "tool", "tool_call_id": "c2", "content": "hit B"},
        {"role": "tool", "tool_call_id": "c3", "content": "hit C"},
    ])

    assert system == "SYS"                       # lifted out, never a message
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]

    results = msgs[2]["content"]
    assert len(results) == 3, "three tool_results must land in ONE user message"
    assert [b["tool_use_id"] for b in results] == ["c1", "c2", "c3"]
    assert all(b["type"] == "tool_result" for b in results)

    # ...and the assistant turn carries the three tool_use blocks, with `input`
    # as a DICT (the wire shape stores `arguments` as a JSON string).
    uses = [b for b in msgs[1]["content"] if b["type"] == "tool_use"]
    assert [u["id"] for u in uses] == ["c1", "c2", "c3"]
    assert uses[0]["input"] == {"query": "pickups"}


# ---------------------------------------------------------------------------
# 2. The HITL suspend transcript — the highest-stakes path in the app.
# ---------------------------------------------------------------------------

def test_the_hitl_suspend_transcript_raises_instead_of_a_vendor_400():
    """`loop.py`'s suspend returns a transcript whose last assistant turn has
    ONE unanswered tool call — that IS "awaiting approval", and it is persisted
    that way. Anthropic rejects a dangling tool_use with an opaque 400.

    We must fail with something that names the call, not something that sends
    the next debugger to the Anthropic status page."""
    with pytest.raises(DanglingToolUseError) as exc:
        to_anthropic([
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "add a student called Nikos"},
            {"role": "assistant", "content": "I'll create him.", "tool_calls": [
                _oai_call("c9", "create_student", {"name": "Nikos"}),
            ]},
        ])
    assert "create_student" in str(exc.value)
    assert "c9" in str(exc.value)


def test_the_RESUMED_hitl_transcript_translates_cleanly():
    """After the tutor approves, the resolve path appends the tool result and
    calls the model again. THAT transcript — the one Claude actually sees — must
    be balanced, with the pre-mutation reads intact and nothing duplicated."""
    system, msgs = to_anthropic([
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "add a student called Nikos"},
        # loop.py includes reads dispatched BEFORE the mutation, then the
        # mutation itself; calls after it are dropped entirely.
        {"role": "assistant", "content": "Checking first.", "tool_calls": [
            _oai_call("r1", "list_students", {}),
            _oai_call("m1", "create_student", {"name": "Nikos"}),
        ]},
        {"role": "tool", "tool_call_id": "r1", "content": "[]"},
        {"role": "tool", "tool_call_id": "m1", "content": "created id=abc"},
    ])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    # both results coalesced into the single trailing user message
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["r1", "m1"]
    # the assistant turn kept its text AND both tool_use blocks, in order
    kinds = [b["type"] for b in msgs[1]["content"]]
    assert kinds == ["text", "tool_use", "tool_use"]


# ---------------------------------------------------------------------------
# 3. Consecutive same-role messages — the forced-retrieval pre-hop does this.
# ---------------------------------------------------------------------------

def test_consecutive_user_messages_are_merged():
    """`loop.py` injects a GROUNDING user message immediately before the tutor's
    own user message. Anthropic rejects two consecutive same-role messages."""
    _, msgs = to_anthropic([
        {"role": "user", "content": "GROUNDING:\n[1] a passage from the book"},
        {"role": "user", "content": "τι είναι το power chord;"},
    ])
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert [b["text"] for b in msgs[0]["content"]] == [
        "GROUNDING:\n[1] a passage from the book",
        "τι είναι το power chord;",
    ]


# ---------------------------------------------------------------------------
# 4. Empty text blocks are a 400.
# ---------------------------------------------------------------------------

def test_empty_and_whitespace_text_is_dropped_not_sent():
    """A tool-calls-only assistant turn has `content: None` from the OpenAI SDK,
    but `""` can reach the DB by other routes. Anthropic 400s on an empty text
    block."""
    _, msgs = to_anthropic([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "   ", "tool_calls": [
            _oai_call("c1", "list_students", {}),
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "[]"},
    ])
    blocks = msgs[1]["content"]
    assert all(b["type"] != "text" for b in blocks), "whitespace text block survived"
    assert len(blocks) == 1 and blocks[0]["type"] == "tool_use"


def test_an_assistant_turn_with_neither_text_nor_calls_is_skipped():
    _, msgs = to_anthropic([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None},
    ])
    assert [m["role"] for m in msgs] == ["user"]


# ---------------------------------------------------------------------------
# 5. Tool schema translation + the response direction.
# ---------------------------------------------------------------------------

def test_tool_schema_translation_uses_the_real_registry():
    from app.agent.tools import TOOLS

    openai_tools = [entry.schema for entry in TOOLS.values()]
    out = to_anthropic_tools(openai_tools)

    assert len(out) == len(TOOLS)
    for tool in out:
        assert set(tool) == {"name", "description", "input_schema"}
        assert tool["input_schema"]["type"] == "object"
    assert {t["name"] for t in out} == set(TOOLS)


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content):
        self.content = content


def test_from_anthropic_returns_None_content_for_a_tool_only_turn():
    """`loop.py` branches on `content is None`. Returning `""` would silently
    take the wrong branch."""
    turn = from_anthropic(_Resp([
        _Block(type="tool_use", id="c1", name="search_knowledge", input={"query": "x"}),
    ]))
    assert turn.content is None
    assert turn.tool_calls[0].name == "search_knowledge"
    assert turn.tool_calls[0].arguments == {"query": "x"}


def test_from_anthropic_joins_text_blocks():
    turn = from_anthropic(_Resp([
        _Block(type="text", text="Το power chord "),
        _Block(type="text", text="είναι..."),
    ]))
    assert turn.content == "Το power chord είναι..."
    assert turn.tool_calls == []
