"""Regression tests for the redirect-safe, size-capped URL fetch (final Brain
review, MUST-FIX).

`assert_public_url` validated only the caller's *original* hostname, but both
extraction paths (`trafilatura.fetch_url`, and the httpx fallback with
`follow_redirects=True`) then followed redirects themselves, straight past
that guard, to whatever host the response's `Location` header pointed at — a
hostile-but-otherwise-public URL could 302 to `http://169.254.169.254/...`
(cloud metadata) or `http://127.0.0.1:8791` (this API's own port) and have
the server fetch it on the caller's behalf, ingest it, and serve it back out
via `/knowledge/ask`. `safe_fetch_html` (`app.brain.urlsafe`) closes this: it
disables the HTTP client's own redirect-following and re-validates every hop
itself (via `assert_public_url`) before ever requesting it, and it caps the
response body at `max_bytes` via a stream instead of buffering an unbounded
body fully in memory first.

Kept fully network- and DB-independent by monkeypatching `httpx.stream` with
a fake, controllable response — the same technique `test_knowledge_
security.py` uses for `assert_public_url`'s DNS-dependent paths. `PUBLIC_URL`
below is a numeric IP (not a hostname), specifically so `assert_public_url`'s
real `socket.getaddrinfo` call resolves it instantly and locally with no
actual DNS lookup (stdlib behavior for a literal IP address) — deterministic
and offline-safe, while still being a genuinely public, non-private address
per `ipaddress` (so it passes the guard, as a real attacker-supplied public
URL would).
"""
import httpx
import pytest

from app.brain.extract import extract_text
from app.brain.urlsafe import safe_fetch_html

# A real-world public unicast address (historically example.com's), used as
# a stand-in "legitimate public URL" an attacker might submit. Numeric, so
# assert_public_url resolves it without any real DNS/network call.
PUBLIC_URL = "http://93.184.216.34/article"


class _FakeStreamResponse:
    """Stands in for the `httpx.Response` yielded by `with httpx.stream(...) as resp`."""

    def __init__(self, status_code, *, headers=None, chunks=(b"",), encoding="utf-8"):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks
        self.encoding = encoding

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code <= 399

    def iter_bytes(self):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


# ---- Redirect SSRF ----------------------------------------------------------


def test_redirect_to_cloud_metadata_is_rejected_and_never_fetched(monkeypatch):
    calls = []

    def fake_stream(method, url, **kwargs):
        calls.append(url)
        if url == PUBLIC_URL:
            return _FakeStreamResponse(
                302, headers={"location": "http://169.254.169.254/latest/meta-data"}
            )
        raise AssertionError(f"must never fetch the redirect target, but fetched {url!r}")

    monkeypatch.setattr(httpx, "stream", fake_stream)

    with pytest.raises(ValueError):
        safe_fetch_html(PUBLIC_URL)

    # The internal target was validated-and-rejected — never requested.
    assert calls == [PUBLIC_URL]


def test_redirect_to_loopback_is_rejected(monkeypatch):
    def fake_stream(method, url, **kwargs):
        if url == PUBLIC_URL:
            return _FakeStreamResponse(302, headers={"location": "http://127.0.0.1:8791"})
        raise AssertionError(f"must never fetch the redirect target, but fetched {url!r}")

    monkeypatch.setattr(httpx, "stream", fake_stream)

    with pytest.raises(ValueError):
        safe_fetch_html(PUBLIC_URL)


def test_extract_text_returns_empty_list_when_redirect_targets_internal_host(monkeypatch):
    """End-to-end through extract_text: the never-raises contract holds, and
    (per the closure over `fake_stream` below) the internal host is never
    requested either — same guarantee as the safe_fetch_html-level test
    above, exercised through the public extract_text entry point instead.
    """

    def fake_stream(method, url, **kwargs):
        if url == PUBLIC_URL:
            return _FakeStreamResponse(
                302, headers={"location": "http://169.254.169.254/latest/meta-data"}
            )
        raise AssertionError(f"must never fetch the redirect target, but fetched {url!r}")

    monkeypatch.setattr(httpx, "stream", fake_stream)

    assert extract_text("url", url=PUBLIC_URL) == []


def test_redirect_loop_exceeding_max_redirects_raises(monkeypatch):
    """Every hop redirects back to the same public URL — an unbounded loop if
    nothing capped it. Must raise once `max_redirects` is exceeded rather
    than looping (or fetching) forever.
    """

    def fake_stream(method, url, **kwargs):
        return _FakeStreamResponse(302, headers={"location": PUBLIC_URL})

    monkeypatch.setattr(httpx, "stream", fake_stream)

    with pytest.raises(ValueError):
        safe_fetch_html(PUBLIC_URL, max_redirects=2)


# ---- Size cap ---------------------------------------------------------------


def test_size_cap_stops_reading_once_ceiling_is_reached(monkeypatch):
    """Contract: the response body is capped at `max_bytes`, never raised on
    purely for being large — but the stream must actually stop early, not
    just buffer everything and slice afterward (the whole point of using
    `httpx.stream` instead of `.get()` here is to avoid holding an
    attacker-controlled-size body fully in memory before enforcing the cap).
    """
    read_count = {"n": 0}
    chunk = b"a" * 1000

    def _endless_chunks():
        for _ in range(10_000):  # "unbounded" stream; would be ~10 MB if fully drained
            read_count["n"] += 1
            yield chunk

    class _EndlessStreamResponse(_FakeStreamResponse):
        def iter_bytes(self):
            yield from _endless_chunks()

    def fake_stream(method, url, **kwargs):
        return _EndlessStreamResponse(200, headers={"content-type": "text/plain"})

    monkeypatch.setattr(httpx, "stream", fake_stream)

    result = safe_fetch_html(PUBLIC_URL, max_bytes=5_000)

    assert len(result) <= 5_000  # UTF-8 decoding never yields more chars than input bytes
    assert read_count["n"] <= 10  # stopped almost immediately, nowhere near the "endless" 10_000


# ---- Legitimate (non-SSRF) behavior must still work ------------------------


def test_normal_200_response_is_returned_as_text(monkeypatch):
    def fake_stream(method, url, **kwargs):
        assert url == PUBLIC_URL
        return _FakeStreamResponse(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            chunks=[b"<html><body>hello there</body></html>"],
        )

    monkeypatch.setattr(httpx, "stream", fake_stream)

    html = safe_fetch_html(PUBLIC_URL)
    assert "hello there" in html


def test_single_redirect_to_another_public_host_is_followed_successfully(monkeypatch):
    """Ordinary, legitimate redirects (http->https, bare-domain->www, etc.)
    must still work — this fix only blocks redirects INTO non-public space,
    not redirects in general.
    """
    other_public_url = "http://93.184.216.34/final-destination"
    calls = []

    def fake_stream(method, url, **kwargs):
        calls.append(url)
        if url == PUBLIC_URL:
            return _FakeStreamResponse(302, headers={"location": other_public_url})
        if url == other_public_url:
            return _FakeStreamResponse(
                200, headers={"content-type": "text/plain"}, chunks=[b"final content"]
            )
        raise AssertionError(f"unexpected fetch of {url!r}")

    monkeypatch.setattr(httpx, "stream", fake_stream)

    assert safe_fetch_html(PUBLIC_URL) == "final content"
    assert calls == [PUBLIC_URL, other_public_url]


def test_extract_text_url_uses_safe_fetch_and_extracts_body(monkeypatch):
    """Wiring check: extract_text("url", ...) fetches exactly once (via
    safe_fetch_html) and extracts from that same in-memory HTML — proven
    here by only ever answering ONE url with content (anything else raises),
    and asserting the expected text still comes back out through the full
    extract_text path (trafilatura first, falling back to the html.parser
    tag-stripper on the very same string if trafilatura finds nothing usable
    in this tiny synthetic page).
    """
    html = (
        "<html><body><article><p>A humbucker pickup cancels 60-cycle hum "
        "by using two coils wound in opposite polarity.</p></article>"
        "</body></html>"
    )

    def fake_stream(method, url, **kwargs):
        assert url == PUBLIC_URL
        return _FakeStreamResponse(
            200, headers={"content-type": "text/html; charset=utf-8"}, chunks=[html.encode()]
        )

    monkeypatch.setattr(httpx, "stream", fake_stream)

    secs = extract_text("url", url=PUBLIC_URL)
    joined = " ".join(s.text for s in secs).lower()
    assert "60-cycle hum" in joined
