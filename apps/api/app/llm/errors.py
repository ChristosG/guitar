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
