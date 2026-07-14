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
    """`draft` asks for 32k. Haiku tops out at 8,192 — asking for more is a 400,
    not a truncation."""
    p = _provider(model=HAIKU)
    p.chat([{"role": "user", "content": "hi"}], role="draft")
    assert p._client.messages.calls[0]["max_tokens"] == 8_192

    p = _provider(model=SONNET)
    p.chat([{"role": "user", "content": "hi"}], role="draft")
    assert p._client.messages.calls[0]["max_tokens"] == 32_000


def test_every_role_resolves_including_default():
    from app.llm.claude import _ROLES

    p = _provider()
    for role in list(_ROLES) + ["a-role-nobody-defined"]:
        p.chat([{"role": "user", "content": "hi"}], role=role)
    assert len(p._client.messages.calls) == len(_ROLES) + 1


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


def test_a_missing_key_is_an_auth_error_not_a_crash():
    """The ONE error the tutor can act on. It must not arrive as a stack trace."""
    p = ClaudeProvider(api_key="none", model=SONNET)
    with pytest.raises(LLMError) as exc:
        _ = p.client
    assert exc.value.kind == "auth"
    assert "Settings" in str(exc.value)
