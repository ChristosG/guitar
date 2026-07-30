"""Unit tests for POST /artifacts/generate's error mapping.

`generate_artifact` is monkeypatched to raise before it would ever touch the
DB or the live model, so — unlike `test_artifacts_api.py` — this module needs
neither a live Postgres nor a live LLM/embed stack: `create_engine(...)` and
`SessionLocal()` are both lazy (no connection opens until a query actually
runs), and the monkeypatched function raises before that would happen. Not
marked `@pytest.mark.integration` for the same reason. Mirrors
`test_curriculum_generate_errors.py` exactly (same 502/504 mapping, same
GuidedJSONError/transport-error precedent) plus one mapping curriculum's
generate endpoint doesn't have: a `ValueError` (unknown kind, or a
`pydantic.ValidationError` surviving the one repair retry — see
`generate_artifact`'s docstring) -> 422.

Patches `app.routers.artifacts.generate_artifact` specifically (the name as
bound into the router module's own namespace by its `from ... import
generate_artifact`), not `app.artifacts.generate.generate_artifact` —
patching the origin module would not affect the router's already-bound
reference (same precedent `test_curriculum_generate_errors.py` documents).
"""
import pytest
from fastapi.testclient import TestClient

import app.routers.artifacts as artifacts_router
from app.llm.errors import GuidedJSONError, LLMError
from app.main import app

client = TestClient(app)

_PAYLOAD = {"kind": "chord_diagram", "prompt": "G major open chord"}


def test_generate_endpoint_maps_guided_json_error_to_502(monkeypatch):
    def _raise(*args, **kwargs):
        raise GuidedJSONError("guided_json: response truncated (finish_reason='length')")

    monkeypatch.setattr(artifacts_router, "generate_artifact", _raise)

    r = client.post("/artifacts/generate", json=_PAYLOAD)

    assert r.status_code == 502, r.text
    assert "try again" in r.json()["detail"].lower()


@pytest.mark.parametrize(
    ("exc", "expected_status"),
    [
        (LLMError("timeout", "request timed out"), 504),
        (LLMError("upstream", "upstream 502"), 502),
        (LLMError("rate_limit", "rate limited"), 502),
    ],
    ids=["llm-timeout", "llm-upstream", "llm-rate-limit"],
)
def test_generate_endpoint_maps_llm_errors(monkeypatch, exc, expected_status):
    """The provider seam raises `LLMError` with a `kind`; the router maps
    `timeout` -> 504 and everything else upstream-shaped -> 502. (The
    openai/httpx transport tuple this covered died with the vLLM era.)"""
    def _raise(*args, **kwargs):
        raise exc

    monkeypatch.setattr(artifacts_router, "generate_artifact", _raise)

    r = client.post("/artifacts/generate", json=_PAYLOAD)

    assert r.status_code == expected_status, r.text
    assert "try again" in r.json()["detail"].lower()


def test_generate_endpoint_maps_value_error_to_422(monkeypatch):
    """Distinct from curriculum's generate endpoint: artifact generation can
    genuinely still fail structurally after the one repair retry (see
    `generate_artifact`'s docstring), surfacing as a `pydantic.
    ValidationError` (a `ValueError` subclass) or a plain `ValueError` for an
    unrecognized `kind` — both map to 422, not a raw 500.
    """
    def _raise(*args, **kwargs):
        raise ValueError("unknown artifact kind: 'banjo_diagram'")

    monkeypatch.setattr(artifacts_router, "generate_artifact", _raise)

    r = client.post("/artifacts/generate", json={"kind": "banjo_diagram", "prompt": "anything"})

    assert r.status_code == 422, r.text
    assert "banjo_diagram" in r.json()["detail"]


def test_generate_endpoint_does_not_map_unrelated_errors_to_502_504_or_422(monkeypatch):
    """Guard against an over-broad except clause: a genuinely unexpected
    error must still surface as-is (FastAPI's default 500), not get silently
    reclassified.
    """
    def _raise(*args, **kwargs):
        raise RuntimeError("something else entirely")

    monkeypatch.setattr(artifacts_router, "generate_artifact", _raise)

    with pytest.raises(RuntimeError):
        client.post("/artifacts/generate", json=_PAYLOAD)
