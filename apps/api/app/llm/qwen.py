import json
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

    def guided_json(self, messages, schema, *, temperature=0.2) -> dict:
        """One-shot structured generation: vLLM's `response_format` json_schema
        CONSTRAINS decoding to `schema` server-side (not a post-hoc parse-and-
        retry) — the response body is always schema-valid JSON, so a malformed
        result is not a case this needs to handle. Verified capability against
        this same infra in `/mnt/nvme2TB/vllm_interract` (guided_json_demo()).

        `timeout=300` (overriding the client's own `timeout=60` default,
        per-call): curriculum-tree guided-JSON generation was live-measured
        at 49-179s/call (Plan 3 Task 2's report) — comfortably past the
        60s default, so this call needs its own generous ceiling rather than
        inheriting the client-wide default sized for ordinary chat/embed calls.
        """
        resp = self._client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            temperature=temperature,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "curriculum", "schema": schema},
            },
            timeout=300,
        )
        return json.loads(resp.choices[0].message.content)

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
