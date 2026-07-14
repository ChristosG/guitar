"""The Settings screen's backend (Plan 13 Tasks 3.4/3.5).

Three of these tests exist because of a specific way this feature can fail in a
way nobody notices until it is in front of the tutor:

  * `test_no_response_ever_contains_the_key` — greps every body this router can
    produce for `sk-ant-`. An echoed key is invisible in the UI and permanent in
    a browser cache.
  * `test_provider_cache_is_keyed_on_the_key` — the `lru_cache` trap. If it comes
    back, "I fixed my key" stops working until someone restarts the container,
    and the tutor concludes the app is broken at the exact moment he was closest
    to giving up.
  * `test_enqueue_without_a_key_is_409_not_500` — the default state of a fresh
    install must not be a stack trace.
"""
import pytest
from cryptography.fernet import Fernet

from app import settings_store
from app.config import settings as env
from app.llm.errors import LLMNotConfigured
from app.llm.factory import clear_provider_cache, get_provider
from app.models.setting import SINGLETON_ID, AppSetting

FAKE_KEY = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF-7f2a"


@pytest.fixture
def claude(monkeypatch):
    """Today's default provider is still `qwen` (the embedding column is
    2560-dim until Stage 4's migration). Settings only means anything in the
    Claude era, so every test here flips the provider explicitly."""
    monkeypatch.setattr(env, "llm_provider", "claude")
    monkeypatch.setattr(env, "llm_api_key", "none")   # no env-key fallback
    clear_provider_cache()
    yield
    clear_provider_cache()


# --- GET / PUT --------------------------------------------------------------

def test_get_settings_on_a_fresh_install(client, claude):
    body = client.get("/settings").json()
    assert body == {
        "provider": "claude",
        "model": "claude-sonnet-5",
        "configured": False,
        "key_hint": None,
    }


def test_put_key_stores_ciphertext_and_returns_a_hint(client, claude, db):
    body = client.put("/settings", json={"anthropic_key": FAKE_KEY}).json()
    assert body["configured"] is True
    assert body["key_hint"] == "7f2a"      # last four only — see `mask_key`

    row = db.get(AppSetting, SINGLETON_ID)
    assert row.anthropic_key_ct
    assert FAKE_KEY not in row.anthropic_key_ct       # not plaintext
    assert settings_store.decrypt_key(row.anthropic_key_ct) == FAKE_KEY


def test_no_response_ever_contains_the_key(client, claude):
    """Grep, not inspect. A future refactor that adds `anthropic_key` back to
    `SettingsOut` would pass every other test in this file.

    This is exactly why `mask_key` returns the last four characters and NOT
    `sk-ant-…7f2a`: the `sk-ant-` prefix is a constant the browser can render
    itself, and if the API emitted it, this grep could not tell a masked hint
    apart from a leaked key.""" 
    bodies = [
        client.put("/settings", json={"anthropic_key": FAKE_KEY}).text,
        client.get("/settings").text,
        client.put("/settings", json={"model": "claude-haiku-4-5"}).text,
    ]
    for body in bodies:
        assert "sk-ant-" not in body, body


def test_put_model(client, claude, db):
    assert client.put("/settings", json={"model": "claude-haiku-4-5"}).json()["model"] == (
        "claude-haiku-4-5"
    )
    assert db.get(AppSetting, SINGLETON_ID).model == "claude-haiku-4-5"


def test_put_unknown_model_is_422(client, claude):
    res = client.put("/settings", json={"model": "gpt-9"})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "unknown_model"


def test_put_model_alone_does_not_clear_the_key(client, claude):
    """`anthropic_key: None` means "leave it alone". The form sends the model on
    its own every time the tutor clicks a radio card; if that wiped the key, the
    act of preferring Haiku would silently log him out of Anthropic."""
    client.put("/settings", json={"anthropic_key": FAKE_KEY})
    body = client.put("/settings", json={"model": "claude-haiku-4-5"}).json()
    assert body["configured"] is True
    assert body["key_hint"] == "7f2a"


def test_empty_key_clears_it(client, claude):
    client.put("/settings", json={"anthropic_key": FAKE_KEY})
    body = client.put("/settings", json={"anthropic_key": ""}).json()
    assert body["configured"] is False
    assert body["key_hint"] is None


# --- encryption at rest -----------------------------------------------------

def test_roundtrip_through_fernet():
    assert settings_store.decrypt_key(settings_store.encrypt_key(FAKE_KEY)) == FAKE_KEY


def test_a_rotated_encryption_secret_reads_as_not_configured(client, claude, db, monkeypatch):
    """Rotating ENCRYPTION_SECRET must not 500 the Settings page. From the
    tutor's side this is indistinguishable from "no key", and the fix is the
    same: paste it again. That is what the UI has to be able to say."""
    client.put("/settings", json={"anthropic_key": FAKE_KEY})
    monkeypatch.setattr(env, "encryption_secret", Fernet.generate_key().decode())
    clear_provider_cache()

    body = client.get("/settings").json()
    assert body["configured"] is False
    assert body["key_hint"] is None


def test_rotating_the_session_secret_does_not_touch_the_key(client, claude, monkeypatch):
    """The reason `app_secret` and `encryption_secret` are two settings and not
    one: logging everyone out must not also destroy the stored API key."""
    client.put("/settings", json={"anthropic_key": FAKE_KEY})
    monkeypatch.setattr(env, "app_secret", "a-completely-different-session-secret")
    assert client.get("/settings").json()["configured"] is True


# --- the provider cache -----------------------------------------------------

def test_provider_cache_is_keyed_on_the_key(client, claude, monkeypatch):
    """THE `lru_cache` TRAP. A key pasted at runtime must take effect on the very
    next call — no restart. The cache key contains a hash of the key itself, so a
    changed key is not a stale entry, it is a different entry."""
    client.put("/settings", json={"anthropic_key": FAKE_KEY})
    first = get_provider()
    assert get_provider() is first          # same config -> cached, not rebuilt

    client.put("/settings", json={"anthropic_key": "sk-ant-api03-a-different-one"})
    second = get_provider()
    assert second is not first
    assert second._api_key == "sk-ant-api03-a-different-one"


def test_model_switch_rebuilds_the_provider(client, claude):
    client.put("/settings", json={"anthropic_key": FAKE_KEY})
    sonnet = get_provider()
    client.put("/settings", json={"model": "claude-haiku-4-5"})
    haiku = get_provider()
    assert haiku is not sonnet
    assert haiku._model == "claude-haiku-4-5"


def test_get_provider_without_a_key_raises_not_configured(claude):
    with pytest.raises(LLMNotConfigured):
        get_provider()


def test_qwen_needs_no_key(monkeypatch):
    """The Qwen era is untouched by any of this: a local vLLM server has no key,
    and ~40 existing tests depend on `get_provider()` never raising."""
    monkeypatch.setattr(env, "llm_provider", "qwen")
    clear_provider_cache()
    assert settings_store.is_configured() is True
    clear_provider_cache()


# --- LLMNotConfigured is a 409, everywhere ---------------------------------

def test_enqueue_without_a_key_is_409_not_500(client, claude):
    """A fresh install's first click must not be a stack trace — and it must not
    create a `GenerationJob` that fails with a traceback in `job.error` either,
    which is what "check it inside the background task" would produce."""
    res = client.post(
        "/curricula/generate",
        json={"title": "Blues", "weeks": 4, "minutes_per_session": 45},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "llm_not_configured"


def test_no_job_row_is_created_when_the_key_is_missing(client, claude, db):
    from app.models.generation_job import GenerationJob

    client.post(
        "/curricula/generate",
        json={"title": "Blues", "weeks": 4, "minutes_per_session": 45},
    )
    assert db.query(GenerationJob).count() == 0


# --- /settings/test ---------------------------------------------------------

class _FakeClient:
    def __init__(self, models_error=None):
        self.models = self
        self._error = models_error

    def retrieve(self, model):
        if self._error:
            raise self._error
        return {"id": model}


class _FakeProvider:
    """Stands in for `ClaudeProvider`. Constructed with the same kwargs, so a
    signature drift here fails loudly rather than silently testing nothing."""

    instances: list["_FakeProvider"] = []
    models_error: Exception | None = None
    guided_error: Exception | None = None
    guided_result: dict = {"ok": True}

    def __init__(self, *, api_key: str, model: str):
        self.api_key, self.model = api_key, model
        self.guided_calls: list = []
        _FakeProvider.instances.append(self)

    @property
    def client(self):
        return _FakeClient(self.models_error)

    def guided_json(self, messages, schema, *, role="spec", **kw):
        self.guided_calls.append((messages, schema, role))
        if self.guided_error:
            raise self.guided_error
        return self.guided_result


@pytest.fixture
def fake_provider(monkeypatch):
    _FakeProvider.instances = []
    _FakeProvider.models_error = None
    _FakeProvider.guided_error = None
    _FakeProvider.guided_result = {"ok": True}
    monkeypatch.setattr("app.routers.settings.ClaudeProvider", _FakeProvider)
    return _FakeProvider


def test_test_with_no_key_at_all(client, claude, fake_provider):
    body = client.post("/settings/test", json={}).json()
    assert body == {"ok": False, "model": "claude-sonnet-5", "code": "not_configured"}


def test_test_makes_a_real_generation_call_not_just_a_key_check(client, claude, fake_provider):
    """A green check must mean GENERATION WORKS. `models.retrieve` alone only
    proves the key parses — and a tick that means "the key parses" sends the
    tutor off to spend an hour authoring a curriculum that cannot be produced."""
    client.put("/settings", json={"anthropic_key": FAKE_KEY})
    body = client.post("/settings/test", json={}).json()

    assert body == {"ok": True, "model": "claude-sonnet-5", "code": None}
    assert len(fake_provider.instances[-1].guided_calls) == 1


def test_test_can_check_an_unsaved_key(client, claude, fake_provider):
    """paste -> Test -> Save. Forcing a save first would persist a key the tutor
    has no reason yet to believe in."""
    body = client.post("/settings/test", json={"anthropic_key": FAKE_KEY}).json()
    assert body["ok"] is True
    assert fake_provider.instances[-1].api_key == FAKE_KEY
    assert client.get("/settings").json()["configured"] is False   # still unsaved


@pytest.mark.parametrize(
    "exc_name, code",
    [
        ("AuthenticationError", "invalid_key"),
        ("PermissionDeniedError", "no_access"),
        ("NotFoundError", "unknown_model"),
        ("RateLimitError", "rate_limited"),
    ],
)
def test_test_classifies_each_failure_into_one_code(
    client, claude, fake_provider, exc_name, code
):
    """Four outcomes the tutor must be told apart — bad key / no access or no
    credit / wrong model / rate limited. This is why the probe is
    `models.retrieve` (which distinguishes them, for zero tokens) and not a
    one-token ping (which does not)."""
    import anthropic
    import httpx

    request = httpx.Request("GET", "https://api.anthropic.com/v1/models")
    response = httpx.Response(400, request=request)
    fake_provider.models_error = getattr(anthropic, exc_name)(
        exc_name, response=response, body=None
    )

    body = client.post("/settings/test", json={"anthropic_key": FAKE_KEY}).json()
    assert body == {"ok": False, "model": "claude-sonnet-5", "code": code}


def test_test_reports_a_generation_failure_separately(client, claude, fake_provider):
    from app.llm.errors import GuidedJSONError

    fake_provider.guided_error = GuidedJSONError("truncated")
    body = client.post("/settings/test", json={"anthropic_key": FAKE_KEY}).json()
    assert body["ok"] is False
    assert body["code"] == "generation_failed"


def test_test_never_returns_a_4xx_for_a_bad_key(client, claude, fake_provider):
    """A wrong key is the EXPECTED outcome of a button whose job is to find out.
    200 with `ok: false` keeps the frontend's error path from ever having to
    render a status code."""
    import anthropic
    import httpx

    fake_provider.models_error = anthropic.AuthenticationError(
        "bad", response=httpx.Response(401, request=httpx.Request("GET", "https://x")), body=None
    )
    assert client.post("/settings/test", json={"anthropic_key": "sk-ant-nope"}).status_code == 200
