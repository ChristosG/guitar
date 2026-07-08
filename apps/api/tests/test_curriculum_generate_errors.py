"""Unit tests for POST /curricula/generate's error mapping.

`generate_curriculum` is monkeypatched to raise before it would ever touch
the DB or the live model, so — unlike `test_curriculum_api.py` — this module
needs neither a live Postgres nor a live LLM/embed stack: `create_engine(...)`
and `SessionLocal()` are both lazy (no connection opens until a query
actually runs), and the monkeypatched function raises before that would
happen. Not marked `@pytest.mark.integration` for the same reason.

Patches `app.routers.curriculum.generate_curriculum` specifically (the name
as bound into the router module's own namespace by its `from ... import
generate_curriculum`), not `app.curriculum.generate.generate_curriculum` —
patching the origin module would not affect the router's already-bound
reference.
"""
import httpx
import openai
import pytest
from fastapi.testclient import TestClient

import app.routers.curriculum as curriculum_router
from app.llm.errors import GuidedJSONError
from app.main import app

client = TestClient(app)

_PAYLOAD = {
    "title": "Test Course",
    "language": "en",
    "profile": {"level": "beginner"},
}


def test_generate_endpoint_maps_guided_json_error_to_502(monkeypatch):
    def _raise(*args, **kwargs):
        raise GuidedJSONError("guided_json: response truncated (finish_reason='length')")

    monkeypatch.setattr(curriculum_router, "generate_curriculum", _raise)

    r = client.post("/curricula/generate", json=_PAYLOAD)

    assert r.status_code == 502, r.text
    assert "try again" in r.json()["detail"].lower()


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.TimeoutException("timed out"),
        openai.APITimeoutError(request=httpx.Request("POST", "http://example.invalid")),
    ],
    ids=["httpx-connect-error", "httpx-timeout", "openai-timeout"],
)
def test_generate_endpoint_maps_transport_errors_to_504(monkeypatch, exc):
    def _raise(*args, **kwargs):
        raise exc

    monkeypatch.setattr(curriculum_router, "generate_curriculum", _raise)

    r = client.post("/curricula/generate", json=_PAYLOAD)

    assert r.status_code == 504, r.text
    assert "timed out" in r.json()["detail"].lower()


def test_generate_endpoint_does_not_map_unrelated_errors_to_502_or_504(monkeypatch):
    """Guard against an over-broad except clause: a genuinely unexpected
    error must still surface as-is (FastAPI's default 500), not get silently
    reclassified as a 502/504.
    """
    def _raise(*args, **kwargs):
        raise RuntimeError("something else entirely")

    monkeypatch.setattr(curriculum_router, "generate_curriculum", _raise)

    with pytest.raises(RuntimeError):
        client.post("/curricula/generate", json=_PAYLOAD)
