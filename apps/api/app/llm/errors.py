"""Typed errors for `app.llm` providers — kept out of `base.py` so callers
that only need the error type (e.g. `routers/curriculum.py`, for a
try/except) don't need to import the abstract provider interface too.
"""


class LLMError(Exception):
    """One transport/auth-level failure type for ALL providers, carrying the
    classification the caller actually branches on.

    Before Plan 13 the catch sites named the VENDOR: `except
    (openai.APIConnectionError, httpx.TransportError)`, in five places — i.e.
    modules far above the LLM seam knew which SDK was underneath. That is
    exactly the coupling `LLMProvider` exists to prevent: adding Claude would
    otherwise have meant adding `anthropic.APIConnectionError` to five tuples
    and hoping none was missed. A missed one is not a crash: it falls to the
    generic `except Exception` and the job is recorded as `internal`, i.e.
    "our bug", when it was really "your key expired". `jobs/runner.py` now
    catches only this taxonomy; the provider's `_mapped_errors` (llm/claude.py)
    is the single place vendor exceptions are translated.

    `kind` is the whole point:

      "auth"       -> the tutor's Anthropic key is missing, invalid, or out of
                      credit. THE ONE ERROR HE CAN ACTUALLY FIX. Maps to HTTP
                      424 and the message "open Settings". Everything else in
                      this taxonomy is "shrug, retry"; this one is a door.
      "rate_limit" -> 429. Recoverable by waiting. A lesson draft that hits this
                      must go back to `queued`, NOT `failed`, or half a
                      curriculum dies because the tutor generated it too fast.
      "timeout"    -> the network/model didn't answer in time. 504.
      "upstream"   -> the model answered with something unusable. 502.
    """

    def __init__(self, kind: str, message: str = "") -> None:
        self.kind = kind
        super().__init__(message or kind)


class LLMNotConfigured(LLMError):
    """There is no usable Anthropic key — because the tutor has not pasted one
    yet, or because the stored ciphertext no longer decrypts under the current
    `ENCRYPTION_SECRET`.

    DISTINCT FROM `LLMError(kind="auth")`, which means "you gave us a key and
    Anthropic rejected it". Both are the tutor's to fix, but they are different
    sentences and different HTTP codes:

        LLMNotConfigured        -> 409  "no key yet — open Settings"
        LLMError(kind="auth")   -> 424  "your key was rejected — open Settings"

    409 SPECIFICALLY, and not a 500. This is the DEFAULT state of a fresh
    install: the first thing the tutor ever does is open an app that has no key.
    A stack trace at that moment tells a non-technical user the software is
    broken, and he is not wrong to think so. `app/main.py` installs an exception
    handler that turns this into a 409 with a machine-readable `code`, so it can
    never reach a browser as a 500 — including from a call site that had no idea
    a provider was about to be constructed under it.

    It is also checked BEFORE a `GenerationJob` row is created (see
    `app.llm.factory.require_llm_configured`, wired into every enqueue route).
    Discovering "no key" inside a BackgroundTask would produce a job that fails
    with a traceback in `job.error` — the tutor would have a red row in his
    Curricula list and no idea it means "go paste a key".
    """

    def __init__(self, message: str = "") -> None:
        super().__init__("not_configured", message or "No LLM provider is configured.")


class GuidedJSONError(LLMError):
    """Raised by `LLMProvider.guided_json` when the model's response can't be
    used as the requested structured JSON: the model refused (`message.
    content is None`), the response was cut off before the JSON closed
    (`finish_reason == "length"` / `stop_reason == "max_tokens"`), or — as a
    last-resort guard — `json.loads` still raised.

    Distinguishing this from a transport-level error (timeout/connection)
    matters to callers: `routers/curriculum.py`'s generate endpoint maps this
    to a 502 ("the model gave us unusable output, retry") and a transport error
    to a 504 ("we/the network didn't get a response in time, retry") — both
    distinct from an actual unhandled 500.

    Subclasses `LLMError` with `kind="upstream"` and keeps its single-argument
    `(message)` signature, so every `except GuidedJSONError` in
    `jobs/runner.py`, `routers/artifacts.py` and `routers/curriculum.py`
    continues to mean exactly what it meant before. The taxonomy is additive,
    not a rewrite.

    TRUNCATION IS THE ONE TO WATCH under Claude. Greek costs ~2-3x the tokens of
    English per word, and Sonnet 5's tokenizer emits more of them than its
    predecessors — so a lesson draft that fit comfortably under Qwen can hit
    `max_tokens` here. A truncated JSON body surfaces as a *parse* failure, and
    a parse failure gets debugged as "the model produced bad JSON" when the real
    cause is "we didn't give it room to finish". Every `guided_json`
    implementation must therefore check the stop reason FIRST and raise this
    with an explicit truncation message, never let a `JSONDecodeError` stand in
    for it.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__("upstream", message)


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
