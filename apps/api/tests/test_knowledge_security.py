"""Security-hardening tests for the Knowledge Brain API.

Covers the two automated-review findings fixed alongside this file:

1. SSRF on URL ingestion — `app.brain.urlsafe.assert_public_url` (unit-level)
   and its wiring into `POST /knowledge/sources` (router-level: rejected
   with 400 before any real fetch happens).
2. Resource exhaustion — upload size cap, text-length cap, and `k`/`query`
   bounds on the search/ask request schemas.

Deliberately kept DB/network-independent wherever the fix's own contract
allows it: the router rejects bad URLs/oversized text/oversized uploads
*before* touching the DB or making any outbound request, and FastAPI itself
rejects a body that fails Pydantic validation before the endpoint function
(and therefore `run_search`/`run_answer`) ever runs — so those cases need
neither a live Postgres nor a live vLLM server. Only "a normal public host is
allowed" needs real DNS/network, and is marked `integration` for that reason.
"""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.brain.urlsafe import assert_public_url
from app.main import app
from app.routers import knowledge as knowledge_router
from app.schemas.knowledge import AskRequest, SearchRequest

client = TestClient(app)


# ---- Fix 1: assert_public_url (fast, pure — no DB/network needed) ---------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6888",     # loopback — also happens to be a vLLM port
        "http://169.254.169.254/",   # cloud-metadata endpoint
        "http://10.0.0.1",           # RFC1918 private
        "http://qwen-vllm:6888",     # platform-net compose-internal hostname:
                                      # unresolvable from outside the compose
                                      # network (fail-closed) or private if it
                                      # does resolve — either way must raise
    ],
    ids=["loopback", "cloud-metadata", "rfc1918-private", "compose-internal-hostname"],
)
def test_assert_public_url_rejects_private_and_internal_targets(url):
    with pytest.raises(ValueError):
        assert_public_url(url)


def test_assert_public_url_rejects_non_http_scheme():
    with pytest.raises(ValueError):
        assert_public_url("ftp://example.com")


@pytest.mark.integration
def test_assert_public_url_allows_a_normal_public_host():
    assert assert_public_url("https://example.com/") is None  # does not raise


# ---- Fix 1: the guard is actually wired into POST /knowledge/sources ------


def test_create_url_source_rejects_loopback_with_400_before_any_fetch():
    r = client.post(
        "/knowledge/sources",
        json={"kind": "url", "title": "SSRF probe", "url": "http://127.0.0.1:8791"},
    )
    assert r.status_code == 400
    assert "127.0.0.1" in r.json()["detail"]


def test_create_url_source_rejects_cloud_metadata_with_400():
    r = client.post(
        "/knowledge/sources",
        json={"kind": "url", "title": "SSRF probe 2", "url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert r.status_code == 400


# ---- Fix 2: search/ask request schema — k and query bounds ----------------


@pytest.mark.parametrize("bad_k", [0, 999])
def test_search_request_schema_rejects_k_out_of_range(bad_k):
    with pytest.raises(ValidationError):
        SearchRequest(query="what removes hum?", k=bad_k)


@pytest.mark.parametrize("bad_k", [0, 999])
def test_ask_request_schema_rejects_k_out_of_range(bad_k):
    with pytest.raises(ValidationError):
        AskRequest(query="what is a humbucker?", locale="en", k=bad_k)


def test_search_request_schema_accepts_k_equal_to_default():
    assert SearchRequest(query="what removes hum?", k=8).k == 8


def test_ask_request_schema_accepts_k_equal_to_default():
    assert AskRequest(query="what is a humbucker?", locale="en", k=8).k == 8


def test_search_request_schema_rejects_query_over_max_length():
    with pytest.raises(ValidationError):
        SearchRequest(query="x" * 2001)


def test_ask_request_schema_rejects_query_over_max_length():
    with pytest.raises(ValidationError):
        AskRequest(query="x" * 2001, locale="en")


# HTTP-level confirmation that a schema violation surfaces as a real 422 —
# FastAPI rejects the body before the endpoint (and run_search/run_answer,
# which need a live DB + embed server) ever runs, so no live dependency
# is required here either.


def test_search_endpoint_rejects_k_out_of_range_with_422():
    assert client.post("/knowledge/search", json={"query": "hum", "k": 0}).status_code == 422
    assert client.post("/knowledge/search", json={"query": "hum", "k": 999}).status_code == 422


def test_search_endpoint_rejects_oversized_query_with_422():
    r = client.post("/knowledge/search", json={"query": "x" * 2001, "k": 8})
    assert r.status_code == 422


def test_ask_endpoint_rejects_k_out_of_range_with_422():
    body = {"query": "hum", "locale": "en"}
    assert client.post("/knowledge/ask", json={**body, "k": 0}).status_code == 422
    assert client.post("/knowledge/ask", json={**body, "k": 999}).status_code == 422


def test_ask_endpoint_rejects_oversized_query_with_422():
    r = client.post("/knowledge/ask", json={"query": "x" * 2001, "locale": "en"})
    assert r.status_code == 422


# ---- Fix 2: text ingestion and upload size caps ----------------------------


def test_max_text_chars_constant_matches_documented_1_000_000():
    assert knowledge_router.MAX_TEXT_CHARS == 1_000_000


def test_max_upload_bytes_constant_matches_documented_30_mib():
    assert knowledge_router.MAX_UPLOAD_BYTES == 30 * 1024 * 1024


def test_create_text_source_over_cap_rejected_with_413(monkeypatch):
    monkeypatch.setattr(knowledge_router, "MAX_TEXT_CHARS", 10)
    r = client.post(
        "/knowledge/sources",
        json={"kind": "text", "title": "Too Much Text", "text": "x" * 11},
    )
    assert r.status_code == 413


def test_upload_source_over_cap_rejected_with_413(monkeypatch):
    monkeypatch.setattr(knowledge_router, "MAX_UPLOAD_BYTES", 10)
    r = client.post(
        "/knowledge/sources/upload",
        data={"title": "Too Big"},
        files={"file": ("x.pdf", b"x" * 11, "application/pdf")},
    )
    assert r.status_code == 413
