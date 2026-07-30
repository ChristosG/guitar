"""`get_embedder()` — the embedding counterpart to `llm/factory.py::get_provider`.

Separate module (rather than a second function in `factory.py`) so importing an
embedder never drags in the chat-provider import chain, and vice versa. That
matters for `scripts/reembed.py`, which needs vectors and must NOT need a
configured Anthropic key to run.
"""
from functools import lru_cache

from app.config import settings
from app.llm.embedder import EmbeddingProvider, LocalE5Embedder


@lru_cache(maxsize=1)
def get_embedder() -> EmbeddingProvider:
    """Cached: `LocalE5Embedder` holds a ~470MB onnxruntime session that must be
    built once per process, not per request.

    Unlike `get_provider()`, this cache is safe to keep as a plain `lru_cache`:
    the embedder is chosen by a static env var and carries no user-supplied
    secret, so there is no "the tutor changed his API key" invalidation problem
    (that one is why `get_provider` becomes a fingerprint-keyed dict in Plan 13
    Task 3.4).
    """
    backend = settings.embed_backend
    if backend == "local-e5":
        return LocalE5Embedder()
    raise ValueError(
        f"Unknown EMBED_BACKEND: {backend!r} (the only supported backend is "
        f"'local-e5' — the remote 'qwen' embedder was removed)"
    )
