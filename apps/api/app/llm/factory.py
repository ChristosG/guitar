from fastapi import HTTPException

from app.llm.base import LLMProvider
from app.llm.claude import ClaudeProvider
from app.llm.claude_cli import ClaudeCLIProvider
from app.llm.errors import LLMNotConfigured
from app.llm.qwen import QwenVLLM
from app.settings_store import LLMConfig, resolve_llm_config

# Fingerprint -> provider. NOT an `lru_cache(maxsize=1)`, and that change is the
# whole of Plan 13 Task 3.4's runtime half.
#
# THE TRAP THIS REPLACES: the key now arrives at RUNTIME, from a web form. A
# process-lifetime cache keyed on nothing would keep serving a provider built
# from the OLD key until someone restarted the container — so the tutor pastes
# the corrected key, clicks Test, and watches it fail *with the same error*.
# That is precisely the moment a non-technical user concludes the software is
# broken and stops. The cache is therefore keyed on the CONTENT of the config —
# `(provider, model, sha256(key)[:16])` — so a stale entry is not merely
# unlikely, it is unreachable: a changed key is a different dict key.
# `clear_provider_cache()` (called on every write in `settings_store`) is belt
# to that braces, not the mechanism.
#
# Unbounded in principle; in practice it holds one entry per distinct key the
# tutor has pasted in this process's lifetime — a handful at the very worst.
_PROVIDERS: dict[tuple[str, str, str], LLMProvider] = {}


def _build(cfg: LLMConfig) -> LLMProvider:
    if cfg.provider == "claude":
        return ClaudeProvider(api_key=cfg.api_key, model=cfg.model)
    if cfg.provider == "claude_cli":
        # Claude on the tutor's SUBSCRIPTION, via the `claude` CLI on the host.
        # No API key — a Max plan buys none. See `llm/claude_cli.py`.
        return ClaudeCLIProvider(model=cfg.model)
    if cfg.provider == "qwen":
        return QwenVLLM()
    raise ValueError(
        f"Unknown LLM_PROVIDER: {cfg.provider!r} "
        f"(expected 'claude', 'claude_cli' or 'qwen')"
    )


def get_provider() -> LLMProvider:
    """The chat/vision provider. Embeddings come from `llm/embed_factory.py`
    (Claude has no embeddings endpoint — see `llm/base.py`).

    ZERO-ARG SIGNATURE, DELIBERATELY UNCHANGED. Ten call sites — `agent/loop.py`,
    `curriculum/generate.py`, `artifacts/generate.py`, `brain/ocr.py`,
    `brain/retrieve.py`, `lessons/draft.py` — call this with no arguments and
    not one of them knows a Settings screen exists. Threading a config through
    all of them would push the tutor's API key into six modules that have no
    business holding it.

    Raises `LLMNotConfigured` when the provider is Claude and no key has been
    pasted. `main.py` turns that into a 409, never a 500.
    """
    cfg = resolve_llm_config()
    fp = cfg.fingerprint
    provider = _PROVIDERS.get(fp)
    if provider is None:
        provider = _build(cfg)
        _PROVIDERS[fp] = provider
    return provider


def clear_provider_cache() -> None:
    _PROVIDERS.clear()
    # A settings change also re-arms retrieval's query-translation breaker: a
    # freshly pasted key deserves a fresh try, not the tail of the dead key's
    # cooldown. Local import — retrieve.py imports this module.
    from app.brain.retrieve import reset_translation_breaker

    reset_translation_breaker()


def require_llm_configured() -> None:
    """A FastAPI dependency for the routes that ENQUEUE a `GenerationJob`.

    The synchronous paths need nothing: they call `get_provider()` inside the
    request, so an unconfigured key surfaces as a 409 on the spot via the
    exception handler. The BACKGROUND paths are the problem — by the time
    `run_curriculum_job` calls `get_provider()`, the request is gone, the 202 has
    been returned, and the only place left to put the failure is `job.error`. The
    tutor gets a red curriculum instead of an answer, and nothing tells him the
    fix is two clicks away in Settings.

    So: check first, enqueue second. No key means no job row at all.
    """
    try:
        resolve_llm_config()
    except LLMNotConfigured as e:
        raise HTTPException(
            status_code=409,
            detail={"code": "llm_not_configured", "message": str(e)},
        ) from e
