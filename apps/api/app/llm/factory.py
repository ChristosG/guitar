from fastapi import HTTPException

from app.config import settings
from app.llm.base import LLMProvider
from app.llm.claude import ClaudeProvider
from app.llm.errors import LLMNotConfigured
from app.settings_store import LLMConfig, resolve_llm_config, resolve_ocr_config

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
    # `claude` is the ONLY provider. The `claude_cli` bridge and the local
    # `qwen` vLLM box were deleted with the CLI-bridge era; a config that still
    # names one of them is a stale .env, and the honest answer is a loud error
    # naming the fix, not a silently different model.
    if cfg.provider == "claude":
        return ClaudeProvider(api_key=cfg.api_key, model=cfg.model)
    raise ValueError(
        f"Unknown LLM_PROVIDER: {cfg.provider!r} (the only supported provider "
        f"is 'claude' — the 'claude_cli' bridge and 'qwen' were removed)"
    )


def _get_cached(cfg: LLMConfig) -> LLMProvider:
    """The one path that ever reads or writes `_PROVIDERS`. Both `get_provider()`
    and `get_ocr_provider()` go through this, so there is exactly one cache,
    keyed the one way described above — never a second, differently-keyed cache
    that `clear_provider_cache()` could forget to clear."""
    fp = cfg.fingerprint
    provider = _PROVIDERS.get(fp)
    if provider is None:
        provider = _build(cfg)
        _PROVIDERS[fp] = provider
    return provider


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
    return _get_cached(resolve_llm_config())


def get_ocr_provider() -> LLMProvider:
    """The provider that transcribes a page scan. `brain/ocr.py` calls this in
    place of `get_provider()`.

    Resolves via `resolve_ocr_config()`: the chat provider's key and rules,
    with `settings.ocr_model` (default `claude-haiku-4-5` — vision is built
    into every Claude model, so a page read is a COST question, not a
    capability one; see the setting's own comment) swapped in for the model.
    When the models coincide (`OCR_MODEL` set to the Settings model, or the
    tutor running Haiku for everything), the content-keyed cache returns the
    IDENTICAL chat provider object — one cache, one invalidation path.

    A missing key fails here with the SAME honest `LLMNotConfigured` chat
    raises — never a quietly broken client that 401s forty pages into a run.
    """
    return _get_cached(resolve_ocr_config())


def clear_provider_cache() -> None:
    _PROVIDERS.clear()
    # A settings change also re-arms retrieval's query-translation breaker: a
    # freshly pasted key deserves a fresh try, not the tail of the dead key's
    # cooldown. Local import — retrieve.py imports this module.
    from app.brain.retrieve import reset_translation_breaker

    reset_translation_breaker()


def _require_configured(provider: str | None) -> None:
    """Shared body of the two guards below: resolve, and turn the one error the
    tutor can actually fix into the 409 that says so."""
    try:
        resolve_llm_config(provider)
    except LLMNotConfigured as e:
        raise HTTPException(
            status_code=409,
            detail={"code": "llm_not_configured", "message": str(e)},
        ) from e


def require_llm_configured() -> None:
    """A FastAPI dependency for the routes that ENQUEUE a `GenerationJob` that
    will dispatch through `get_provider()` — i.e. chat's provider.

    The synchronous paths need nothing: they call `get_provider()` inside the
    request, so an unconfigured key surfaces as a 409 on the spot via the
    exception handler. The BACKGROUND paths are the problem — by the time
    `run_curriculum_job` calls `get_provider()`, the request is gone, the 202 has
    been returned, and the only place left to put the failure is `job.error`. The
    tutor gets a red curriculum instead of an answer, and nothing tells him the
    fix is two clicks away in Settings.

    So: check first, enqueue second. No key means no job row at all.
    """
    _require_configured(None)


def require_ocr_configured() -> None:
    """The same contract, for the routes whose job READS A PAGE SCAN.

    `require_llm_configured` resolves zero-arg — `settings.llm_provider`, chat's
    provider. The OCR routes' job does not dispatch there: it goes through
    `get_ocr_provider()`, which resolves `settings.ocr_provider`. With
    `OCR_PROVIDER` set-and-unconfigured, a guard that only resolved chat's
    provider would let the job enqueue anyway, and `LLMNotConfigured` would fire
    INSIDE the background job — the exact failure the dependency exists to
    prevent: the tutor gets 888 failed pages instead of a 409 pointing him at
    Settings.

    A GUARD MUST RESOLVE THE CONFIG ITS JOB WILL ACTUALLY USE — which is now
    `resolve_ocr_config()` (the provider seam plus the `ocr_model` swap), the
    exact resolution `get_ocr_provider()` dispatches on. The model swap can
    never change key-configuredness (same key either way), but resolving
    anything OTHER than the dispatch path here is how a guard drifts into
    vouching for a read it is not doing.
    """
    try:
        resolve_ocr_config()
    except LLMNotConfigured as e:
        raise HTTPException(
            status_code=409,
            detail={"code": "llm_not_configured", "message": str(e)},
        ) from e
