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

Review pass 2 additions (still DB/network-independent, same reasoning as
above — the guard raises before any DB write or fetch either way):
3. Broadened DNS-resolution exception in `assert_public_url` — a resolver
   OSError that isn't a `socket.gaierror` must still fail closed into the
   guard's own `ValueError`, not escape as an unhandled 500.
4. The SSRF `ValueError`'s detailed message (which host, which resolved IP)
   must not reach the caller — `routers/knowledge.py` now logs it server-side
   and raises a generic `HTTPException(400, detail="URL not allowed")`
   instead. (The total-ingested-text cap, the third review-pass-2 finding,
   is DB-dependent and lives in `test_ingest.py` alongside the rest of
   `ingest_source`'s pipeline tests instead of here.)
"""
import socket

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


def test_assert_public_url_wraps_non_gaierror_oserror_as_valueerror(monkeypatch):
    """Fix 2 (review pass 2): before this fix, the resolution try/except
    caught only `socket.gaierror`; any other resolver-raised `OSError` (e.g.
    a sandboxed/restricted-network environment, or a transient resolver
    failure surfaced differently) would propagate uncaught out of
    `assert_public_url` as a raw `OSError` -> an unhandled 500 at the router,
    instead of this guard's own fail-closed `ValueError`.
    """

    def _boom(*_args, **_kwargs):
        raise OSError("simulated resolver failure, not a socket.gaierror")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)

    with pytest.raises(ValueError):
        assert_public_url("https://example.com/")


# ---- Fix 1: the guard is actually wired into POST /knowledge/sources ------


def test_create_url_source_rejects_loopback_with_400_and_generic_detail():
    """Also covers Fix 3 (review pass 2): the detailed internal reason (which
    host, which resolved IP) must NOT reach the caller — only the generic
    "URL not allowed" detail crosses the HTTP boundary; the specific reason is
    logged server-side instead (`routers/knowledge.py`'s `log.warning` in the
    `except ValueError` branch around `assert_public_url`).
    """
    r = client.post(
        "/knowledge/sources",
        json={"kind": "url", "title": "SSRF probe", "url": "http://127.0.0.1:8791"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "URL not allowed"
    assert "127.0.0.1" not in r.json()["detail"]


def test_create_url_source_rejects_cloud_metadata_with_400():
    r = client.post(
        "/knowledge/sources",
        json={"kind": "url", "title": "SSRF probe 2", "url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert r.status_code == 400


# ---- Fix 1 (Plan 12 Task 1): the guard also runs per-URL in the bulk path -


def test_bulk_sources_rejects_internal_urls_honestly_without_touching_db():
    """`POST /knowledge/sources/bulk` runs the same SSRF guard per URL, before
    creating any row or fetching anything — so an all-internal-URLs request
    needs neither a live DB nor a live embed server (same reasoning as the
    single-source SSRF tests above). Each rejected URL is reported as
    `"rejected"`, never `"ready"` or silently dropped — spec D6's "never a
    green lie" applies at the batch level too.
    """
    r = client.post(
        "/knowledge/sources/bulk",
        json={
            "urls": [
                "http://127.0.0.1:8791/health/live",
                "http://169.254.169.254/latest/meta-data/",
            ]
        },
    )
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert len(results) == 2
    for result in results:
        assert result["status"] == "rejected"
        assert result["source_id"] is None
        assert result["error"] == "URL not allowed"
        assert "127.0.0.1" not in result["error"]
        assert "169.254" not in result["error"]


def test_bulk_sources_rejects_blank_url_entry():
    r = client.post("/knowledge/sources/bulk", json={"urls": ["   "]})
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["status"] == "rejected"
    assert result["source_id"] is None


def test_bulk_sources_rejects_empty_url_list_with_422():
    r = client.post("/knowledge/sources/bulk", json={"urls": []})
    assert r.status_code == 422


def test_bulk_sources_rejects_oversized_url_list_with_422():
    r = client.post("/knowledge/sources/bulk", json={"urls": ["http://example.com/"] * 21})
    assert r.status_code == 422


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


def test_max_upload_bytes_constant_matches_documented_60_mib():
    # 60 MiB (raised from 30): the tutor's largest real book is 26 MB, and
    # scans of long books routinely run 40-50 MB — the old cap left the very
    # next book he buys one bounce away. See the constant's own comment.
    assert knowledge_router.MAX_UPLOAD_BYTES == 60 * 1024 * 1024


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


def test_upload_rejects_non_pdf_with_415_and_a_machine_code():
    """The route hard-codes `type="pdf"`; a JPEG picked through "All Files"
    used to sail in, explode inside `fitz.open`, and leave a red row whose
    error was MuPDF's own English with a dead-end Retry. Magic bytes, checked
    before any row exists — and a `{code, message}` detail so the dialog can
    render its own localized sentence."""
    r = client.post(
        "/knowledge/sources/upload",
        data={"title": "Not a PDF"},
        files={"file": ("x.jpg", b"\xff\xd8\xff\xe0 not a pdf at all", "image/jpeg")},
    )
    assert r.status_code == 415
    assert r.json()["detail"]["code"] == "upload_not_pdf"


def test_upload_same_bytes_twice_is_409_naming_the_existing_row(tmp_path, monkeypatch):
    """THE DUPLICATE GUARD: two ids for the same book become two compiles, and
    canon divergence — keyed on distinct source ids — then shows the same book
    "disagreeing with itself" on the product's headline surface."""
    monkeypatch.setattr("app.brain.paginate.settings.media_dir", str(tmp_path))

    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "A humbucker pickup cancels 60-cycle hum.")
    data = doc.tobytes()
    doc.close()

    first = client.post(
        "/knowledge/sources/upload",
        data={"title": "Tone Book"},
        files={"file": ("tone.pdf", data, "application/pdf")},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/knowledge/sources/upload",
        data={"title": "Tone Book again"},
        files={"file": ("tone-copy.pdf", data, "application/pdf")},
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["code"] == "upload_duplicate"
    assert detail["existing_title"] == "Tone Book"
