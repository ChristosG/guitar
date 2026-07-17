"""`get_ocr_provider()` — Task 6. `OCR_PROVIDER` is a knob that must change
NOTHING when unset (every install today, including the live one:
`LLM_PROVIDER=claude_cli`, `OCR_PROVIDER` absent) and must let OCR run on a
DIFFERENT provider from chat when set — the whole point being "chat on the
subscription (`claude_cli`, free per token), OCR on a real key (`claude`,
pennies, no 5-hour cap)". Today's `ocr.py` calls `get_provider()` zero-arg, so
one knob picks both and that combination is unreachable; these tests pin the
fix at the factory seam.
"""
import pytest


def test_ocr_provider_defaults_to_the_chat_provider(monkeypatch):
    """Unset changes nothing for anyone. The knob exists to be ignored."""
    monkeypatch.setattr("app.config.settings.ocr_provider", None)
    monkeypatch.setattr("app.config.settings.llm_provider", "qwen")
    from app.llm.factory import get_ocr_provider, get_provider

    assert type(get_ocr_provider()) is type(get_provider())


def test_ocr_provider_can_differ_from_chat(monkeypatch):
    """The whole point: chat on the subscription (claude_cli, free), OCR on a
    real key (claude, pennies). Today `ocr.py` calls get_provider() zero-arg,
    so one knob picks both and this combination is unreachable."""
    monkeypatch.setattr("app.config.settings.llm_provider", "claude_cli")
    monkeypatch.setattr("app.config.settings.ocr_provider", "claude")
    monkeypatch.setattr("app.config.settings.llm_api_key", "sk-ant-test")
    from app.llm.claude import ClaudeProvider
    from app.llm.claude_cli import ClaudeCLIProvider
    from app.llm.factory import get_ocr_provider, get_provider

    assert isinstance(get_provider(), ClaudeCLIProvider)
    assert isinstance(get_ocr_provider(), ClaudeProvider)


def test_ocr_provider_claude_without_key_fails_honestly(monkeypatch):
    """`OCR_PROVIDER=claude` with no key configured anywhere (no DB row, no env
    fallback) must raise the same `LLMNotConfigured` a bare `claude` CHAT
    provider would — not silently fall back to an empty/broken key, which
    would surface 40 pages later as a cryptic auth failure instead of an
    honest "no key" at the moment OCR starts."""
    monkeypatch.setattr("app.config.settings.llm_provider", "claude_cli")
    monkeypatch.setattr("app.config.settings.ocr_provider", "claude")
    monkeypatch.setattr("app.config.settings.llm_api_key", "none")
    from app.llm.errors import LLMNotConfigured
    from app.llm.factory import get_ocr_provider

    with pytest.raises(LLMNotConfigured):
        get_ocr_provider()


def test_ocr_provider_shares_cache_with_get_provider_when_same_config(monkeypatch):
    """`get_ocr_provider()` must reuse `get_provider()`'s content-keyed cache,
    not bolt on a second one — so a settings write's `clear_provider_cache()`
    invalidates both, and two identical configs never build two objects."""
    monkeypatch.setattr("app.config.settings.llm_provider", "qwen")
    monkeypatch.setattr("app.config.settings.ocr_provider", "qwen")
    from app.llm.factory import get_ocr_provider, get_provider

    assert get_ocr_provider() is get_provider()
