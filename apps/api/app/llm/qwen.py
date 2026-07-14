import base64
import json
import logging

import httpx
from openai import OpenAI

from app.config import settings
from app.llm.base import LLMProvider
from app.llm.embeddings import l2_normalize, query_instruct
from app.llm.errors import GuidedJSONError, ToolArgsError
from app.llm.tools_types import AssistantTurn, ToolCall

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
        retry) — the response body is always schema-valid JSON *provided the
        model actually finished generating it*. Verified capability against
        this same infra in `/mnt/nvme2TB/vllm_interract` (guided_json_demo()).

        `timeout=300` (overriding the client's own `timeout=60` default,
        per-call): curriculum-tree guided-JSON generation was live-measured
        at 49-179s/call (Plan 3 Task 2's report) — comfortably past the
        60s default, so this call needs its own generous ceiling rather than
        inheriting the client-wide default sized for ordinary chat/embed calls.

        `max_tokens=8000` (review fix): guided decoding constrains *shape*,
        not *length* — a large tree (many modules/lessons/segments) can still
        run out of the server's own default token budget mid-object and get
        cut off (`finish_reason == "length"`), which is not schema-valid JSON
        despite the "always schema-valid" guarantee above. A generous ceiling
        makes that a rare edge case rather than a routine one; `GuidedJSONError`
        below is the backstop for when it happens anyway (or the model
        refuses outright, `message.content is None`) instead of letting a
        `TypeError`/`JSONDecodeError` surface as a raw 500 at the router.
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
            max_tokens=8000,
        )
        choice = resp.choices[0]
        if choice.message.content is None or choice.finish_reason == "length":
            raise GuidedJSONError(
                f"guided_json: unusable response (finish_reason={choice.finish_reason!r}, "
                f"content={'present' if choice.message.content is not None else 'None'})"
            )
        try:
            return json.loads(choice.message.content)
        except json.JSONDecodeError as e:
            raise GuidedJSONError(
                f"guided_json: invalid JSON (finish_reason={choice.finish_reason!r}): {e}"
            ) from e

    def chat_tools(self, messages, tools, *, tool_choice="auto", temperature=0.3) -> AssistantTurn:
        """Tool-calling (function-calling) turn for Task 2's ReAct loop: plain
        OpenAI-protocol `tools=`/`tool_choice=` — vLLM parses the model's tool
        calls server-side (launched with `--enable-auto-tool-choice
        --tool-call-parser qwen3_coder`; live-probed working against this
        exact server, see `/mnt/nvme2TB/vllm_interract/reference/
        agentic-gotchas.md` §9 and `examples/tool_calling_minimal.py`). The
        loop drives this in a call -> run tools -> feed results back -> repeat
        cycle; this method only makes the one call and returns a parsed
        `AssistantTurn` — the loop owns dispatch and history-building.

        `enable_thinking: False` (same as `chat`/`guided_json`) coexists with
        `tools=` on this infra (agentic-gotchas.md §7) — no `<think>` noise to
        strip out of tool-call turns.

        A tool call's `function.arguments` is a JSON *string* per the OpenAI
        SDK shape; this parses it eagerly so every caller gets a dict, never
        a string to re-parse. The 9B does occasionally emit malformed
        arguments JSON (agentic-gotchas.md §5) — that raises `ToolArgsError`
        (carrying the tool name + raw string) instead of letting a raw
        `JSONDecodeError` surface, so the loop can do bounded repair instead
        of crashing.
        """
        resp = self._client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        msg = resp.choices[0].message
        tool_calls = []
        for tc in msg.tool_calls or []:
            try:
                arguments = json.loads(tc.function.arguments)
            except json.JSONDecodeError as e:
                raise ToolArgsError(tool_name=tc.function.name, raw=tc.function.arguments) from e
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=arguments))
        return AssistantTurn(content=msg.content, tool_calls=tool_calls)

    def chat_tools_stream(self, messages, tools, *, tool_choice="auto", temperature=0.3):
        """Streaming counterpart of `chat_tools` above (Plan 11 Task 3, C4) —
        the ONE piece of real token-level streaming this app has: plain
        OpenAI-protocol `stream=True` on the exact same tool-calling call
        `chat_tools` makes (same `tools=`/`tool_choice=`/`enable_thinking:
        False` — this is not a different code path against the model, only a
        different response mode of the identical request).

        Yields `{"type": "content", "text": ...}` for each content delta AS
        THE SERVER SENDS IT (this is what actually eliminates the dead pause
        — the caller can forward each chunk to the browser the instant it
        arrives, unlike `chat_tools`, which blocks until the whole
        completion is done). Tool-call deltas arrive fragmented too (a
        `function.arguments` JSON string built up incrementally, `index`-
        keyed per the OpenAI streaming tool-call shape) — these are
        accumulated silently, NOT yielded incrementally: a caller has no use
        for a half-written tool-call JSON string, and streaming loose
        `text` deltas file `content` for a turn that turns out to be a tool
        call would ask a caller to distinguish something that's showable
        from something that isn't. The loop always ends with exactly ONE
        terminal `{"type": "done", "content": ..., "tool_calls": [...]}` —
        `content` mirrors `chat_tools`'s own `AssistantTurn.content` contract
        (`None` when nothing was written, never `""`); `tool_calls` is
        always a list, same as `AssistantTurn.tool_calls`.

        Malformed tool-call-arguments JSON raises `ToolArgsError` (same as
        `chat_tools`), from the ACCUMULATED string once the stream ends —
        there is no meaningful way to detect "malformed" from a partial
        fragment mid-stream.
        """
        stream = self._client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            stream=True,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        content_parts: list[str] = []
        pending_calls: dict[int, dict] = {}
        for chunk in stream:
            if not chunk.choices:
                continue  # some streamed chunks (e.g. a trailing usage-only chunk) carry no choice
            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
                yield {"type": "content", "text": delta.content}
            for tc in delta.tool_calls or []:
                slot = pending_calls.setdefault(tc.index, {"id": None, "name": None, "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function is not None:
                    if tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function.arguments:
                        slot["arguments"] += tc.function.arguments

        tool_calls: list[ToolCall] = []
        for index in sorted(pending_calls):
            slot = pending_calls[index]
            raw = slot["arguments"]
            try:
                arguments = json.loads(raw) if raw else {}
            except json.JSONDecodeError as e:
                raise ToolArgsError(tool_name=slot["name"] or "?", raw=raw) from e
            tool_calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=arguments))

        yield {"type": "done", "content": "".join(content_parts) or None, "tool_calls": tool_calls}

    def vision(self, image_bytes, prompt, *, media_type="image/jpeg") -> str:
        """Transcribe/describe one image. Backs OCR (`app.brain.ocr`).

        The local Qwen3.5-9B IS vision-capable — empirically confirmed against
        this exact server: an OpenAI `image_url` content block carrying a
        base64 data URI returned a faithful verbatim transcription of a real
        scanned book page (~1,127 prompt tokens/page at 110dpi).

        RENDER CEILING — 110dpi. Callers must not hand this method a page
        rendered above ~110dpi: this server's vLLM is configured with a
        multimodal encoder cache of 2048, and a 150dpi page overflows it with
        a hard HTTP 400 ("image item with length 2080 exceeds pre-allocated
        encoder cache size 2048"). Every page would fail, not just large ones.
        Raising the render DPI requires raising the server's mm budget first.

        `max_tokens=4000` is deliberate and load-bearing: the first live probe
        used 400 and came back `finish_reason="length"` with `content=None` —
        a dense page of body text simply does not fit in a small budget, and a
        truncated transcription is a silently corrupted page.

        Returns "" rather than raising on empty output; `ocr.py` owns the
        retry/failed lifecycle and treats "" as "nothing readable here".

        SWAP POINT (spec D3): this is the one method to reimplement to move
        OCR to Claude (`{"type": "document", ...}` with native PDF support and
        `page_location` citations). Nothing above this seam changes.
        """
        b64 = base64.b64encode(image_bytes).decode()
        resp = self._client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                {"type": "text", "text": prompt},
            ]}],
            temperature=0.0,          # transcription, not creativity
            max_tokens=4000,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            timeout=180,
        )
        return resp.choices[0].message.content or ""

    def health(self) -> dict:
        """Chat-server reachability only.

        The `embed` key is NOT produced here any more: embeddings left this ABC
        in Plan 13 Task 1.1 (see `app/llm/base.py`), so a chat provider no
        longer knows or cares whether the embedder is alive. `routers/health.py`
        composes the two probes into the `{db, llm, embed}` contract the
        `/health/ready` endpoint has always returned — that contract is
        unchanged, only its authorship is.
        """
        out = {"llm": False}
        try:
            httpx.get(f"{settings.llm_base_url}/models", timeout=5).raise_for_status()
            out["llm"] = True
        except Exception:
            log.warning("LLM health probe failed at %s", settings.llm_base_url, exc_info=True)
        return out
