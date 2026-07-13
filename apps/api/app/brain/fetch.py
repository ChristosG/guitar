"""HTTP transport seam for URL ingestion (Plan 12 Task 1).

This is the ONLY place that performs the actual network GET for
`app.brain.urlsafe.safe_fetch_html`. It exists because `httpx` is
TLS-fingerprinted and blocked by some of the tutor's real sources —
confirmed empirically for this task: a plain `httpx.get()` against
`https://en.wikipedia.org/wiki/Guitar_amplifier` with a full browser-like
`User-Agent` gets a bare 403 from Wikimedia's edge (141-byte body), while
`urllib.request` against the SAME URL with the SAME headers, in the same
Python process, gets a normal 200 with the full article (~300KB). Same
request, different client, different result — that is a TLS ClientHello
fingerprint being blocked, not anything about the request itself (this
matches what Plan 9 already found with `curl`). Swapping the transport to
`urllib.request` (stdlib, no new dependency) fixes that without touching
`urlsafe.py`'s SSRF guarantees at all — see the note below.

Security note (read before changing this file): this module implements NO
SSRF protection of its own. It is a dumb, single-hop, non-redirect-following
GET. `assert_public_url` MUST be called by the caller (`urlsafe.
safe_fetch_html`) on the target URL before every call here, and again on
every redirect hop's target before following it. This module's whole job is
to hand back a redirect response (status code + headers, including
`Location`) as plain data instead of chasing it automatically — the caller
decides whether to follow it, and re-validates first.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass


class FetchError(Exception):
    """Any transport-level failure: DNS/connect/timeout/protocol/etc.

    Deliberately one exception type — `safe_fetch_html` collapses this (like
    it did `httpx.HTTPError` before) into its own single `ValueError`, so
    callers with a "never raises" contract (`app.brain.extract._extract_url`)
    only need to catch one thing.
    """


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disables urllib's own redirect-following.

    Returning `None` from `redirect_request` tells urllib "do not build a
    follow-up request for this hop" — urllib's `HTTPErrorProcessor` then
    surfaces the 3xx response as a plain `urllib.error.HTTPError` (status +
    headers + body all still readable off it) instead of silently fetching
    the redirect target itself. That is exactly the hand-off `fetch_one_hop`
    needs: the caller gets the `Location` header back as data and decides
    whether/where to follow it, after re-validating.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 (stdlib override signature)
        return None


# One shared opener: cheap to reuse, and its only job (disabling
# auto-redirect) is stateless per-request anyway.
_opener = urllib.request.build_opener(_NoRedirectHandler)


@dataclass
class FetchResult:
    status_code: int
    headers: dict[str, str]  # lower-cased keys, e.g. headers["location"]
    body: bytes  # already capped at `max_bytes` below


def fetch_one_hop(
    url: str, *, headers: dict[str, str], timeout: float, max_bytes: int
) -> FetchResult:
    """GET `url` once, following NO redirects.

    Never raises for a 3xx/4xx/5xx response — those come back as an
    ordinary `FetchResult` (status_code in range, headers populated from the
    response) so the caller can branch on `status_code` itself, exactly like
    the previous `httpx.stream(..., follow_redirects=False)` call gave
    `safe_fetch_html`. Raises `FetchError` only for a genuine transport
    failure (DNS/connect/timeout/protocol-level) with no response at all.

    Reads the body via bounded `.read(n)` calls that stop once `max_bytes`
    have been read, rather than buffering an arbitrarily large response
    fully in memory first — same resource-exhaustion guarantee the previous
    httpx-based streaming implementation had.
    """
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        response = _opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as e:
        # A non-2xx status (including every 3xx, since _NoRedirectHandler
        # stops them from being auto-followed) still carries a real response
        # — headers, a body to read/close — so it is handled identically to
        # an ordinary response below, not raised further.
        response = e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise FetchError(f"{type(e).__name__}: {e}") from e

    try:
        status_code = getattr(response, "status", None) or response.getcode()
        resp_headers = {k.lower(): v for k, v in response.headers.items()}

        chunks: list[bytes] = []
        total = 0
        while total < max_bytes:
            chunk = response.read(min(65_536, max_bytes - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        body = b"".join(chunks)
    except OSError as e:
        raise FetchError(f"{type(e).__name__}: {e}") from e
    finally:
        response.close()

    return FetchResult(status_code=status_code, headers=resp_headers, body=body)
