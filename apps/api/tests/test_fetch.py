"""Tests for the URL-fetch transport seam (Plan 12 Task 1).

Two concerns:

1. `app.brain.fetch.fetch_one_hop` itself: a real (offline, no network)
   exercise of its urllib-based transport — no-redirect-following, status
   passthrough for 2xx/3xx/4xx, and the `max_bytes` read cap actually
   stopping early rather than buffering everything and slicing afterward.
   Uses a throwaway local `http.server` (`_LocalServer` below) instead of
   mocks, so this is a real socket/HTTP round trip through the exact code
   path `safe_fetch_html` depends on.

2. The SSRF regression this whole seam swap must NOT reintroduce: an
   internal/private target is still rejected by `assert_public_url` before
   `fetch_one_hop` (or anything else) ever touches the network, and a
   redirect to one is still blocked by `safe_fetch_html`. These are the
   exact two URLs the task brief calls out (loopback health endpoint,
   cloud-metadata IP).
"""
import http.server
import threading

import pytest

from app.brain.fetch import FetchError, fetch_one_hop
from app.brain.urlsafe import assert_public_url, safe_fetch_html

_HEADERS = {"User-Agent": "test-agent"}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib override)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()
        elif self.path == "/final":
            body = b"final content here"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/notfound":
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"nope")
        elif self.path == "/big":
            body = b"a" * 1_000_000
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args):  # silence test output
        pass


@pytest.fixture(scope="module")
def local_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


# ---- fetch_one_hop: real (local) HTTP behavior -----------------------------


def test_fetch_one_hop_does_not_follow_redirects(local_server):
    result = fetch_one_hop(f"{local_server}/redirect", headers=_HEADERS, timeout=5, max_bytes=10_000)
    assert result.status_code == 302
    assert result.headers["location"] == "/final"
    # No body expected from a redirect — the point is it did NOT chase it.


def test_fetch_one_hop_returns_2xx_body(local_server):
    result = fetch_one_hop(f"{local_server}/final", headers=_HEADERS, timeout=5, max_bytes=10_000)
    assert result.status_code == 200
    assert result.body == b"final content here"
    assert "text/plain" in result.headers["content-type"]


def test_fetch_one_hop_passes_through_4xx_as_data_not_exception(local_server):
    result = fetch_one_hop(f"{local_server}/notfound", headers=_HEADERS, timeout=5, max_bytes=10_000)
    assert result.status_code == 404
    assert result.body == b"nope"


def test_fetch_one_hop_stops_reading_at_max_bytes(local_server):
    result = fetch_one_hop(f"{local_server}/big", headers=_HEADERS, timeout=5, max_bytes=1_000)
    assert len(result.body) == 1_000


def test_fetch_one_hop_raises_fetch_error_on_connection_failure():
    # Nothing listens on this port (pytest doesn't bind it) — a real connect
    # failure, not a mock.
    with pytest.raises(FetchError):
        fetch_one_hop("http://127.0.0.1:1/nope", headers=_HEADERS, timeout=1, max_bytes=1_000)


# ---- SSRF regression: internal/private targets still rejected -------------
#
# The exact URLs named in the task brief: this API's own loopback health
# endpoint, and the cloud-metadata IP. Both must still be rejected after the
# transport swap — a fetch-client change must never be the thing that
# reopens this hole.


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8791/health/live",
        "http://169.254.169.254/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://localhost/",
    ],
    ids=["loopback-health", "cloud-metadata-root", "cloud-metadata-path", "rfc1918", "localhost"],
)
def test_assert_public_url_still_rejects_internal_targets_after_transport_swap(url):
    with pytest.raises(ValueError):
        assert_public_url(url)


def test_safe_fetch_html_never_calls_fetch_one_hop_for_a_rejected_url(monkeypatch):
    """Belt-and-suspenders: prove the guard runs BEFORE the network seam is
    ever invoked, not merely that the guard itself raises somewhere.
    """
    from app.brain import fetch as fetch_module

    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("fetch_one_hop must not be called for a rejected URL")

    monkeypatch.setattr(fetch_module, "fetch_one_hop", _must_not_be_called)

    with pytest.raises(ValueError):
        safe_fetch_html("http://127.0.0.1:8791/health/live")


def test_safe_fetch_html_blocks_redirect_into_cloud_metadata(monkeypatch):
    """Same guarantee as test_url_ssrf_redirect.py, kept here too since this
    is the file the task brief points at for "internal URL still rejected".
    """
    from app.brain import fetch as fetch_module
    from app.brain.fetch import FetchResult

    public_url = "http://93.184.216.34/article"
    calls = []

    def fake_fetch_one_hop(url, **_kwargs):
        calls.append(url)
        if url == public_url:
            return FetchResult(
                status_code=302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
                body=b"",
            )
        raise AssertionError(f"must never fetch the redirect target, but fetched {url!r}")

    monkeypatch.setattr(fetch_module, "fetch_one_hop", fake_fetch_one_hop)

    with pytest.raises(ValueError):
        safe_fetch_html(public_url)

    assert calls == [public_url]
