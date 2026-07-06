import logging

import httpx
from openai import OpenAI

from app.config import settings
from app.llm.base import LLMProvider
from app.llm.embeddings import l2_normalize, query_instruct

log = logging.getLogger(__name__)


class QwenVLLM(LLMProvider):
    def __init__(self) -> None:
        self._client = OpenAI(
            base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=60
        )

    def chat(self, messages, *, temperature=0.3, enable_thinking=False) -> str:
        resp = self._client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            temperature=temperature,
            extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
        )
        return resp.choices[0].message.content or ""

    def embed(self, texts, *, is_query=False) -> list[list[float]]:
        inputs = [query_instruct(t) for t in texts] if is_query else list(texts)
        vectors: list[list[float]] = []
        for i in range(0, len(inputs), 16):  # batch 16
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

    def health(self) -> dict:
        out = {"llm": False, "embed": False}
        try:
            httpx.get(f"{settings.llm_base_url}/models", timeout=5).raise_for_status()
            out["llm"] = True
        except Exception:
            log.warning("LLM health probe failed at %s", settings.llm_base_url, exc_info=True)
        try:
            httpx.get(f"{settings.embed_base_url}/models", timeout=5).raise_for_status()
            out["embed"] = True
        except Exception:
            log.warning("Embed health probe failed at %s", settings.embed_base_url, exc_info=True)
        return out
