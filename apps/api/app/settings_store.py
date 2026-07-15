"""Read/write the tutor's runtime LLM configuration (`app_setting`), and turn it
into the one thing `llm/factory.py::get_provider()` needs: an `LLMConfig`.

WHY THIS MODULE EXISTS AT ALL. Until now the API key was an environment
variable, which means it was decided at `docker compose up` and could only be
changed by someone with a shell. The tutor is not that someone. He pastes a key
into a web form, and it has to work on the very next click — no restart, no
redeploy. That single requirement is what forces all three of the following:

1. FERNET, KEYED OFF `settings.encryption_secret`. Anything the tutor types is
   his liability, not ours: the key can charge his card. It is encrypted at rest
   so that a database dump, a `SELECT *` over his shoulder, or a stray backup
   is not a wallet. The secret is deliberately NOT `app_secret` (which signs the
   session cookie) — those two have different rotation stories, and coupling
   them means "log everyone out" and "destroy the stored API key" become the
   same operation.

2. `LLMNotConfigured`, WHICH IS A 409 AND NOT A 500. Before Settings existed,
   "no key" was impossible by construction. Now it is the DEFAULT state of a
   fresh install, and it is the state the tutor is in on the day he first opens
   the app. A traceback is not an answer to a question he can trivially fix; a
   409 with a code the UI turns into "Δεν έχει οριστεί κλειδί — άνοιξε τις
   Ρυθμίσεις" is.

3. THE PROVIDER CACHE MUST BE KEYED ON THE KEY. See `llm/factory.py`.

Decryption of a ciphertext written under a DIFFERENT `encryption_secret` raises
`LLMNotConfigured`, not `InvalidToken`. An operator who rotated the secret has,
from the app's point of view, exactly the same problem as one who never pasted a
key: the fix is to paste it again, and that is what the UI must say.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.llm.errors import LLMNotConfigured
from app.models.setting import DEFAULT_MODEL, MODELS, SINGLETON_ID, AppSetting

log = logging.getLogger(__name__)


# --- encryption -------------------------------------------------------------

def _fernet() -> Fernet:
    """Fernet wants 32 bytes of url-safe-base64 key material; `ENCRYPTION_SECRET`
    is an arbitrary human-supplied string. SHA-256 folds any secret to exactly
    the 32 bytes Fernet requires, deterministically, so the same secret always
    yields the same key and no key-derivation state has to be stored anywhere.

    This is not a password-hashing context (there is no attacker-supplied
    candidate to slow down), so a KDF with a work factor buys nothing here — the
    secret is high-entropy operator-supplied material, not a password.
    """
    digest = hashlib.sha256(settings.encryption_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_key(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_key(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise LLMNotConfigured(
            "The stored Anthropic key could not be decrypted (the encryption "
            "secret changed). Open Settings and paste the key again."
        ) from e


def mask_key(plaintext: str) -> str:
    """THE LAST FOUR CHARACTERS. Nothing else. Not a prefix, not a length, not a
    middle.

    The Settings screen shows `sk-ant-…7f2a` — but the `sk-ant-` part is rendered
    by the BROWSER, from a constant, because every Anthropic key starts with it
    and it therefore carries exactly zero information about *this* key. Putting it
    in the response would mean `GET /settings` returns a body containing the
    substring `sk-ant-`, and the single cheapest, most durable regression test
    this feature can have — grep the response for `sk-ant-` and fail — could no
    longer be written. A test that cannot distinguish "the masked hint" from "the
    whole key echoed by accident" is not a test.

    So: the server returns the four characters that identify WHICH key is saved
    (enough for a tutor with two keys to tell them apart), the UI supplies the
    seven that identify nothing, and `tests/test_settings_api.py` can grep for a
    leak with no false positive.
    """
    return plaintext[-4:] if len(plaintext) >= 8 else "••••"


# --- the row ---------------------------------------------------------------

def load(db: Session) -> AppSetting:
    """The singleton, created on first read. Idempotent by construction — the
    fixed primary key means a concurrent creator collides rather than forking a
    second row (see `models/setting.py`)."""
    row = db.get(AppSetting, SINGLETON_ID)
    if row is None:
        row = AppSetting(id=SINGLETON_ID, model=DEFAULT_MODEL)
        db.add(row)
        db.commit()
    return row


def set_api_key(db: Session, plaintext: str | None) -> AppSetting:
    """`None`/empty clears the key — which is a legitimate action ("remove my
    key from this machine"), not an error."""
    row = load(db)
    row.anthropic_key_ct = encrypt_key(plaintext.strip()) if plaintext and plaintext.strip() else None
    db.commit()
    invalidate()
    return row


def set_model(db: Session, model: str) -> AppSetting:
    if model not in MODELS:
        raise ValueError(f"Unknown model {model!r}. Known: {list(MODELS)}")
    row = load(db)
    row.model = model
    db.commit()
    invalidate()
    return row


# --- the resolved configuration --------------------------------------------

@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str

    @property
    def fingerprint(self) -> tuple[str, str, str]:
        """What `get_provider()`'s cache is keyed on. The key is HASHED, not
        stored: a cache key is the kind of thing that ends up in a repr, a log
        line, or a debugger watch window."""
        return (
            self.provider,
            self.model,
            hashlib.sha256(self.api_key.encode()).hexdigest()[:16],
        )


def _model_from_db() -> str:
    """The model the tutor picked in Settings, or the default. Never raises."""
    try:
        with SessionLocal() as db:
            row = load(db)
            return row.model if row.model in MODELS else DEFAULT_MODEL
    except Exception:
        log.warning("settings_store: could not read app_setting model; using default", exc_info=True)
        return DEFAULT_MODEL


def resolve_llm_config() -> LLMConfig:
    """What provider should we build RIGHT NOW.

    `qwen` (today's default) needs no key: it is a local vLLM server, and the
    whole point of the Settings screen is the *Claude* era. So this returns the
    env-configured Qwen config untouched, and every existing test keeps passing
    without ever touching `app_setting`.

    `claude_cli` needs no key EITHER, and that is not a loophole — it is the
    entire proposition. It spends the tutor's Claude *subscription* through the
    `claude` CLI (see `llm/claude_cli.py`), and a subscription is not an API key:
    a Max plan buys zero API credits. So "no key" is the correct, working,
    steady state for this provider, and raising `LLMNotConfigured` at it — which
    is what the `claude` branch below does, correctly, for its own case — would
    409 a perfectly healthy install and send the tutor to a Settings screen to
    paste something he does not have and does not need.

    It still reads the MODEL from the database, so the Settings screen's picker
    (Sonnet vs Haiku) keeps working across both Claude providers. It must NOT
    fall through to `settings.llm_model` the way `qwen` does: that value is
    `/models/Qwen3.5-9B`, a vLLM filesystem path, and handing it to
    `ClaudeCLIProvider` is a `ValueError` at construction.

    For `claude` the DATABASE WINS over the environment. `ANTHROPIC_API_KEY` in
    the env is a bootstrap/CI convenience (and what Stage 0's smoke test used);
    a key the tutor pasted into the UI is his explicit, most recent intent, and
    an env var that silently overrode it would make the Settings screen a lie.
    """
    provider = settings.llm_provider

    if provider == "claude_cli":
        # api_key="" — there is nothing to hold. The fingerprint still keys the
        # provider cache on (provider, model, sha256("")), so switching Sonnet ->
        # Haiku in Settings still builds a fresh provider rather than serving a
        # stale one, exactly as it does for `claude`.
        return LLMConfig(provider=provider, model=_model_from_db(), api_key="")

    if provider != "claude":
        return LLMConfig(provider=provider, model=settings.llm_model, api_key=settings.llm_api_key)

    model = DEFAULT_MODEL
    key: str | None = None
    try:
        with SessionLocal() as db:
            row = load(db)
            model = row.model if row.model in MODELS else DEFAULT_MODEL
            if row.anthropic_key_ct:
                key = decrypt_key(row.anthropic_key_ct)
    except LLMNotConfigured:
        raise
    except Exception:
        # The DB being unreachable is not "no key configured" — but it also must
        # not crash a health probe with a SQLAlchemy traceback. Fall through to
        # the env key (if any) and let the caller's own DB check report the real
        # problem.
        log.warning("settings_store: could not read app_setting; falling back to env", exc_info=True)

    if not key:
        env_key = settings.llm_api_key
        key = env_key if env_key and env_key != "none" else None

    if not key:
        raise LLMNotConfigured(
            "No Anthropic API key is configured. Open Settings and paste your key."
        )
    return LLMConfig(provider="claude", model=model, api_key=key)


def is_configured() -> bool:
    try:
        resolve_llm_config()
        return True
    except LLMNotConfigured:
        return False


def invalidate() -> None:
    """Drop the provider cache after a write.

    Imported lazily to keep `settings_store` free of an import cycle
    (`llm/factory` imports this module to resolve its config).
    """
    from app.llm.factory import clear_provider_cache

    clear_provider_cache()
