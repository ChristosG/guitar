"""`app.llm.claude_cli.ClaudeCLIProvider` — the request it BUILDS, and the
`AssistantTurn` it RECONSTRUCTS, with no bridge and no subprocess.

Same discipline as `test_claude_provider.py`: assert on what leaves the process.
This provider has one extra thing to get wrong, and it is the interesting one —
it does not HAVE native tool calling, it EMULATES it on top of structured
output. That emulation has to produce, byte for byte, the same `AssistantTurn`
that `QwenVLLM.chat_tools` produces from a real OpenAI tool-call response, or
`agent/loop.py` breaks in ways that only show up against a live model.

The four things worth a test:

  1. `arguments` IS A JSON STRING IN THE SCHEMA, NOT AN OBJECT. Claude's
     structured outputs reject open-ended objects (`additionalProperties` must be
     `false`, so every key must be declared — impossible when the keys differ per
     tool). Typing it as a string is what makes the emulation legal at all. If a
     future edit "tidies" it into an object, every tool-calling turn 400s.

  2. The turn is RECONSTRUCTED to the ABC's contract: `content=None` (never `""`)
     on a tools-only turn, `tool_calls` always a list, `arguments` always a dict.

  3. Malformed arguments raise `ToolArgsError`, not `JSONDecodeError` — that is
     what `loop.py`'s bounded-repair branch catches.

  4. A bridge `kind` survives into `LLMError.kind` UNCHANGED. `rate_limit` in
     particular: `jobs/runner.py` requeues a rate-limited lesson and buries a
     failed one, and on a subscription the 5-hour cap is routine, not exceptional.
"""
import base64
import json
import os

import httpx
import pytest

from app.config import settings
from app.llm.claude_cli import _TOOL_TURN_SCHEMA, ClaudeCLIProvider, _tool_system_prompt
from app.llm.errors import GuidedJSONError, LLMError, ToolArgsError

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Search the tutor's library.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
]


@pytest.fixture
def bridge(monkeypatch):
    """Capture the body the provider POSTs, and script the bridge's reply."""
    sent: dict = {}
    reply: dict = {"ok": True, "text": "", "structured": None}

    def _post(url, **kw):
        sent["url"] = url
        sent["body"] = kw.get("json")
        sent["headers"] = kw.get("headers")
        return httpx.Response(200, json=reply)

    monkeypatch.setattr(httpx, "post", _post)
    return sent, reply


def _provider() -> ClaudeCLIProvider:
    return ClaudeCLIProvider(model="claude-sonnet-5", bridge_url="http://bridge:8799")


# --- 1. the schema contract -------------------------------------------------

def test_tool_arguments_are_typed_as_a_json_string_not_an_object():
    """THE load-bearing detail of the emulation. See the module docstring."""
    args = _TOOL_TURN_SCHEMA["properties"]["tool_calls"]["items"]["properties"]["arguments"]
    assert args["type"] == "string", (
        "arguments must be a JSON STRING. Claude's structured outputs forbid "
        "open-ended objects (additionalProperties must be false), and tool args "
        "differ per tool — typing this as an object 400s every tool-calling turn."
    )


def test_tool_turn_schema_forbids_extra_keys_everywhere():
    assert _TOOL_TURN_SCHEMA["additionalProperties"] is False
    assert _TOOL_TURN_SCHEMA["properties"]["tool_calls"]["items"]["additionalProperties"] is False


# --- 2. the request that leaves the process ---------------------------------

def test_chat_sends_model_alias_effort_and_no_sampling_params(bridge):
    sent, reply = bridge
    reply.update({"ok": True, "text": "γεια"})

    out = _provider().chat([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])

    assert out == "γεια"
    body = sent["body"]
    assert body["model"] == "sonnet"           # alias, not the full id
    assert body["effort"] == "medium"          # role="chat"
    assert body["system"] == "S"
    assert "USER: U" in body["prompt"]
    # `temperature` is accepted by the signature and DISCARDED — there is no CLI
    # flag for it, and forwarding a key the bridge does not know is a silent no-op
    # that would read as "temperature works" forever.
    assert "temperature" not in body
    assert sent["headers"]["Authorization"].startswith("Bearer ")


def test_guided_json_sanitizes_the_schema_before_sending(bridge):
    """`minItems` is not supported by Claude's structured outputs — on the SDK path
    it is stripped and then enforced client-side AFTER billing. `llm/schema.py`
    removes it first; this provider must use that sanitizer, not the raw schema."""
    sent, reply = bridge
    reply.update({"ok": True, "structured": {"frets": [1, 2, 3, 4, 5, 6]}})

    _provider().guided_json(
        [{"role": "user", "content": "chord"}],
        {"type": "object",
         "properties": {"frets": {"type": "array", "items": {"type": "integer"}, "minItems": 6}},
         "required": ["frets"]},
    )

    schema = sent["body"]["json_schema"]
    assert "minItems" not in json.dumps(schema)
    assert schema["additionalProperties"] is False


def test_guided_json_returns_the_parsed_structured_output(bridge):
    sent, reply = bridge
    reply.update({"ok": True, "structured": {"title": "Μάθημα"}})
    assert _provider().guided_json([{"role": "user", "content": "x"}], {"type": "object"}) == {
        "title": "Μάθημα"
    }


def test_guided_json_raises_when_structured_output_is_missing(bridge):
    """`--json-schema` was sent, so `structured_output` should always come back.
    Its absence means refusal or a cut-off object — a `GuidedJSONError`, which
    callers already handle, not a `None` leaking into a caller expecting a dict."""
    sent, reply = bridge
    reply.update({"ok": True, "text": "", "structured": None, "stop_reason": "max_tokens"})
    with pytest.raises(GuidedJSONError, match="no structured output"):
        _provider().guided_json([{"role": "user", "content": "x"}], {"type": "object"})


def test_the_compile_role_gets_a_whole_book_timeout_not_a_chat_one(bridge):
    """Reading a WHOLE book in one `guided_json` call is the LONGEST call in the app.
    Gallagher is 366K tokens — ~1.75x Hunter's 209K, which already took ~290s via
    `claude -p`. The default 600s a curriculum draft gets is too short for it and
    times the compile out (`ReadTimeout`). The `compile` role must ask the bridge for
    far more headroom; a genuine hang still dies at that ceiling, it does not hang
    forever."""
    sent, reply = bridge
    reply.update({"ok": True, "structured": {"concepts": []}})

    _provider().guided_json([{"role": "user", "content": "the whole book"}],
                            {"type": "object"}, role="compile")

    assert sent["body"]["timeout_s"] >= 1200, (
        "a whole-book compile must get well over the 600s a chat/draft gets — "
        f"got {sent['body']['timeout_s']}"
    )


def test_the_draft_role_gets_2400s_because_617s_was_only_typical(bridge):
    """A full-library lesson draft under `claude -p` re-reads the whole prefix on
    every call — there is no prompt cache — and the time is VARIABLE, not slow:
    617s for a 277K-char draft at 07:04 on 2026-09-12, and the same call past
    1200s at 12:17, which killed the tutor's lesson with «claude -p exceeded
    1200s». 2400s is headroom for that variance; a genuine hang still dies
    there, it does not hang forever."""
    sent, reply = bridge
    reply.update({"ok": True, "structured": {"title": "Το σχήμα C"}})

    _provider().guided_json([{"role": "user", "content": "the whole library"}],
                            {"type": "object"}, role="draft")

    assert sent["body"]["timeout_s"] == 2400


def test_a_non_compile_guided_json_still_fails_fast_at_600s(bridge):
    """The long timeout is SCOPED to the compile path. A stuck spec/chat-shaped
    structured call must still die at the default 600s, not inherit the book-length
    ceiling — a hung chat turn should fail fast."""
    sent, reply = bridge
    reply.update({"ok": True, "structured": {"ok": True}})

    _provider().guided_json([{"role": "user", "content": "x"}], {"type": "object"})

    assert sent["body"]["timeout_s"] == 600


# --- 3. the AssistantTurn it reconstructs -----------------------------------

def test_chat_tools_reconstructs_the_assistant_turn_contract(bridge):
    sent, reply = bridge
    reply.update({"ok": True, "structured": {
        "content": "",                                   # tools-only turn
        "tool_calls": [{"name": "search_knowledge",
                        "arguments": '{"query": "tube screamer"}'}],
    }})

    turn = _provider().chat_tools([{"role": "user", "content": "Τι λέει το βιβλίο;"}], TOOLS)

    # `content=None`, NEVER `""` — the contract `tools_types.AssistantTurn` states
    # and `agent/loop.py` branches on.
    assert turn.content is None
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert call.name == "search_knowledge"
    assert call.arguments == {"query": "tube screamer"}   # a dict, never the raw string
    assert call.id                                        # minted locally; the loop pairs on it

    # The tools reach the model as PROSE in the system prompt — there is no tool
    # protocol here. The reply is what's schema-constrained.
    assert "search_knowledge" in sent["body"]["system"]
    assert sent["body"]["json_schema"] == _TOOL_TURN_SCHEMA


def test_chat_tools_plain_answer_has_empty_tool_calls_list(bridge):
    sent, reply = bridge
    reply.update({"ok": True, "structured": {"content": "Γεια σου!", "tool_calls": []}})
    turn = _provider().chat_tools([{"role": "user", "content": "γεια"}], TOOLS)
    assert turn.content == "Γεια σου!"
    assert turn.tool_calls == []          # a list, not None — callers iterate unconditionally


def test_malformed_arguments_raise_ToolArgsError_not_JSONDecodeError(bridge):
    """`loop.py` catches `ToolArgsError` and does bounded repair. A raw
    `JSONDecodeError` would escape that branch and kill the turn."""
    sent, reply = bridge
    reply.update({"ok": True, "structured": {
        "content": "",
        "tool_calls": [{"name": "search_knowledge", "arguments": '{"query": '}],  # truncated
    }})
    with pytest.raises(ToolArgsError) as e:
        _provider().chat_tools([{"role": "user", "content": "x"}], TOOLS)
    assert e.value.tool_name == "search_knowledge"


# --- 4. error kinds survive the bridge --------------------------------------

@pytest.mark.parametrize("kind", ["rate_limit", "auth", "timeout", "upstream"])
def test_bridge_error_kind_survives_into_LLMError(bridge, kind):
    """The bridge classifies (it can see the CLI's exit code and stderr; we cannot),
    and the provider must not re-interpret. `rate_limit` above all: `jobs/runner.py`
    requeues on it and buries on anything else, and the subscription's 5-hour cap
    is routine."""
    sent, reply = bridge
    reply.clear()
    reply.update({"ok": False, "kind": kind, "message": "boom"})

    with pytest.raises(LLMError) as e:
        _provider().chat([{"role": "user", "content": "x"}])
    assert e.value.kind == kind


def test_unreachable_bridge_is_a_timeout_not_a_crash(monkeypatch):
    """The bridge lives on the HOST. It not running is the single most likely
    failure of this whole design, and it must say so — not surface as a raw
    httpx.ConnectError from six layers down."""
    def _boom(url, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _boom)
    with pytest.raises(LLMError) as e:
        _provider().chat([{"role": "user", "content": "x"}])
    assert e.value.kind == "timeout"
    assert "claude-bridge" in str(e.value)


# --- transcript rendering ---------------------------------------------------

def test_render_flattens_a_react_transcript_including_tool_results(bridge):
    """`claude -p` takes ONE string. The loop's OpenAI-shaped transcript — including
    the assistant's tool call and the result fed back — has to survive that
    flattening, or the second ReAct step has amnesia about the first."""
    sent, reply = bridge
    reply.update({"ok": True, "text": "done"})

    _provider().chat([
        {"role": "system", "content": "You are a tutor."},
        {"role": "user", "content": "Τι λέει το βιβλίο;"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "function": {"name": "search_knowledge",
                                                  "arguments": '{"query": "screamer"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "search_knowledge",
         "content": "σελ. 67: the Tube Screamer is..."},
    ])

    prompt = sent["body"]["prompt"]
    assert sent["body"]["system"] == "You are a tutor."      # extracted, not inlined
    assert "USER: Τι λέει το βιβλίο;" in prompt
    assert "search_knowledge" in prompt                      # the call survives
    assert "σελ. 67" in prompt                               # the RESULT survives


# --- 5. vision: the page goes as a FILE, not as base64 ----------------------
#
# This method used to delegate to Qwen, and the reason it no longer does is the
# whole point of the plan around it: the tutor's books carry someone else's
# Tesseract OCR, which turned every fraction glyph into a digit — "¼-inch" became
# "4-inch", and a 4-inch cable does not exist. The pages are being re-read.
#
# `claude -p` has NO image parameter. The only route from a scan to the model is
# the `Read` tool plus a real file, so the provider STAGES the page into the media
# volume both containers mount and posts the PATH. See `tools/claude_bridge/`.

PAGE = b"\xff\xd8\xff\xe0 pretend this is a rendered page"


@pytest.fixture
def vision_bridge(monkeypatch, tmp_path):
    """`media_dir` -> a tmpdir, and capture what the provider POSTs — including
    whether the staged page was ON DISK at the moment of the call, which is the
    entire contract with the bridge."""
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    sent: dict = {"paths": []}
    reply: dict = {"ok": True, "text": ""}

    def _post(url, **kw):
        body = kw.get("json") or {}
        sent["url"] = url
        sent["body"] = body
        sent["headers"] = kw.get("headers")
        staged = tmp_path / (body.get("image_path") or "_")
        sent["staged_existed"] = staged.is_file()
        sent["staged_bytes"] = staged.read_bytes() if staged.is_file() else None
        sent["paths"].append(body.get("image_path"))
        return httpx.Response(200, json=reply)

    monkeypatch.setattr(httpx, "post", _post)
    return sent, reply, tmp_path


def test_vision_posts_a_path_to_the_vision_endpoint_not_a_base64_page(vision_bridge):
    sent, reply, _media = vision_bridge
    reply.update({"ok": True, "text": "the ¼-inch jack"})

    out = _provider().vision(PAGE, "Transcribe all text on this page verbatim.")

    assert out == "the ¼-inch jack"
    # A SEPARATE endpoint. `/v1/complete` still runs `--tools ""` and must never
    # learn to read a file; that separation is the bridge's security boundary.
    assert sent["url"].endswith("/v1/vision")
    assert sent["body"]["prompt"] == "Transcribe all text on this page verbatim."
    assert "image_path" in sent["body"]
    # The page travels as a FILE. The Read tool needs a real one anyway, and
    # base64'ing an 11-megapixel scan through a JSON body would be ~15MB of
    # gratuitous copying between two containers that already share the volume.
    assert base64.b64encode(PAGE).decode() not in json.dumps(sent["body"])


def test_the_staged_page_is_on_disk_where_the_bridge_will_look(vision_bridge):
    """THE mount contract, and the one most likely to be wrong: the api WRITES
    the page and the bridge READS it, so they must be the same filesystem. If the
    bytes are not there at POST time, the bridge answers "no such image" while
    both containers sit there healthy."""
    sent, reply, _media = vision_bridge
    reply.update({"ok": True, "text": "ok"})

    _provider().vision(PAGE, "transcribe")

    assert sent["staged_existed"] is True
    assert sent["staged_bytes"] == PAGE
    # RELATIVE to the media root. The bridge refuses an absolute path outright —
    # silently rebasing one would be the same bug as trusting it.
    assert not os.path.isabs(sent["body"]["image_path"])


def test_the_staged_page_is_deleted_after_the_call(vision_bridge):
    sent, reply, media = vision_bridge
    reply.update({"ok": True, "text": "ok"})

    _provider().vision(PAGE, "transcribe")

    assert not (media / sent["body"]["image_path"]).exists()


def test_the_staged_page_is_deleted_even_when_the_bridge_fails(vision_bridge):
    """Scratch is scratch on the failure path too. 888 pages leaking one scan
    each is a second copy of the tutor's whole library on his disk — and the run
    that leaks them is exactly the one that hit the 5-hour cap and gets retried."""
    sent, reply, media = vision_bridge
    reply.clear()
    reply.update({"ok": False, "kind": "rate_limit", "message": "usage limit reached"})

    with pytest.raises(LLMError):
        _provider().vision(PAGE, "transcribe")

    assert not (media / sent["paths"][0]).exists()


def test_two_pages_in_flight_cannot_collide(vision_bridge):
    """The bridge runs 3 `claude` processes at once and the OCR run is a fan-out.
    A fixed scratch name lets page 2 overwrite page 1's bytes in the window
    between the write and the Read — and the symptom is a transcript of the WRONG
    PAGE: silent data corruption that reads like a bad model."""
    sent, reply, _media = vision_bridge
    reply.update({"ok": True, "text": "ok"})

    p = _provider()
    p.vision(PAGE, "page one")
    p.vision(PAGE, "page two")

    assert len(set(sent["paths"])) == 2


@pytest.mark.parametrize("kind", ["rate_limit", "auth", "timeout", "upstream"])
def test_a_vision_error_kind_survives_into_LLMError(vision_bridge, kind):
    """Same contract as chat, and it matters more here: re-reading 888 pages WILL
    hit the subscription cap, and `jobs/runner.py` requeues a `rate_limit` page
    while burying an `upstream` one."""
    sent, reply, _media = vision_bridge
    reply.clear()
    reply.update({"ok": False, "kind": kind, "message": "boom"})

    with pytest.raises(LLMError) as e:
        _provider().vision(PAGE, "transcribe")

    assert e.value.kind == kind


def test_vision_reads_the_page_itself_and_delegates_to_nothing(vision_bridge):
    """The hybrid is over: vision goes to the bridge and nowhere else.

    This used to assert specifically that `vision()` never constructs
    `QwenVLLM` — the local 9B that was measured LOSING the very glyphs the
    re-read exists to recover. `app/llm/qwen.py` was deleted with the CLI-bridge
    era (feb9b2c) and did not come back when the bridge did, so that assertion
    could no longer even import. Rewritten to pin the INTENT rather than the
    dead name: one call, to `/v1/vision`, and no second provider anywhere in the
    path. A test whose premise has been deleted should be re-aimed, not kept
    passing by importing a corpse.
    """
    sent, reply, _media = vision_bridge
    reply.update({"ok": True, "text": "ok"})

    assert _provider().vision(PAGE, "transcribe") == "ok"
    assert sent["url"].endswith("/v1/vision")

    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.llm.qwen")


def test_vision_allows_far_more_time_than_a_chat_turn(vision_bridge):
    """One page is ~40s: Node boots, the model reads the scan, and it may take an
    agentic turn or two zooming in. A chat-shaped timeout kills a working call and
    reads as "vision is broken"."""
    sent, reply, _media = vision_bridge
    reply.update({"ok": True, "text": "ok"})

    _provider().vision(PAGE, "transcribe")

    assert sent["body"]["timeout_s"] >= 120


def test_tool_system_prompt_disclaims_native_tools():
    """2026-09-12: the model inside `claude -p` (run with `--tools ""`, i.e. NO
    native tools) attempted a native tool call anyway, got "no such tool
    available" from the CLI harness, and answered the tutor with nothing —
    zero emulated tool calls in that turn. The prompt must say, explicitly,
    that native tool calls do not exist here and the only way in is the
    "tool_calls" field of the JSON reply."""
    prompt = _tool_system_prompt(TOOLS, "auto")

    assert (
        'You have NO native tools in this environment and must never attempt a '
        'native tool call — the ONLY way to call one of the tools below is to '
        'list it in the "tool_calls" field of your JSON reply.'
    ) in prompt
