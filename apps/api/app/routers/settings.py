"""`/settings` — the tutor's API key and model, and the one button that proves
they work.

WHAT `GET /settings` MUST NEVER DO is return the key. It returns
`key_hint: "sk-ant-…7f2a"` — enough to recognise *which* key is saved, useless
to anyone who reads it over his shoulder or finds it in a browser cache.
`tests/test_settings_api.py` greps every response body in this router for the
substring `sk-ant-` and fails if it appears.

WHY `POST /settings/test` MAKES **TWO** CALLS, not one:

  1. `client.models.retrieve(model)` — costs ZERO tokens and is the only probe
     that cleanly separates *401 bad key* from *403 no access / no credit* from
     *404 wrong model* from *429 rate limited*. A `max_tokens=1` ping muddles all
     four into "something went wrong", and on Sonnet 5 it also silently runs
     adaptive thinking, i.e. it is a billed call that answers nothing.

  2. One REAL structured-output generation. This is the part that matters. A key
     can authenticate perfectly and still be unable to produce the app's actual
     output — a workspace with no access to the model, an org policy, a schema
     the account's model tier rejects. A green check that only means "the key
     parses" is worse than no check at all: it sends the tutor off to generate a
     curriculum with a confident tick behind him. Green here means GENERATION
     WORKS.

Failures come back as **HTTP 200 with `ok: false` and a `code`**, not as a 4xx.
A wrong key is not a malformed request — it is the expected outcome of a button
whose entire job is to find out. The browser turns `code` into ONE plain
sentence in the tutor's own language (`settings.errors.*` in `messages/el.json`).
He never sees JSON, a status code, or the word "Anthropic" followed by a number.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import settings_store
from app.config import settings as env
from app.db import get_db
from app.llm.claude import ClaudeProvider
from app.llm.errors import GuidedJSONError, LLMError, LLMNotConfigured
from app.models.setting import MODELS
# `prompts.overrides` is a LEAF (it imports only its own table), so this does not
# invert the layering the way importing `prompts.registry` would: the registry
# imports THIS module to point at `_PROBE_PROMPT`, and a viewer must never become
# load-bearing for the prompts it claims only to watch.
from app.prompts.overrides import resolve

log = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsOut(BaseModel):
    provider: str
    model: str
    configured: bool
    key_hint: str | None = None


class SettingsIn(BaseModel):
    # `None` means "leave it alone" — the Settings form submits the model on its
    # own whenever the tutor switches the radio card, and re-sending a masked
    # placeholder as if it were a key would overwrite the real one with `sk-ant-…`.
    anthropic_key: str | None = Field(default=None)
    model: str | None = Field(default=None)


class TestIn(BaseModel):
    """An unsaved key may be tested. That ordering matters: the natural motion is
    paste -> Test -> Save, and forcing a save first would persist a key the tutor
    has no reason yet to believe in."""
    anthropic_key: str | None = None


class TestOut(BaseModel):
    ok: bool
    model: str
    code: str | None = None


def _to_out(row, *, configured: bool) -> SettingsOut:
    key_hint = None
    if row.anthropic_key_ct:
        try:
            key_hint = settings_store.mask_key(settings_store.decrypt_key(row.anthropic_key_ct))
        except LLMNotConfigured:
            # Ciphertext written under a rotated ENCRYPTION_SECRET. Not a crash:
            # from the tutor's side this is identical to "no key", and the UI
            # says the same thing — paste it again.
            key_hint = None
    return SettingsOut(
        provider=env.llm_provider,
        model=row.model,
        configured=configured,
        key_hint=key_hint,
    )


@router.get("", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)) -> SettingsOut:
    row = settings_store.load(db)
    return _to_out(row, configured=settings_store.is_configured())


@router.put("", response_model=SettingsOut)
def put_settings(payload: SettingsIn, db: Session = Depends(get_db)) -> SettingsOut:
    if payload.model is not None:
        try:
            settings_store.set_model(db, payload.model)
        except ValueError as e:
            raise HTTPException(
                status_code=422, detail={"code": "unknown_model", "message": str(e)}
            ) from e
    if payload.anthropic_key is not None:
        settings_store.set_api_key(db, payload.anthropic_key)

    row = settings_store.load(db)
    return _to_out(row, configured=settings_store.is_configured())


# The smallest possible structured output that still exercises the whole path:
# schema sanitizing (`llm/schema.py`), `output_config.format`, the stop-reason
# check, and the JSON parse. Sanitized by construction — no `minLength`, no
# `minimum`, nothing Claude's structured outputs reject (see `llm/schema.py`).
_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}

# LIFTED OUT OF THE CALL BELOW, BYTE-IDENTICAL, for `app/prompts/registry.py`.
# The registry's one rule is that it POINTS AT a prompt rather than copying it,
# and an inline literal is the one shape that cannot be pointed at: the viewer
# would have to carry its own second copy, which is exactly the drift the
# registry exists to prevent. So the string moved; not one byte of it changed,
# and it is still the only thing this call sends.
_PROBE_PROMPT = "Reply with {\"ok\": true} and nothing else."
_PROBE_SLICE_ID = "settings.probe"


@router.post("/test", response_model=TestOut)
def test_settings(payload: TestIn, db: Session = Depends(get_db)) -> TestOut:
    row = settings_store.load(db)
    model = row.model if row.model in MODELS else MODELS[0]

    key = (payload.anthropic_key or "").strip()
    if not key:
        if not row.anthropic_key_ct:
            return TestOut(ok=False, model=model, code="not_configured")
        try:
            key = settings_store.decrypt_key(row.anthropic_key_ct)
        except LLMNotConfigured:
            return TestOut(ok=False, model=model, code="not_configured")

    provider = ClaudeProvider(api_key=key, model=model)

    # Step 1 — zero tokens. See this module's docstring for why the four
    # outcomes below have to be told apart.
    try:
        provider.client.models.retrieve(model)
    except Exception as e:
        return TestOut(ok=False, model=model, code=_classify(e))

    # Step 2 — one real generation. A green check means this passed.
    try:
        result = provider.guided_json(
            # `_PROBE_PROMPT` contains a LITERAL `{"ok": true}` — it is showing the
            # model the shape to reply in. So it is resolved and never `.format()`-ed.
            [{"role": "user", "content": resolve(db, _PROBE_SLICE_ID, _PROBE_PROMPT)}],
            _PROBE_SCHEMA,
            role="spec",
        )
    except (GuidedJSONError, LLMError) as e:
        return TestOut(ok=False, model=model, code=_classify(e))
    except Exception as e:  # pragma: no cover — an SDK shape we do not know
        log.warning("settings/test: unexpected failure", exc_info=True)
        return TestOut(ok=False, model=model, code=_classify(e))

    if not isinstance(result, dict):
        return TestOut(ok=False, model=model, code="generation_failed")
    return TestOut(ok=True, model=model)


def _classify(exc: Exception) -> str:
    """Anything that can go wrong -> one of six codes the UI has a Greek sentence
    for. Never a message, never a status number: the tutor's screen must not
    contain a single character we did not write in his language.

    The `anthropic` import is inside the function because this module is imported
    at app boot on every deployment, including the Qwen-era one where the SDK is
    installed but never exercised.
    """
    if isinstance(exc, LLMError):
        return {
            "auth": "invalid_key",
            "rate_limit": "rate_limited",
            "timeout": "network",
            "not_configured": "not_configured",
        }.get(exc.kind, "generation_failed")

    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return "network"

    if isinstance(exc, anthropic.AuthenticationError):
        return "invalid_key"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "no_access"
    if isinstance(exc, anthropic.NotFoundError):
        return "unknown_model"
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limited"
    if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return "network"
    return "generation_failed"
