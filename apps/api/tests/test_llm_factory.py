"""`llm/factory.py` — the claude-only era.

`get_ocr_provider()` (Task 6) survives the bridge/qwen deletion as a SEAM:
`OCR_PROVIDER` unset must change nothing (it resolves to exactly the chat
provider), and set-but-unconfigured must fail with the same honest
`LLMNotConfigured` chat would raise — never a silently broken client that 401s
forty pages into a run. `_build` itself must reject anything that is not
`claude` loudly: a stale `LLM_PROVIDER=qwen` in an old .env is a config bug to
name, not a model to invent.
"""
import pytest


def test_ocr_defaults_to_the_cheap_reader_on_the_chat_key(monkeypatch):
    """Vision is built into every Claude model — a page read is a COST
    question, so OCR defaults to Haiku while everything that THINKS keeps the
    Settings model. Same key, same provider rules; only the model differs.
    (Chris: "lets use the cheapest :D" — this replaced the old
    knob-exists-to-be-ignored identity contract on 2026-08-03.)"""
    monkeypatch.setattr("app.config.settings.ocr_provider", None)
    monkeypatch.setattr("app.config.settings.llm_provider", "claude")
    monkeypatch.setattr("app.config.settings.llm_api_key", "sk-ant-test")
    from app.llm.factory import clear_provider_cache, get_ocr_provider, get_provider

    clear_provider_cache()
    ocr, chat = get_ocr_provider(), get_provider()
    assert ocr is not chat
    assert ocr._model == "claude-haiku-4-5"
    assert chat._model == "claude-sonnet-5"
    clear_provider_cache()


def test_a_stale_ocr_model_falls_back_rather_than_bricking_every_read(monkeypatch):
    """An `OCR_MODEL` naming a model we do not know must not stop the tutor
    reading books — the resolution falls back to the chat model."""
    monkeypatch.setattr("app.config.settings.ocr_model", "claude-3-haiku-ancient")
    monkeypatch.setattr("app.config.settings.llm_provider", "claude")
    monkeypatch.setattr("app.config.settings.llm_api_key", "sk-ant-test")
    from app.llm.factory import clear_provider_cache, get_ocr_provider, get_provider

    clear_provider_cache()
    assert get_ocr_provider() is get_provider()
    clear_provider_cache()


def test_ocr_provider_claude_without_key_fails_honestly(monkeypatch):
    """`OCR_PROVIDER=claude` with no key configured anywhere (no DB row, no env
    fallback) must raise the same `LLMNotConfigured` a bare `claude` CHAT
    provider would — not silently fall back to an empty/broken key, which
    would surface 40 pages later as a cryptic auth failure instead of an
    honest "no key" at the moment OCR starts."""
    monkeypatch.setattr("app.config.settings.llm_provider", "claude")
    monkeypatch.setattr("app.config.settings.ocr_provider", "claude")
    monkeypatch.setattr("app.config.settings.llm_api_key", "none")
    from app.llm.errors import LLMNotConfigured
    from app.llm.factory import clear_provider_cache, get_ocr_provider

    clear_provider_cache()
    with pytest.raises(LLMNotConfigured):
        get_ocr_provider()
    clear_provider_cache()


def test_ocr_provider_shares_cache_with_get_provider_when_models_coincide(monkeypatch):
    """`get_ocr_provider()` must reuse `get_provider()`'s content-keyed cache,
    not bolt on a second one — so a settings write's `clear_provider_cache()`
    invalidates both, and two identical configs never build two objects. With
    `OCR_MODEL` pointed at the chat model, the fingerprints coincide and the
    IDENTICAL object comes back."""
    monkeypatch.setattr("app.config.settings.llm_provider", "claude")
    monkeypatch.setattr("app.config.settings.ocr_provider", "claude")
    monkeypatch.setattr("app.config.settings.ocr_model", "claude-sonnet-5")
    monkeypatch.setattr("app.config.settings.llm_api_key", "sk-ant-test")
    from app.llm.factory import clear_provider_cache, get_ocr_provider, get_provider

    clear_provider_cache()
    assert get_ocr_provider() is get_provider()
    clear_provider_cache()


def test_a_stale_provider_name_is_a_loud_valueerror_not_a_silent_model(monkeypatch):
    """The bridge (`claude_cli`) and the local vLLM (`qwen`) are deleted. A
    config that still names one must fail naming the fix, not quietly build
    something else."""
    monkeypatch.setattr("app.config.settings.llm_provider", "qwen")
    monkeypatch.setattr("app.config.settings.llm_api_key", "sk-ant-test")
    from app.llm.factory import clear_provider_cache, get_provider

    clear_provider_cache()
    with pytest.raises(ValueError, match="claude"):
        get_provider()
    clear_provider_cache()
