"""`app.llm.claude_cli.ClaudeCLIProvider` — the request it BUILDS, and the
`AssistantTurn` it RECONSTRUCTS, with no bridge and no subprocess.

Same discipline as `test_claude_provider.py`: assert on what leaves the process.
This provider has one extra thing to get wrong, and it is the interesting one —
it does not HAVE native tool calling, it EMULATES it on top of structured
output. That emulation has to produce, byte for byte, the same `AssistantTurn`
that `QwenVLLM.chat_tools` produces from a real OpenAI tool-call response, or
`agent/loop.py` breaks in ways that only show up against a live model.

The four things worth a test:

  1. `arguments` IS A JSON STRING IN THE SCHEMA, NOT AN OBJECT. Claude's
     structured outputs reject open-ended objects (`additionalProperties` must be
     `false`, so every key must be declared — impossible when the keys differ per
     tool). Typing it as a string is what makes the emulation legal at all. If a
     future edit "tidies" it into an object, every tool-calling turn 400s.

  2. The turn is RECONSTRUCTED to the ABC's contract: `content=None` (never `""`)
     on a tools-only turn, `tool_calls` always a list, `arguments` always a dict.

  3. Malformed arguments raise `ToolArgsError`, not `JSONDecodeError` — that is
     what `loop.py`'s bounded-repair branch catches.

  4. A bridge `kind` survives into `LLMError.kind` UNCHANGED. `rate_limit` in
     particular: `jobs/runner.py` requeues a rate-limited lesson and buries a
     failed one, and on a subscription the 5-hour cap is routine, not exceptional.
"""
import json

import httpx
import pytest

from app.llm.claude_cli import _TOOL_TURN_SCHEMA, ClaudeCLIProvider
from app.llm.errors import GuidedJSONError, LLMError, ToolArgsError

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Search the tutor's library.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
]


@pytest.fixture
def bridge(monkeypatch):
    """Capture the body the provider POSTs, and script the bridge's reply."""
    sent: dict = {}
    reply: dict = {"ok": True, "text": "", "structured": None}

    def _post(url, **kw):
        sent["url"] = url
        sent["body"] = kw.get("json")
        sent["headers"] = kw.get("headers")
        return httpx.Response(200, json=reply)

    monkeypatch.setattr(httpx, "post", _post)
    return sent, reply


def _provider() -> ClaudeCLIProvider:
    return ClaudeCLIProvider(model="claude-sonnet-5", bridge_url="http://bridge:8799")


# --- 1. the schema contract -------------------------------------------------

def test_tool_arguments_are_typed_as_a_json_string_not_an_object():
    """THE load-bearing detail of the emulation. See the module docstring."""
    args = _TOOL_TURN_SCHEMA["properties"]["tool_calls"]["items"]["properties"]["arguments"]
    assert args["type"] == "string", (
        "arguments must be a JSON STRING. Claude's structured outputs forbid "
        "open-ended objects (additionalProperties must be false), and tool args "
        "differ per tool — typing this as an object 400s every tool-calling turn."
    )


def test_tool_turn_schema_forbids_extra_keys_everywhere():
    assert _TOOL_TURN_SCHEMA["additionalProperties"] is False
    assert _TOOL_TURN_SCHEMA["properties"]["tool_calls"]["items"]["additionalProperties"] is False


# --- 2. the request that leaves the process ---------------------------------

def test_chat_sends_model_alias_effort_and_no_sampling_params(bridge):
    sent, reply = bridge
    reply.update({"ok": True, "text": "γεια"})

    out = _provider().chat([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])

    assert out == "γεια"
    body = sent["body"]
    assert body["model"] == "sonnet"           # alias, not the full id
    assert body["effort"] == "medium"          # role="chat"
    assert body["system"] == "S"
    assert "USER: U" in body["prompt"]
    # `temperature` is accepted by the signature and DISCARDED — there is no CLI
    # flag for it, and forwarding a key the bridge does not know is a silent no-op
    # that would read as "temperature works" forever.
    assert "temperature" not in body
    assert sent["headers"]["Authorization"].startswith("Bearer ")


def test_guided_json_sanitizes_the_schema_before_sending(bridge):
    """`minItems` is not supported by Claude's structured outputs — on the SDK path
    it is stripped and then enforced client-side AFTER billing. `llm/schema.py`
    removes it first; this provider must use that sanitizer, not the raw schema."""
    sent, reply = bridge
    reply.update({"ok": True, "structured": {"frets": [1, 2, 3, 4, 5, 6]}})

    _provider().guided_json(
        [{"role": "user", "content": "chord"}],
        {"type": "object",
         "properties": {"frets": {"type": "array", "items": {"type": "integer"}, "minItems": 6}},
         "required": ["frets"]},
    )

    schema = sent["body"]["json_schema"]
    assert "minItems" not in json.dumps(schema)
    assert schema["additionalProperties"] is False


def test_guided_json_returns_the_parsed_structured_output(bridge):
    sent, reply = bridge
    reply.update({"ok": True, "structured": {"title": "Μάθημα"}})
    assert _provider().guided_json([{"role": "user", "content": "x"}], {"type": "object"}) == {
        "title": "Μάθημα"
    }


def test_guided_json_raises_when_structured_output_is_missing(bridge):
    """`--json-schema` was sent, so `structured_output` should always come back.
    Its absence means refusal or a cut-off object — a `GuidedJSONError`, which
    callers already handle, not a `None` leaking into a caller expecting a dict."""
    sent, reply = bridge
    reply.update({"ok": True, "text": "", "structured": None, "stop_reason": "max_tokens"})
    with pytest.raises(GuidedJSONError, match="no structured output"):
        _provider().guided_json([{"role": "user", "content": "x"}], {"type": "object"})


# --- 3. the AssistantTurn it reconstructs -----------------------------------

def test_chat_tools_reconstructs_the_assistant_turn_contract(bridge):
    sent, reply = bridge
    reply.update({"ok": True, "structured": {
        "content": "",                                   # tools-only turn
        "tool_calls": [{"name": "search_knowledge",
                        "arguments": '{"query": "tube screamer"}'}],
    }})

    turn = _provider().chat_tools([{"role": "user", "content": "Τι λέει το βιβλίο;"}], TOOLS)

    # `content=None`, NEVER `""` — the contract `tools_types.AssistantTurn` states
    # and `agent/loop.py` branches on.
    assert turn.content is None
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert call.name == "search_knowledge"
    assert call.arguments == {"query": "tube screamer"}   # a dict, never the raw string
    assert call.id                                        # minted locally; the loop pairs on it

    # The tools reach the model as PROSE in the system prompt — there is no tool
    # protocol here. The reply is what's schema-constrained.
    assert "search_knowledge" in sent["body"]["system"]
    assert sent["body"]["json_schema"] == _TOOL_TURN_SCHEMA


def test_chat_tools_plain_answer_has_empty_tool_calls_list(bridge):
    sent, reply = bridge
    reply.update({"ok": True, "structured": {"content": "Γεια σου!", "tool_calls": []}})
    turn = _provider().chat_tools([{"role": "user", "content": "γεια"}], TOOLS)
    assert turn.content == "Γεια σου!"
    assert turn.tool_calls == []          # a list, not None — callers iterate unconditionally


def test_malformed_arguments_raise_ToolArgsError_not_JSONDecodeError(bridge):
    """`loop.py` catches `ToolArgsError` and does bounded repair. A raw
    `JSONDecodeError` would escape that branch and kill the turn."""
    sent, reply = bridge
    reply.update({"ok": True, "structured": {
        "content": "",
        "tool_calls": [{"name": "search_knowledge", "arguments": '{"query": '}],  # truncated
    }})
    with pytest.raises(ToolArgsError) as e:
        _provider().chat_tools([{"role": "user", "content": "x"}], TOOLS)
    assert e.value.tool_name == "search_knowledge"


# --- 4. error kinds survive the bridge --------------------------------------

@pytest.mark.parametrize("kind", ["rate_limit", "auth", "timeout", "upstream"])
def test_bridge_error_kind_survives_into_LLMError(bridge, kind):
    """The bridge classifies (it can see the CLI's exit code and stderr; we cannot),
    and the provider must not re-interpret. `rate_limit` above all: `jobs/runner.py`
    requeues on it and buries on anything else, and the subscription's 5-hour cap
    is routine."""
    sent, reply = bridge
    reply.clear()
    reply.update({"ok": False, "kind": kind, "message": "boom"})

    with pytest.raises(LLMError) as e:
        _provider().chat([{"role": "user", "content": "x"}])
    assert e.value.kind == kind


def test_unreachable_bridge_is_a_timeout_not_a_crash(monkeypatch):
    """The bridge lives on the HOST. It not running is the single most likely
    failure of this whole design, and it must say so — not surface as a raw
    httpx.ConnectError from six layers down."""
    def _boom(url, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _boom)
    with pytest.raises(LLMError) as e:
        _provider().chat([{"role": "user", "content": "x"}])
    assert e.value.kind == "timeout"
    assert "claude-bridge" in str(e.value)


# --- transcript rendering ---------------------------------------------------

def test_render_flattens_a_react_transcript_including_tool_results(bridge):
    """`claude -p` takes ONE string. The loop's OpenAI-shaped transcript — including
    the assistant's tool call and the result fed back — has to survive that
    flattening, or the second ReAct step has amnesia about the first."""
    sent, reply = bridge
    reply.update({"ok": True, "text": "done"})

    _provider().chat([
        {"role": "system", "content": "You are a tutor."},
        {"role": "user", "content": "Τι λέει το βιβλίο;"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "function": {"name": "search_knowledge",
                                                  "arguments": '{"query": "screamer"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "search_knowledge",
         "content": "σελ. 67: the Tube Screamer is..."},
    ])

    prompt = sent["body"]["prompt"]
    assert sent["body"]["system"] == "You are a tutor."      # extracted, not inlined
    assert "USER: Τι λέει το βιβλίο;" in prompt
    assert "search_knowledge" in prompt                      # the call survives
    assert "σελ. 67" in prompt                               # the RESULT survives
