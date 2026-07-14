"""The embedding seam — deliberately SEPARATE from `app.llm.base.LLMProvider`.

WHY THIS FILE EXISTS AT ALL (Plan 13 Task 1.1): `embed()` used to be an
`@abstractmethod` on `LLMProvider`, alongside `chat`/`guided_json`/`vision`.
That was fine while one vLLM server did both. It stops being fine the moment
the chat provider is Claude: **Anthropic has no embeddings endpoint.** A
`ClaudeProvider` could not even be *instantiated* against that ABC — Python
raises `TypeError: Can't instantiate abstract class` at construction, so the
app would fail at import, not at the first query.

So embedding gets its own ABC and its own factory. Chat providers and
embedding providers now vary independently, which is the truth of the system:
we run Claude in the cloud for language, and a small model on the local CPU
for vectors.

THE MODEL — `intfloat/multilingual-e5-small`, 384-dim, ONNX, CPU, weights baked
into the image. Chosen by measurement against the real 408-chunk library, not
by reputation (see `scripts/retrieval_baseline.py` and this task's report):

  - `fastembed` does NOT carry e5-small (only `-large`, 1024-dim/2.24GB, at
    629ms/chunk on CPU — 15x slower and unusable in a desktop bundle). We use
    the ONNX `intfloat` itself publishes, loaded through `onnxruntime`.
  - int8 quantization is REJECTED. It is 4x smaller (118MB vs 470MB) and the
    vectors look fine (cosine 0.986-0.992 vs reference) — but it changes the
    top-5 retrieval ranking on 6 of 6 real queries. A quietly worse index is
    not worth 350MB. We ship fp32.

THE PREFIXES ARE NOT DECORATION. e5 is trained with an asymmetric objective:
queries must be prefixed `"query: "` and passages `"passage: "`. Omit them and
you still get a plausible 384-dim unit vector — retrieval just gets quietly
worse, with nothing to see in any log. They are owned HERE, in the one place
that knows which model is loaded, rather than at the call sites (which is where
the old Qwen `query_instruct` prefix lived, and which is why it was easy to
forget).

THE MASKED MEAN POOL is the other silent-corruption risk: e5 mean-pools the
last hidden state over the *non-padding* tokens. Average over the padding too
and, again, you get a normal-looking wrong vector. `tests/test_embedder.py`
pins the whole path against `sentence-transformers` as the reference
implementation (verified: cosine 1.000000 on fp32 ONNX).
"""
from __future__ import annotations

import logging
import os
import threading
from abc import ABC, abstractmethod

import httpx
import numpy as np

from app.config import settings
from app.llm.embeddings import l2_normalize, query_instruct

log = logging.getLogger(__name__)

# e5's asymmetric-retrieval prefixes. See the module docstring.
_E5_QUERY_PREFIX = "query: "
_E5_PASSAGE_PREFIX = "passage: "

# e5-small's positional limit. Chunks target ~1200 chars (`brain/chunk.py`),
# which is comfortably inside 512 XLM-R tokens for Latin script; Greek is more
# token-dense, so truncation is a real (if rare) possibility and we let the
# tokenizer do it rather than silently erroring.
_MAX_TOKENS = 512


class EmbeddingProvider(ABC):
    """Text -> unit-norm vectors. The ONLY contract the rest of the app has."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """Vector width. MUST match `chunk.embedding`'s pgvector column, or
        every insert fails at the DB, not at the ORM. Checked at boot by
        `app.main`'s lifespan warm-up."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Stamped onto `knowledge_source.embed_model` so a future model swap
        can tell which rows still hold vectors from the old embedding space."""

    @abstractmethod
    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        """`is_query` is not a hint — it selects a different prefix, and getting
        it backwards measurably degrades retrieval."""

    @abstractmethod
    def health(self) -> bool: ...


class LocalE5Embedder(EmbeddingProvider):
    """multilingual-e5-small on the local CPU via onnxruntime.

    Thread-safety: `onnxruntime.InferenceSession.run` is documented as
    thread-safe, and this class holds no mutable state after `__init__`. That
    matters because `curriculum`'s draft fan-out and `ocr`'s page pool both
    call `embed()` from a `ThreadPoolExecutor`.

    Lazy-loading is deliberate: the session is built on first use, then warmed
    explicitly at boot by `app.main`'s lifespan, so a missing/corrupt model file
    fails at STARTUP with a clear error rather than at the tutor's first
    question.
    """

    def __init__(self, model_dir: str | None = None) -> None:
        self._dir = model_dir or settings.embed_model_dir
        self._lock = threading.Lock()
        self._sess = None
        self._tok = None
        self._input_names: set[str] = set()

    @property
    def dim(self) -> int:
        return 384

    @property
    def model_id(self) -> str:
        return "intfloat/multilingual-e5-small"

    def _ensure_loaded(self) -> None:
        if self._sess is not None:
            return
        with self._lock:
            if self._sess is not None:  # another thread won the race
                return
            import onnxruntime as ort  # imported lazily: ~50MB, only the API needs it
            from tokenizers import Tokenizer

            onnx_path = os.path.join(self._dir, "model.onnx")
            tok_path = os.path.join(self._dir, "tokenizer.json")
            for p in (onnx_path, tok_path):
                if not os.path.exists(p):
                    raise RuntimeError(
                        f"embedding model not found at {p}. The weights are baked into "
                        f"the image at build time (see apps/api/Dockerfile); a missing "
                        f"file means the image was built wrong, not that a download "
                        f"failed at runtime."
                    )

            tok = Tokenizer.from_file(tok_path)
            tok.enable_truncation(max_length=_MAX_TOKENS)
            tok.enable_padding()

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = settings.embed_threads
            sess = ort.InferenceSession(
                onnx_path, opts, providers=["CPUExecutionProvider"]
            )
            self._input_names = {i.name for i in sess.get_inputs()}
            self._tok, self._sess = tok, sess
            log.info("loaded %s from %s (dim=%d)", self.model_id, self._dir, self.dim)

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_loaded()
        prefix = _E5_QUERY_PREFIX if is_query else _E5_PASSAGE_PREFIX
        out: list[list[float]] = []
        for i in range(0, len(texts), settings.embed_batch_size):
            batch = [prefix + t for t in texts[i : i + settings.embed_batch_size]]
            out.extend(self._run(batch))
        return out

    def _run(self, prefixed: list[str]) -> list[list[float]]:
        enc = self._tok.encode_batch(prefixed)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)

        feed = {"input_ids": ids, "attention_mask": mask}
        # The published graph declares token_type_ids; a re-export might not.
        # Feeding an input the graph doesn't declare is a hard onnxruntime error.
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros_like(ids)

        hidden = self._sess.run(None, feed)[0]  # (batch, seq, 384)

        # Masked mean pool — averaging over padding too yields a normal-looking
        # WRONG vector. See the module docstring.
        m = mask[..., None].astype(np.float32)
        vec = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
        vec /= np.linalg.norm(vec, axis=-1, keepdims=True)
        return vec.astype(np.float32).tolist()

    def health(self) -> bool:
        try:
            self._ensure_loaded()
            return len(self.embed(["ok"])[0]) == self.dim
        except Exception:
            log.warning("local embedder health probe failed", exc_info=True)
            return False


class QwenRemoteEmbedder(EmbeddingProvider):
    """The incumbent: Qwen3-Embedding-4B (2560-dim) on the shared vLLM box.

    Kept ALIVE, not deleted, and it is still the default — because the live
    `chunk.embedding` column is `vector(2560)` and holds 408 Qwen vectors. The
    switch to `LocalE5Embedder` is not a code change, it is a DATA migration
    (`ALTER TYPE vector(384)` + a full re-embed); flipping `EMBED_BACKEND`
    before that migration runs would hand a 384-dim query vector to a 2560-dim
    column and fail every search at the DB.

    So: this class is what makes Stage 1 shippable on its own. Stage 4 flips
    the default in the same commit as the migration. Once that has run on prod,
    this class and `EMBED_BASE_URL` can be deleted.
    """

    @property
    def dim(self) -> int:
        return settings.embed_dim

    @property
    def model_id(self) -> str:
        return settings.embed_model

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        inputs = [query_instruct(t) for t in texts] if is_query else list(texts)
        vectors: list[list[float]] = []
        for i in range(0, len(inputs), 16):
            batch = inputs[i : i + 16]
            r = httpx.post(
                f"{settings.embed_base_url}/embeddings",
                json={"model": settings.embed_model, "input": batch},
                timeout=60,
            )
            r.raise_for_status()
            # Pair by the response's `index`, not arrival order, so a reordered
            # batch response can never silently mis-pair text -> vector.
            ordered = sorted(r.json()["data"], key=lambda d: d["index"])
            vectors.extend(l2_normalize(d["embedding"]) for d in ordered)
        return vectors

    def health(self) -> bool:
        try:
            httpx.get(f"{settings.embed_base_url}/models", timeout=5).raise_for_status()
            return True
        except Exception:
            log.warning(
                "embed health probe failed at %s", settings.embed_base_url, exc_info=True
            )
            return False
