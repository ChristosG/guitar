from functools import lru_cache

from app.config import settings
from app.llm.base import LLMProvider
from app.llm.claude import ClaudeProvider
from app.llm.qwen import QwenVLLM


@lru_cache(maxsize=1)
def get_provider() -> LLMProvider:
    """The chat/vision provider. Embeddings come from `llm/embed_factory.py`
    (Claude has no embeddings endpoint — see `llm/base.py`).

    THE `lru_cache` IS A TRAP AND IT IS SCHEDULED FOR REMOVAL (Plan 13, Task
    3.4). The tutor will paste his Anthropic key into a Settings screen at
    runtime; a process-lifetime cache keyed on nothing would keep serving a
    provider built from the OLD key until someone restarted the container — so
    "I fixed my key" would appear not to work, which is precisely the moment a
    non-technical user gives up. Task 3.4 replaces this with a dict cache keyed
    on `(provider, model, sha256(key)[:16])`, so even a missed invalidation
    cannot serve a stale key indefinitely. The zero-arg signature stays, so no
    call site changes.
    """
    if settings.llm_provider == "claude":
        return ClaudeProvider()
    if settings.llm_provider == "qwen":
        return QwenVLLM()
    raise ValueError(
        f"Unknown LLM_PROVIDER: {settings.llm_provider!r} (expected 'claude' or 'qwen')"
    )
