"""`app.llm.claude.ClaudeProvider` — the request it BUILDS, without a key.

Every assertion here is about what leaves the process. That is deliberate: the
two ways this provider fails are both invisible without inspecting the outgoing
request, and both of them 400 *every single call*, so there is no partial
degradation to notice in a log.

  1. A forwarded `temperature`. The ABC's signature is
     `chat(messages, *, temperature=0.3)` and ten call sites rely on that
     default. Sonnet 5 rejects `temperature`, `top_p` and `top_k` outright. The
     provider must ACCEPT the argument and DISCARD it. If it ever forwards one,
     the app is 100% broken and the only symptom is a vendor 400.

  2. Haiku receiving `effort` or `thinking: adaptive`. Both are Sonnet-5-only.
     The tutor picks his model from a Settings screen, so this is a runtime
     choice, not a deploy-time one — a "deep thinking" role must resolve to
     NOTHING on Haiku rather than to a 400.

We assert on the kwargs the provider hands the SDK rather than on raw HTTP: it
is the same boundary, and it does not couple the test to the SDK's wire
serialization.
"""
import pytest

from app.llm.claude import HAIKU, SONNET, ClaudeProvider
from app.llm.errors import GuidedJSONError, LLMError

SAMPLING_KEYS = {"temperature", "top_p", "top_k"}


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _SpyMessages:
    """Captures the kwargs instead of making a call."""

    def __init__(self, resp):
        self.calls: list[dict] = []
        self._resp = resp

    def create(self, **kw):
        self.calls.append(kw)
        return self._resp


class _SpyClient:
    def __init__(self, resp):
        self.messages = _SpyMessages(resp)
        # Every per-request override the provider applied (`with_options`).
        # The real SDK returns a view of the same client; returning `self`
        # mirrors that closely enough for the seam under test.
        self.option_calls: list[dict] = []

    def with_options(self, **kw):
        self.option_calls.append(kw)
        return self


def _provider(model=SONNET, resp=None):
    p = ClaudeProvider(api_key="sk-ant-test", model=model)
    p._client = _SpyClient(resp or _Resp([_Block(type="text", text="hi")]))
    return p


# ---------------------------------------------------------------------------
# 1. No sampling parameter may EVER reach the SDK.
# ---------------------------------------------------------------------------

def test_chat_accepts_temperature_and_never_forwards_it():
    p = _provider()
    p.chat([{"role": "user", "content": "hi"}], temperature=0.7)
    sent = p._client.messages.calls[0]
    assert not (SAMPLING_KEYS & sent.keys()), f"sampling key leaked: {sent.keys()}"


def test_guided_json_accepts_temperature_and_never_forwards_it():
    p = _provider(resp=_Resp([_Block(type="text", text='{"ok": true}')]))
    p.guided_json([{"role": "user", "content": "x"}],
                  {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                  temperature=0.2)
    sent = p._client.messages.calls[0]
    assert not (SAMPLING_KEYS & sent.keys())


def test_chat_tools_accepts_temperature_and_never_forwards_it():
    p = _provider(resp=_Resp([_Block(type="text", text="ok")]))
    p.chat_tools([{"role": "user", "content": "hi"}], [], temperature=0.9)
    sent = p._client.messages.calls[0]
    assert not (SAMPLING_KEYS & sent.keys())


# ---------------------------------------------------------------------------
# 2. Capability is a property of the MODEL, not the call.
# ---------------------------------------------------------------------------

def test_haiku_never_receives_effort_or_adaptive_thinking():
    """`plan` is the role that most wants thinking. On Haiku it must resolve to
    nothing at all — Haiku 4.5 400s on both `effort` and `thinking: adaptive`."""
    p = _provider(model=HAIKU)
    p.chat([{"role": "user", "content": "hi"}], role="plan")
    sent = p._client.messages.calls[0]
    assert "thinking" not in sent
    assert "output_config" not in sent


def test_sonnet_does_receive_them_for_a_thinking_role():
    p = _provider(model=SONNET)
    p.chat([{"role": "user", "content": "hi"}], role="plan")
    sent = p._client.messages.calls[0]
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"]["effort"] == "high"


def test_the_chat_role_never_enables_thinking_even_on_sonnet():
    """Thinking + tool use requires replaying assistant `thinking` blocks. We do
    not store them (`models.chat.Message` has content + tool_calls, nothing
    else), and `loop.py`'s HITL suspend deliberately drops post-mutation calls —
    a verbatim replay would 400 on exactly the approve-a-mutation path. Stated
    trade; pin it."""
    p = _provider(model=SONNET)
    p.chat_tools([{"role": "user", "content": "hi"}], [], role="chat")
    assert "thinking" not in p._client.messages.calls[0]


def test_max_tokens_is_clamped_to_what_the_model_can_actually_emit():
    """The clamp is `min(role budget, model ceiling)`. Haiku 4.5's real output
    cap is 64K (the stale 8,192 that used to sit in `_CAPABILITIES` silently
    quartered `draft`'s budget and truncated every long Greek lesson on Haiku),
    so `draft`'s 32k passes through intact on BOTH models — while a role asking
    for more than 64k on Haiku would still be clamped, not 400."""
    p = _provider(model=HAIKU)
    p.chat([{"role": "user", "content": "hi"}], role="draft")
    assert p._client.messages.calls[0]["max_tokens"] == 32_000

    p = _provider(model=HAIKU)
    p.chat([{"role": "user", "content": "hi"}], role="draft")
    assert p._kwargs("draft", max_tokens=100_000)["max_tokens"] == 64_000

    p = _provider(model=SONNET)
    p.chat([{"role": "user", "content": "hi"}], role="draft")
    assert p._client.messages.calls[0]["max_tokens"] == 32_000


def test_every_role_resolves_including_default():
    from app.llm.claude import _ROLES

    p = _provider()
    for role in list(_ROLES) + ["a-role-nobody-defined"]:
        p.chat([{"role": "user", "content": "hi"}], role=role)
    assert len(p._client.messages.calls) == len(_ROLES) + 1


def test_the_compile_role_streams_a_large_budget_for_a_whole_book_read():
    """Compiling a book's concept canon is the app's BIGGEST single structured call —
    a 388-page book read in one shot. On the real API that role must:

      (a) allow a large output budget. The `spec` role the compile used to borrow
          caps at 4,096 tokens, which truncates a whole book's canon and surfaces as
          a `max_tokens` GuidedJSONError AFTER the money is spent; and
      (b) STREAM — a non-streaming request whose `max_tokens` implies more than ~10
          minutes of generation is rejected by the API outright, exactly as `draft`.
    """
    from app.llm.claude import _ROLES

    assert _ROLES["compile"]["stream"] is True, (
        "compile emits a large structured output; a non-streaming request that big "
        "is rejected by the API"
    )
    p = _provider(model=SONNET)
    kw = p._kwargs("compile")
    assert kw["max_tokens"] >= 16_000, (
        "4k truncates a whole book's canon — the compile role needs a real budget"
    )


# ---------------------------------------------------------------------------
# 3. The schema is sanitized before it is sent.
# ---------------------------------------------------------------------------

def test_guided_json_sanitizes_the_schema_it_sends():
    """A raw Pydantic schema carries `minItems`/`minimum` and no
    `additionalProperties`. The SDK strips those and validates client-side —
    i.e. it raises AFTER billing. Strip them here, visibly."""
    from app.artifacts.specs import SPECS

    p = _provider(resp=_Resp([_Block(type="text", text="{}")]))
    p.guided_json([{"role": "user", "content": "x"}],
                  SPECS["chord_diagram"].model_json_schema())

    sent = p._client.messages.calls[0]["output_config"]["format"]["schema"]
    assert sent["additionalProperties"] is False
    assert "minItems" not in sent["properties"]["frets"]
    # ...and $defs survived, or BarreSpec is unreachable
    assert "BarreSpec" in sent["$defs"]
    assert sent["$defs"]["BarreSpec"]["additionalProperties"] is False


# ---------------------------------------------------------------------------
# 4. Truncation must be NAMED, not reported as a JSON syntax error.
# ---------------------------------------------------------------------------

def test_max_tokens_truncation_is_named_not_reported_as_bad_json():
    """A Greek lesson that overruns `max_tokens` returns truncated JSON.
    `json.loads` would call that a syntax error, and whoever debugs it goes
    hunting for a bad prompt instead of a small budget."""
    p = _provider(resp=_Resp([_Block(type="text", text='{"lessons": [{"ti')],
                             stop_reason="max_tokens"))
    with pytest.raises(GuidedJSONError) as exc:
        p.guided_json([{"role": "user", "content": "x"}], {"type": "object"})
    msg = str(exc.value)
    assert "max_tokens" in msg and "BUDGET" in msg
    assert exc.value.kind == "upstream"


def test_claude_never_points_at_the_qwen_box():
    """`settings.llm_base_url` defaults to `http://qwen-vllm:6888/v1` and is
    ALWAYS truthy. A ClaudeProvider that read it sent every request to the local
    9B model — and the only symptom was the Settings screen telling the tutor
    "this model isn't available on your account" for a perfectly good key,
    because vLLM 404s on /v1/models/claude-sonnet-5. Wrong answer, wrong thing,
    no error anywhere. Two servers, two settings."""
    from app.config import settings

    assert settings.llm_base_url, "precondition: the Qwen URL is non-empty and truthy"
    assert settings.anthropic_base_url == "", (
        "Claude must default to api.anthropic.com, not to whatever llm_base_url holds"
    )

    p = ClaudeProvider(api_key="sk-ant-test", model=SONNET)
    client = p.client  # constructs the real SDK client (no network)
    assert "qwen" not in str(client.base_url).lower()
    assert "anthropic.com" in str(client.base_url)


def test_a_missing_key_is_an_auth_error_not_a_crash():
    """The ONE error the tutor can act on. It must not arrive as a stack trace."""
    p = ClaudeProvider(api_key="none", model=SONNET)
    with pytest.raises(LLMError) as exc:
        _ = p.client
    assert exc.value.kind == "auth"
    assert "Settings" in str(exc.value)


# ---------------------------------------------------------------------------
# 5. Per-role request timeouts — every request goes out with ITS role's budget.
# ---------------------------------------------------------------------------

def test_each_role_gets_its_own_request_timeout():
    """The client's 600s default is right for chat and lethally short for the
    long structured calls: a whole-book compile or a 32K-token Greek lesson
    legitimately runs past 10 minutes, and an SDK timeout there throws away
    tokens that were already billed. The mapping is applied per request via
    `with_options`, so `max_retries` stays as configured on the client."""
    expected = {"compile": 1800.0, "draft": 1200.0, "plan": 1200.0,
                "chat": 600.0, "spec": 600.0, "ocr": 600.0,
                "default": 600.0, "a-role-nobody-defined": 600.0}
    for role, timeout in expected.items():
        p = _provider(model=SONNET)
        p.chat([{"role": "user", "content": "hi"}], role=role)
        assert p._client.option_calls == [{"timeout": timeout}], (
            f"role {role!r} should carry timeout={timeout}"
        )


def test_guided_json_and_chat_tools_apply_the_role_timeout_too():
    p = _provider(resp=_Resp([_Block(type="text", text='{"ok": true}')]))
    p.guided_json([{"role": "user", "content": "x"}], {"type": "object"}, role="spec")
    assert p._client.option_calls == [{"timeout": 600.0}]

    p = _provider(resp=_Resp([_Block(type="text", text="ok")]))
    p.chat_tools([{"role": "user", "content": "hi"}], [], role="chat")
    assert p._client.option_calls == [{"timeout": 600.0}]


def test_vision_uses_the_ocr_timeout():
    p = _provider(resp=_Resp([_Block(type="text", text="page text")]))
    p.vision(b"\xff\xd8jpeg", "Transcribe this page.")
    assert p._client.option_calls == [{"timeout": 600.0}]


# ---------------------------------------------------------------------------
# 6. The silent 10x-invoice guard: a cache breakpoint that neither wrote nor
#    read the cache must be SAID, because nothing else looks different.
# ---------------------------------------------------------------------------

class _Usage:
    def __init__(self, **kw):
        self.input_tokens = kw.get("input_tokens", 0)
        self.output_tokens = kw.get("output_tokens", 0)
        self.cache_creation_input_tokens = kw.get("cache_creation_input_tokens", 0)
        self.cache_read_input_tokens = kw.get("cache_read_input_tokens", 0)


def _guided(p, messages):
    return p.guided_json(messages, {"type": "object"}, role="spec")


def test_a_dead_cache_breakpoint_logs_a_warning(caplog):
    resp = _Resp([_Block(type="text", text="{}")])
    resp.usage = _Usage(input_tokens=90_000)   # both cache counters zero
    p = _provider(resp=resp)
    with caplog.at_level("WARNING", logger="app.llm.claude"):
        _guided(p, [{"role": "user", "content": "the library", "cache": True},
                    {"role": "user", "content": "the task"}])
    assert any("cache" in r.message and "10x" in r.message for r in caplog.records), (
        "a request that carried a cache breakpoint but neither wrote nor read "
        "the cache is the silent 10x-invoice failure mode — it must be logged"
    )


def test_a_working_cache_stays_silent(caplog):
    for usage in (_Usage(cache_creation_input_tokens=90_000),   # 1st call: writes
                  _Usage(cache_read_input_tokens=90_000)):      # later calls: read
        resp = _Resp([_Block(type="text", text="{}")])
        resp.usage = usage
        p = _provider(resp=resp)
        with caplog.at_level("WARNING", logger="app.llm.claude"):
            _guided(p, [{"role": "user", "content": "the library", "cache": True}])
        assert not caplog.records
        caplog.clear()


def test_a_request_with_no_breakpoint_never_warns(caplog):
    """Chat-sized calls carry no cache breakpoint; zero cache counters there are
    normal, not a failure mode — the guard must not cry wolf."""
    resp = _Resp([_Block(type="text", text="{}")])
    resp.usage = _Usage(input_tokens=500)
    p = _provider(resp=resp)
    with caplog.at_level("WARNING", logger="app.llm.claude"):
        _guided(p, [{"role": "user", "content": "a small question"}])
    assert not caplog.records


# ---------------------------------------------------------------------------
# 6. `_mapped_errors` — a context overflow is `too_long`, not the generic
#    `BadRequestError` -> 400 or the `upstream` bucket. See errors.py's kind list.
# ---------------------------------------------------------------------------

def test_bad_request_prompt_too_long_maps_to_too_long(monkeypatch):
    import anthropic
    import httpx

    from app.llm.claude import _mapped_errors

    resp = httpx.Response(400, request=httpx.Request("POST", "https://x"),
                          json={"error": {"message": "prompt is too long: 1200000 tokens > 1000000 maximum"}})
    err = anthropic.BadRequestError("prompt is too long: 1200000 tokens > 1000000 maximum", response=resp, body=None)
    with pytest.raises(LLMError) as ei:
        with _mapped_errors():
            raise err
    assert ei.value.kind == "too_long"
