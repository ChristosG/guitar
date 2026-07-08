"""Unit tests for QwenVLLM.guided_json's error handling.

A large curriculum tree can truncate mid-generation (`finish_reason ==
"length"`, no `max_tokens` previously set) or the model can refuse
(`message.content is None`) - both used to surface as an unhandled
`JSONDecodeError`/`TypeError` (a raw HTTP 500 at the router). These are pure
unit tests: no DB, no live model needed - constructing `OpenAI(...)` makes no
network call, so `provider._client.chat.completions.create` can be
monkeypatched directly to return a fake response shaped like the real SDK's.
Not marked `@pytest.mark.integration` (that marker means "hits live vLLM" per
pyproject.toml - this module hits neither).
"""
from types import SimpleNamespace

import pytest

from app.llm.errors import GuidedJSONError
from app.llm.qwen import QwenVLLM

_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}
_MESSAGES = [{"role": "user", "content": "hi"}]


def _stub_create(content, finish_reason="stop", capture: dict | None = None):
    def _create(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason=finish_reason,
            )]
        )
    return _create


def _provider_with_stub(monkeypatch, content, finish_reason="stop", capture=None) -> QwenVLLM:
    provider = QwenVLLM()
    monkeypatch.setattr(
        provider._client.chat.completions, "create",
        _stub_create(content, finish_reason, capture),
    )
    return provider


def test_guided_json_raises_on_none_content(monkeypatch):
    """A refusal: the SDK returns `message.content is None`."""
    provider = _provider_with_stub(monkeypatch, content=None, finish_reason="stop")
    with pytest.raises(GuidedJSONError):
        provider.guided_json(_MESSAGES, _SCHEMA)


def test_guided_json_raises_on_length_finish_reason_even_if_content_parses(monkeypatch):
    """Truncation must be caught via `finish_reason == "length"` itself, not
    merely inferred from a JSONDecodeError - so even a `content` string that
    happens to still parse as valid JSON must not be trusted once the model
    says it was cut off before it was done.
    """
    provider = _provider_with_stub(monkeypatch, content='{"ok": true}', finish_reason="length")
    with pytest.raises(GuidedJSONError):
        provider.guided_json(_MESSAGES, _SCHEMA)


def test_guided_json_raises_on_malformed_json(monkeypatch):
    """Last-resort guard: some non-JSON string got through anyway."""
    provider = _provider_with_stub(monkeypatch, content="not json at all", finish_reason="stop")
    with pytest.raises(GuidedJSONError):
        provider.guided_json(_MESSAGES, _SCHEMA)


def test_guided_json_error_message_includes_finish_reason(monkeypatch):
    provider = _provider_with_stub(monkeypatch, content=None, finish_reason="length")
    with pytest.raises(GuidedJSONError, match="length"):
        provider.guided_json(_MESSAGES, _SCHEMA)


def test_guided_json_still_returns_dict_on_a_valid_response(monkeypatch):
    """Regression guard: the happy path must keep working unchanged."""
    provider = _provider_with_stub(monkeypatch, content='{"answer": "PONG"}', finish_reason="stop")
    out = provider.guided_json(_MESSAGES, _SCHEMA)
    assert out == {"answer": "PONG"}


def test_guided_json_requests_a_generous_max_tokens(monkeypatch):
    """No `max_tokens` was previously set on the `create(...)` call at all,
    so a large tree could silently truncate against vLLM's server-side
    default. Pin the fix's `max_tokens=8000`.
    """
    captured: dict = {}
    provider = _provider_with_stub(
        monkeypatch, content='{"answer": "PONG"}', finish_reason="stop", capture=captured,
    )
    provider.guided_json(_MESSAGES, _SCHEMA)
    assert captured.get("max_tokens") == 8000
