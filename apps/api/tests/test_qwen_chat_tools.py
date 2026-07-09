"""Unit tests for QwenVLLM.chat_tools - the tools-aware provider seam Task 2's
ReAct loop calls to drive the call -> run tools -> feed results back -> repeat
cycle (see `examples/tool_calling_minimal.py` in `/mnt/nvme2TB/vllm_interract`,
verified live against this exact vLLM server). Pure unit tests: no DB, no live
model needed - constructing `OpenAI(...)` makes no network call, so
`provider._client.chat.completions.create` can be monkeypatched directly to
return a fake response shaped like the real SDK's (`resp.choices[0].message`
carrying `.content` and `.tool_calls`, each tool_call carrying `.id` and
`.function.{name,arguments}`). Not marked `@pytest.mark.integration` (that
marker means "hits live vLLM" per pyproject.toml - this module hits neither).
"""
from types import SimpleNamespace

import pytest

from app.llm.errors import ToolArgsError
from app.llm.qwen import QwenVLLM
from app.llm.tools_types import AssistantTurn, ToolCall

_MESSAGES = [{"role": "user", "content": "What's the weather in Athens?"}]
_TOOLS = [{"type": "function", "function": {
    "name": "get_weather",
    "description": "Get the current weather for a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]},
}}]


def _fake_tool_call(id_: str, name: str, arguments: str) -> SimpleNamespace:
    """Mimics one `resp.choices[0].message.tool_calls[i]` entry from the
    OpenAI SDK: `.id` + `.function.name` + `.function.arguments` (a JSON
    *string*, not yet parsed - that's `chat_tools`'s job).
    """
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=arguments))


def _stub_create(content, tool_calls=None, capture: dict | None = None):
    def _create(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls)
            )]
        )
    return _create


def _provider_with_stub(monkeypatch, content, tool_calls=None, capture=None) -> QwenVLLM:
    provider = QwenVLLM()
    monkeypatch.setattr(
        provider._client.chat.completions, "create",
        _stub_create(content, tool_calls, capture),
    )
    return provider


def test_chat_tools_parses_tool_calls_response(monkeypatch):
    """A tool_calls response yields an AssistantTurn with parsed ToolCalls -
    `arguments` already `json.loads`'d into a dict, never left as a raw
    string for the caller to parse itself.
    """
    fake_calls = [_fake_tool_call("call_1", "get_weather", '{"city": "Athens"}')]
    provider = _provider_with_stub(monkeypatch, content=None, tool_calls=fake_calls)

    turn = provider.chat_tools(_MESSAGES, _TOOLS)

    assert isinstance(turn, AssistantTurn)
    assert turn.content is None
    assert turn.tool_calls == [
        ToolCall(id="call_1", name="get_weather", arguments={"city": "Athens"})
    ]


def test_chat_tools_plain_content_response_has_empty_tool_calls(monkeypatch):
    """A plain-text answer (`message.tool_calls is None` on the SDK object)
    must surface as `tool_calls == []`, not None, so callers (the ReAct loop)
    never need a None-check before iterating.
    """
    provider = _provider_with_stub(monkeypatch, content="It's sunny in Athens.", tool_calls=None)

    turn = provider.chat_tools(_MESSAGES, _TOOLS)

    assert turn.content == "It's sunny in Athens."
    assert turn.tool_calls == []


def test_chat_tools_raises_tool_args_error_on_malformed_json(monkeypatch):
    """The 9B occasionally emits malformed `arguments` JSON for a tool call
    (see agentic-gotchas.md #5). This must raise a typed `ToolArgsError`
    carrying the offending tool name - not let a raw `JSONDecodeError`
    surface - so Task 2's loop can do bounded repair instead of crashing.
    """
    fake_calls = [_fake_tool_call("call_1", "get_weather", "{city: Athens")]  # malformed JSON
    provider = _provider_with_stub(monkeypatch, content=None, tool_calls=fake_calls)

    with pytest.raises(ToolArgsError) as exc_info:
        provider.chat_tools(_MESSAGES, _TOOLS)
    assert exc_info.value.tool_name == "get_weather"
    assert exc_info.value.raw == "{city: Athens"


def test_chat_tools_forwards_tools_and_tool_choice_to_the_client(monkeypatch):
    """Regression guard: `tools`/`tool_choice` must actually reach the
    underlying `create(...)` call, not get silently dropped.
    """
    captured: dict = {}
    provider = _provider_with_stub(monkeypatch, content="ok", tool_calls=None, capture=captured)

    provider.chat_tools(_MESSAGES, _TOOLS, tool_choice="required")

    assert captured.get("tools") == _TOOLS
    assert captured.get("tool_choice") == "required"
