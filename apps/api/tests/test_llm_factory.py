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
    """`qwen` — the local vLLM box — is deleted and stays deleted. A config that
    still names it must fail naming the fix, not quietly build something else.

    (`claude_cli` used to be listed here too. It came back on 2026-08-07: the
    webapp runs on the tutor's subscription and the desktop app runs on an API
    key, so the two surfaces need two providers. See `_build`.)"""
    monkeypatch.setattr("app.config.settings.llm_provider", "qwen")
    monkeypatch.setattr("app.config.settings.llm_api_key", "sk-ant-test")
    from app.llm.factory import clear_provider_cache, get_provider

    clear_provider_cache()
    with pytest.raises(ValueError, match="claude"):
        get_provider()
    clear_provider_cache()


def test_the_two_surfaces_get_two_providers_and_cannot_cross(monkeypatch):
    """THE SPLIT THAT PAYS FOR THE BRIDGE EXISTING AT ALL.

    `claude` bills a card per token; `claude_cli` spends a flat-rate
    subscription. Wiring either surface to the other's provider is a real
    financial event, not a config nit — so both directions are pinned here.

    `desktop/src-tauri/src/supervisor.rs` hardcodes `LLM_PROVIDER=claude` on the
    api child, which is what makes the .dmg/.deb structurally unable to reach
    the bridge (they also ship no `tools/claude_bridge`, and no bridge would be
    running on a tutor's laptop to reach). This test is the API-side half of
    that guarantee: given each provider name, you get exactly one class.
    """
    from app.llm.claude import ClaudeProvider
    from app.llm.claude_cli import ClaudeCLIProvider
    from app.llm.factory import _build
    from app.settings_store import LLMConfig

    desktop = _build(LLMConfig(provider="claude", model="claude-sonnet-5", api_key="sk-ant-test"))
    assert isinstance(desktop, ClaudeProvider)
    assert not isinstance(desktop, ClaudeCLIProvider)

    webapp = _build(LLMConfig(provider="claude_cli", model="claude-sonnet-5", api_key=""))
    assert isinstance(webapp, ClaudeCLIProvider)
    assert not isinstance(webapp, ClaudeProvider)


def test_the_bridge_needs_no_key_and_the_api_still_does(monkeypatch):
    """A Max plan buys ZERO API credits, so "no key" is the webapp's correct
    steady state — and `resolve_llm_config` must return a config for it rather
    than raising the tutor at a Settings screen to paste something he does not
    have. The `claude` path keeps raising, because there a missing key really is
    the problem."""
    from app.llm.errors import LLMNotConfigured
    from app.settings_store import resolve_llm_config

    monkeypatch.setattr("app.config.settings.llm_api_key", "none")

    cfg = resolve_llm_config("claude_cli")
    assert cfg.provider == "claude_cli"
    assert cfg.api_key == ""
    assert cfg.model, "the bridge still reads the model from Settings"

    with pytest.raises(LLMNotConfigured):
        resolve_llm_config("claude")
