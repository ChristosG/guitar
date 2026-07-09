"""Typed errors for `app.llm` providers — kept out of `base.py` so callers
that only need the error type (e.g. `routers/curriculum.py`, for a
try/except) don't need to import the abstract provider interface too.
"""


class GuidedJSONError(Exception):
    """Raised by `LLMProvider.guided_json` when the model's response can't be
    used as the requested structured JSON: the model refused (`message.
    content is None`), the response was cut off before the JSON closed
    (`finish_reason == "length"`), or — as a last-resort guard, even though
    vLLM's guided decoding is verified (see `generate.py`'s CURRICULUM_SCHEMA
    comment) to constrain output to schema-valid JSON in the normal case —
    `json.loads` still raised.

    Distinguishing this from a transport-level error (timeout/connection —
    `openai.APIConnectionError`/`httpx.TransportError`) matters to callers:
    `routers/curriculum.py`'s generate endpoint maps this to a 502 ("the
    model gave us unusable output, retry") and a transport error to a 504
    ("we/the network didn't get a response in time, retry") — both distinct
    from an actual unhandled 500.
    """


class ToolArgsError(Exception):
    """Raised by `LLMProvider.chat_tools` when a tool call's `arguments`
    string fails to parse as JSON. The 9B model occasionally emits malformed
    arguments JSON (empirically observed — see
    `/mnt/nvme2TB/vllm_interract/reference/agentic-gotchas.md` §5); this is
    NOT a crash-worthy condition, so `chat_tools` raises this typed error
    instead of letting a raw `json.JSONDecodeError` surface. Task 2's ReAct
    loop catches it and does *bounded* repair — feeds an "ERROR: invalid
    arguments" tool result back so the model can self-correct on the next
    step, capping consecutive failures rather than burning the whole loop
    budget on one stuck call.

    Carries the offending `tool_name` and the raw (unparsed) `arguments`
    string so the loop can build that repair message without re-deriving
    them from the original response.
    """

    def __init__(self, tool_name: str, raw: str) -> None:
        self.tool_name = tool_name
        self.raw = raw
        super().__init__(f"chat_tools: malformed arguments JSON for tool {tool_name!r}: {raw!r}")
