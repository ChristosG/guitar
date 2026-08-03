"""`ClaudeProvider` — Anthropic behind the existing `LLMProvider` ABC.

Nothing above the seam learns that Anthropic exists. `agent/loop.py`,
`curriculum/generate.py`, `artifacts/generate.py` and `brain/ocr.py` are
untouched by this file; the OpenAI-shaped transcript in the database is
untouched too (see `llm/anthropic_wire.py` for why, and what it costs).

THE FOUR THINGS THAT ARE NOT OBVIOUS, each of which is a 400 if you get it wrong:

1. NO SAMPLING PARAMETERS. Sonnet 5 rejects `temperature`, `top_p` and `top_k`
   outright. The ABC's signatures pass `temperature=0.3`/`0.2` by default and
   every caller relies on those defaults existing. So the parameter is ACCEPTED
   and DISCARDED here — not removed from the ABC (that would touch 10 call
   sites to satisfy one vendor), and not forwarded (that would 400 every
   request). `tests/test_claude_provider.py` asserts no sampling key ever
   reaches the SDK, because a silently-forwarded `temperature` is a 400 on
   literally every call and there is no partial failure to notice.

2. CAPABILITY IS A PROPERTY OF THE MODEL, NOT THE CALL. Haiku 4.5 rejects
   `effort` AND rejects `thinking: {"type": "adaptive"}` (adaptive is 4.6+).
   Sonnet 5 accepts both. The tutor picks his model from a Settings screen, so
   a role that asks for "deep thinking" must resolve to *nothing* on Haiku
   rather than to a 400. Hence `_CAPABILITIES`: the request is composed from
   (what this role WANTS) x (what this model ALLOWS).

3. STRUCTURED OUTPUT IS NOT `guided_json`. There is no vLLM-style
   `response_format`. We use `output_config.format` with a json_schema — and the
   schema must first go through `llm/schema.py::to_anthropic_schema`, because
   Claude does not support `minItems`/`minLength`/`minimum` and the SDK will
   strip-and-then-fail on them *after* billing. The constraints those keywords
   used to carry now live in the prompt and in Pydantic.

4. THINKING IS OFF ON THE ReAct PATH, DELIBERATELY. Extended thinking + tool use
   requires replaying the assistant's `thinking` blocks back on the next turn.
   We do not store them: `models.chat.Message` has `content` and `tool_calls`,
   nothing else. Worse, `agent/loop.py`'s HITL suspend *deliberately drops*
   post-mutation tool calls from the transcript it persists — so a verbatim
   replay would resurrect blocks with no partner and 400 on exactly the
   approve-a-mutation path, which is the one path we least want to be fragile.
   Thinking stays on for `plan`/`draft` (single-shot structured calls with no
   replay), and off for `chat`. This is a stated trade, not an oversight.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Iterator

from app.config import settings
from app.llm.anthropic_wire import from_anthropic, to_anthropic, to_anthropic_tools
from app.llm.base import LLMProvider
from app.llm.errors import GuidedJSONError, LLMError
from app.llm.schema import to_anthropic_schema
from app.llm.tools_types import AssistantTurn

log = logging.getLogger(__name__)

SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"

# What each MODEL allows. Getting this wrong is a 400 on every request to that
# model, not a degraded response — Haiku does not "ignore" an `effort` it does
# not support, it rejects the call.
_CAPABILITIES: dict[str, dict[str, Any]] = {
    SONNET: {
        "thinking": True,          # adaptive
        "effort": True,
        # Sonnet 5's documented ceiling is 128K output tokens. Inert today —
        # the largest role budget is 32K, so the min() below always chooses
        # the role — but the old stale 64_000 would have silently halved the
        # first role budget anyone raised past it, which is exactly how
        # Haiku's stale 8_192 truncated every long Greek lesson once upon a
        # time. NOTE: whoever first raises a role budget past 64K should
        # verify against the live API that no beta header is required at that
        # size — a 400 there means this number, not your budget.
        "max_output": 128_000,
        "web_search_tool": "web_search_20260209",
    },
    HAIKU: {
        "thinking": False,         # adaptive is 4.6+; Haiku 4.5 genuinely 400s on it
        "effort": False,           # ...and on output_config.effort — keep both False
        # Haiku 4.5's real output cap is 64K (the old 8_192 here was stale — it
        # silently clamped `draft`'s 32K budget to a quarter of the model's
        # actual ceiling and truncated every long Greek lesson on Haiku).
        "max_output": 64_000,
        "web_search_tool": "web_search_20250305",
    },
}

# What each ROLE wants. Composed with the table above at request time.
#
# `max_tokens` is justified in GREEK, not English: Greek runs ~2-3x the tokens
# per word, and Sonnet 5's tokenizer emits more than its predecessors. A
# 2,200-word Greek lesson is ~6k tokens of prose inside a JSON envelope; 32k
# leaves room for a long one plus the structure. OCR's 4000 (a Qwen-era number)
# is TIGHTER under Claude than it was under Qwen, not looser — hence 8000.
#
# `stream` is not about the UI — no `guided_json` caller consumes deltas. The API
# REJECTS a non-streaming request whose `max_tokens` implies more than ~10 minutes
# of generation, and a 32,000-token Greek lesson is precisely that request. So the
# two long roles stream and wait for the final message; the short ones do not.
_ROLES: dict[str, dict[str, Any]] = {
    "default": {"thinking": False, "effort": "medium", "max_tokens": 4_096,  "stream": False},
    "chat":    {"thinking": False, "effort": "medium", "max_tokens": 8_192,  "stream": False},
    "plan":    {"thinking": True,  "effort": "high",   "max_tokens": 16_000, "stream": True},
    "draft":   {"thinking": True,  "effort": "high",   "max_tokens": 32_000, "stream": True},
    "spec":    {"thinking": False, "effort": "medium", "max_tokens": 4_096,  "stream": False},
    "ocr":     {"thinking": False, "effort": "low",    "max_tokens": 8_000,  "stream": False},
    # Reading a WHOLE book into the concept canon — the app's biggest single
    # structured call. `max_tokens` is `draft`-sized (32k), NOT `spec`'s 4,096: a
    # 388-page book's canon does not fit in 4k, and the overflow arrives as a
    # `max_tokens` GuidedJSONError AFTER the tokens are spent. `stream: True` for the
    # same reason `draft` streams — a non-streaming request whose `max_tokens`
    # implies >~10 min of generation is rejected outright. `thinking: False` is
    # deliberate here even though this is a no-replay single-shot call: on Anthropic
    # thinking tokens share the `max_tokens` budget, and letting them eat into a
    # whole-book JSON output is exactly the truncation this role exists to avoid.
    "compile": {"thinking": False, "effort": "medium", "max_tokens": 32_000, "stream": True},
}

# Per-ROLE request timeouts, applied per call via `client.with_options(...)`.
# The client's own 600s default is right for chat-sized calls and lethally
# short for the long structured ones: a whole-book compile or a 32K-token Greek
# lesson legitimately runs past 10 minutes, and an SDK timeout there does not
# save money — the tokens are already being generated; it just throws the
# result away and re-bills the retry. Ported from the CLI bridge's
# `_GUIDED_TIMEOUT_S` intent when the bridge was deleted.
_ROLE_TIMEOUT_S: dict[str, float] = {
    "compile": 1800.0,   # a 388-page book read whole, in one structured call
    "draft":   1200.0,   # a 32K-token Greek lesson
    "plan":    1200.0,   # outline over the whole library
}
_DEFAULT_TIMEOUT_S = 600.0

# Sonnet 5's real image ceiling is 2576px on the long edge (NOT the widely-cited
# 1568px). We cap at 2000: comfortably inside the limit, and a deliberate
# cost/legibility choice rather than an asserted maximum — every extra pixel is
# billed on all 77 pages of the book.
# Enforced by `brain/ocr.py::_render_for_vision` (the one image producer):
# a page whose long edge would rasterise past this is rendered at a reduced
# zoom instead. Not a provider hard limit — the API downscales oversized
# images itself — but downscaling here keeps the bytes and the aspect ratio
# under our control and off the wire.
MAX_IMAGE_EDGE_PX = 2000


class ClaudeProvider(LLMProvider):
    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key or settings.llm_api_key
        self._model = model or settings.llm_model
        if self._model not in _CAPABILITIES:
            raise ValueError(
                f"Unknown Claude model {self._model!r}. Known: {sorted(_CAPABILITIES)}"
            )
        self._client = None
        # The last response's `usage`, as a plain dict. Exists so the caller can
        # ask the ONE question the prompt cache makes it possible to get wrong
        # silently: `cache_read_input_tokens` on the second call of a fan-out. If
        # that is zero, the "stable prefix" is not stable, every lesson is
        # re-writing the 90K-token library at 1.25x, and NOTHING ELSE LOOKS
        # DIFFERENT — the curriculum still generates, it just costs 10x. The only
        # symptom is the invoice, which the tutor sees a month later.
        self.last_usage: dict[str, int] = {}

    # -- request composition ------------------------------------------------

    def _kwargs(self, role: str, *, max_tokens: int | None = None) -> dict[str, Any]:
        """(role -> intent) x (model -> capability) -> request kwargs.

        NOTE what is absent: `temperature`, `top_p`, `top_k`. See point 1.
        """
        want = _ROLES.get(role) or _ROLES["default"]
        can = _CAPABILITIES[self._model]

        cap = min(max_tokens or want["max_tokens"], can["max_output"])
        kw: dict[str, Any] = {"model": self._model, "max_tokens": cap}

        if want["thinking"] and can["thinking"]:
            kw["thinking"] = {"type": "adaptive"}
        if can["effort"]:
            kw["output_config"] = {"effort": want["effort"]}
        return kw

    @property
    def client(self):
        if self._client is None:
            import anthropic  # lazy: keeps the import cost off every process

            if not self._api_key or self._api_key == "none":
                raise LLMError(
                    "auth",
                    "No Anthropic API key configured. Open Settings and paste your key.",
                )
            # max_retries=4: the SDK backs off exponentially on 408/429/5xx/529.
            # Two retries survived a blip but not a real 429 burst — a 20-lesson
            # fan-out that trips the tier's RPM limit needs the later, longer
            # waits (the 3rd/4th retry) to outlive the window instead of
            # surfacing `rate_limit` and parking the lesson for a manual Resume.
            kw: dict[str, Any] = {"api_key": self._api_key, "max_retries": 4, "timeout": 600.0}
            # `anthropic_base_url`, NOT `llm_base_url`. They look interchangeable
            # and they are not: `llm_base_url` belongs to the QWEN provider and
            # defaults to `http://qwen-vllm:6888/v1`, which is always truthy. An
            # earlier version of this method read it, so ClaudeProvider silently
            # pointed every request at the local vLLM box — where
            # `models.retrieve("claude-sonnet-5")` 404s, and the Settings screen
            # duly told the tutor "this model isn't available on your account,
            # try the other one". A wrong answer, about the wrong thing, with no
            # error anywhere. Two settings, because they are two servers.
            #
            # Empty by default: the SDK then uses api.anthropic.com. Set it only
            # to point at a gateway that speaks the Anthropic Messages API.
            if settings.anthropic_base_url:
                kw["base_url"] = settings.anthropic_base_url
            self._client = anthropic.Anthropic(**kw)
        return self._client

    def _client_for(self, role: str):
        """The client, with THIS role's request timeout applied.

        `with_options` is the SDK's per-request override: it returns a view of
        the same client with only `timeout` changed — connection pool and the
        configured `max_retries` are untouched, so a `compile` still gets the
        4-retry backoff, just with 30 minutes per attempt instead of 10.
        """
        return self.client.with_options(timeout=_ROLE_TIMEOUT_S.get(role, _DEFAULT_TIMEOUT_S))

    # -- the ABC ------------------------------------------------------------

    def chat(self, messages, *, temperature: float = 0.3, enable_thinking: bool = False,
             role: str = "chat") -> str:
        system, msgs = to_anthropic(messages)
        with _mapped_errors():
            resp = self._client_for(role).messages.create(
                system=system or anthropic_omit(), messages=msgs, **self._kwargs(role)
            )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    def guided_json(self, messages, schema, *, temperature: float = 0.2,
                    role: str = "spec", max_tokens: int | None = None) -> dict:
        """Structured output via `output_config.format`, NOT vLLM's
        `response_format`. The schema is sanitized first — see `llm/schema.py`.

        The stop-reason check is not defensive padding: a Greek lesson that runs
        past `max_tokens` comes back as a TRUNCATED JSON string, and
        `json.loads` would report it as a syntax error. Whoever debugs that goes
        looking for a bad prompt instead of a small budget. Name it.
        """
        system, msgs = to_anthropic(messages)
        kw = self._kwargs(role, max_tokens=max_tokens)
        clean = to_anthropic_schema(schema)
        request = {
            "system": system or anthropic_omit(),
            "messages": msgs,
            "output_config": {**kw.pop("output_config", {}),
                              "format": {"type": "json_schema", "schema": clean}},
            **kw,
        }

        with _mapped_errors():
            if _ROLES.get(role, _ROLES["default"])["stream"]:
                # STREAMING IS NOT A UX CHOICE HERE — nothing consumes the deltas.
                # A non-streaming request whose `max_tokens` implies more than ~10
                # minutes of generation is REJECTED by the API outright, and a
                # 32,000-token Greek lesson is exactly that request. So `plan` and
                # `draft` stream and we simply wait for the final message; `spec`
                # and `chat` (4-8k) do not need to.
                with self._client_for(role).messages.stream(**request) as stream:
                    resp = stream.get_final_message()
            else:
                resp = self._client_for(role).messages.create(**request)
        self._record_usage(
            resp,
            expects_cache_hit=any(
                isinstance(m, dict) and m.get("cache") for m in messages
            ),
        )

        if resp.stop_reason == "max_tokens":
            raise GuidedJSONError(
                f"response hit max_tokens ({kw['max_tokens']}) and the JSON was cut "
                f"off mid-structure. This is a BUDGET problem, not a prompt problem — "
                f"Greek costs ~2-3x the tokens per word of English."
            )
        if resp.stop_reason == "refusal":
            raise GuidedJSONError("the model declined to produce this output")

        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        if not text.strip():
            raise GuidedJSONError("the model returned no content")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise GuidedJSONError(f"response was not valid JSON: {e}") from e

    def chat_tools(self, messages, tools, *, tool_choice: str = "auto",
                   temperature: float = 0.3, role: str = "chat") -> AssistantTurn:
        system, msgs = to_anthropic(messages)
        with _mapped_errors():
            resp = self._client_for(role).messages.create(
                system=system or anthropic_omit(),
                messages=msgs,
                tools=to_anthropic_tools(tools),
                tool_choice=_tool_choice(tool_choice),
                **self._kwargs(role),
            )
        return from_anthropic(resp)

    def chat_tools_stream(self, messages, tools, *, tool_choice: str = "auto",
                          temperature: float = 0.3, role: str = "chat") -> Iterator[dict]:
        """Yields `{"type": "content", "text": ...}` deltas, then exactly one
        `{"type": "done", "content", "tool_calls"}` — the contract
        `agent/loop.py::stream_plain_turn` relies on (see `base.py`)."""
        system, msgs = to_anthropic(messages)
        with _mapped_errors():
            with self._client_for(role).messages.stream(
                system=system or anthropic_omit(),
                messages=msgs,
                tools=to_anthropic_tools(tools),
                tool_choice=_tool_choice(tool_choice),
                **self._kwargs(role),
            ) as stream:
                for text in stream.text_stream:
                    if text:
                        yield {"type": "content", "text": text}
                final = stream.get_final_message()
        turn = from_anthropic(final)
        yield {"type": "done", "content": turn.content, "tool_calls": turn.tool_calls}

    def vision(self, image_bytes: bytes, prompt: str, *, media_type: str = "image/jpeg") -> str:
        """One page scan -> its text. Backs `brain/ocr.py`.

        A bad API key MUST surface as `LLMError(kind="auth")` and not be
        swallowed: `ocr.py` wraps each page in a bare `except Exception` and
        marks it `failed`, so an expired key would otherwise produce 77 "failed"
        pages and an `internal` job — telling the tutor his book is unreadable
        when the truth is that his key needs renewing. `_mapped_errors` raises
        `LLMError`, and `ocr.py` re-raises that class rather than counting it as
        a page failure.
        """
        b64 = base64.b64encode(image_bytes).decode()
        with _mapped_errors():
            resp = self._client_for("ocr").messages.create(
                messages=[{"role": "user", "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": prompt},
                ]}],
                **self._kwargs("ocr"),
            )
        if resp.stop_reason == "max_tokens":
            raise GuidedJSONError(
                "page transcription hit max_tokens — the page text is truncated, "
                "and a truncated page is a silently corrupted page"
            )
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()

    def count_tokens(self, text: str) -> int:
        """Exact, and FREE — `messages.count_tokens` is not a billed endpoint.

        This is what lets `app.curriculum.corpus` say "3 sources · 92,400 tokens ·
        fits whole" instead of guessing. The alternative (a chars/N heuristic) is
        wrong in the one direction that matters: it undercounts a Greek/English
        mixed corpus, and the app would only find out when a 90-second call came
        back as a context-length 400.
        """
        with _mapped_errors():
            resp = self.client.messages.count_tokens(
                model=self._model, messages=[{"role": "user", "content": text}]
            )
        return int(resp.input_tokens)

    def _record_usage(self, resp: Any, *, expects_cache_hit: bool = False) -> None:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        self.last_usage = {
            field: int(getattr(usage, field, 0) or 0)
            for field in (
                "input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens",
            )
        }
        # THE SILENT 10x-INVOICE GUARD. A request that carried a `cache: True`
        # breakpoint (the corpus prefix — see corpus.py:40-43) is supposed to
        # either WRITE the cache (first call of a fan-out) or READ it (every
        # later call). Both counters at zero means the breakpoint did nothing:
        # the "stable prefix" is not stable, every lesson re-bills the whole
        # library at full price, and NOTHING ELSE LOOKS DIFFERENT — the
        # curriculum still generates; the only other symptom is the invoice a
        # month later. Cheap and non-fatal: one log line, never an exception.
        if (
            expects_cache_hit
            and self.last_usage.get("cache_creation_input_tokens", 0) == 0
            and self.last_usage.get("cache_read_input_tokens", 0) == 0
        ):
            log.warning(
                "claude: a request carried a prompt-cache breakpoint but the "
                "response reports zero cache writes AND zero cache reads — the "
                "cached prefix is not caching. Every call in this fan-out is "
                "re-billing the full library at 1x instead of 0.1x (the silent "
                "10x-invoice failure mode; see app/curriculum/corpus.py)."
            )

    def health(self) -> dict:
        """Zero-token probe. `models.retrieve` distinguishes a bad key (401) from
        a bad model id (404) from a rate limit (429) without generating anything
        — unlike a `max_tokens=1` ping, which on Sonnet 5 runs adaptive thinking
        when `thinking` is omitted and is at best a wasted billed call.

        SHORT timeout, NO retries — deliberately unlike every generating call.
        This runs on `/health/ready`'s poll path: on the base client (600s ×
        5 attempts) a captive portal or dead network could park a worker
        thread for the better part of an hour per probe, and the desktop
        supervisor gives its own side of the poll 5 seconds anyway."""
        try:
            self.client.with_options(timeout=5.0, max_retries=0).models.retrieve(self._model)
            return {"llm": True}
        except Exception:
            log.warning("Claude health probe failed for %s", self._model, exc_info=True)
            return {"llm": False}


# ---------------------------------------------------------------------------


def anthropic_omit():
    """The SDK's sentinel for "don't send this field at all". Sending
    `system=None` is not the same as omitting it."""
    import anthropic

    return anthropic.NOT_GIVEN


def _tool_choice(choice: str) -> dict:
    if choice == "required":
        return {"type": "any"}
    if choice == "none":
        return {"type": "none"}
    return {"type": "auto"}


class _mapped_errors:
    """Anthropic exceptions -> this app's `LLMError` taxonomy.

    This is what `jobs/runner.py`'s `except LLMError` branches are built on:
    without it, every real transport failure would fall through to the generic
    `except Exception` and be recorded as `internal` — "our bug" — when it was
    a timeout, a rate limit, or an expired key.
    """

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            return False
        import anthropic

        if isinstance(exc, anthropic.AuthenticationError):
            raise LLMError(
                "auth",
                "Your Anthropic API key is invalid or expired. Open Settings to update it.",
            ) from exc
        if isinstance(exc, anthropic.PermissionDeniedError):
            raise LLMError(
                "auth",
                "Your Anthropic key is valid but not permitted to use this model, "
                "or the account is out of credit. Open Settings.",
            ) from exc
        if isinstance(exc, anthropic.RateLimitError):
            raise LLMError("rate_limit", "Anthropic rate limit reached — retrying shortly.") from exc
        if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
            raise LLMError("timeout", f"Could not reach Anthropic: {exc}") from exc
        if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
            raise LLMError("upstream", f"Anthropic returned {exc.status_code}") from exc
        return False  # anything else propagates unchanged
