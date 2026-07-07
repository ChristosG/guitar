"""SSRF guard + redirect-safe fetch for the Knowledge Brain's URL-ingestion path.

`assert_public_url` runs BEFORE the router creates/ingests a `kind="url"`
KnowledgeSource, so a hostile, unauthenticated caller (this PoC ships with NO
auth — see the module docstring in `routers/knowledge.py`) cannot make this
server fetch an internal `platform-net` target on its behalf: the vLLM
chat/embed containers (`qwen-vllm:6888`, `qwen-emb-vllm:8090`), Postgres,
`127.0.0.1`, the cloud-metadata endpoint (`169.254.169.254`), etc.

Approach: resolve the URL's hostname via `socket.getaddrinfo` to *every* IP it
maps to, and reject the URL if the scheme isn't http(s), or if ANY resolved
IP is not a plain public unicast address (private/loopback/link-local/
reserved/multicast/unspecified, per the stdlib `ipaddress` module). Resolution
failure is also rejected (fail closed): a name that cannot be resolved here
cannot be validated at all, so it is refused rather than let through.

`safe_fetch_html` (final Brain review, MUST-FIX): validating only the
caller's original hostname was not enough on its own — the fetchers in
`app.brain.extract` used to follow redirects themselves (`trafilatura.
fetch_url` and `httpx.get(..., follow_redirects=True)`), so a hostile but
otherwise-public URL that 302s to `http://169.254.169.254/...` or a
compose-internal host (`http://qwen-emb-vllm:8090`) sailed straight past
`assert_public_url` and got fetched anyway — the guard checked the front
door while the redirect walked in the back. `safe_fetch_html` is now the
ONE fetch used by both extraction paths: it disables the HTTP client's own
redirect-following and instead follows redirects itself, re-running
`assert_public_url` on every hop's target before ever requesting it, and it
caps the response body at `max_bytes` via a stream (stopping partway through
a huge body instead of buffering it fully first).

Known limitation, accepted for this PoC (applies identically to both
functions below): this validates the *name* at each hop, not necessarily the
exact connection the underlying socket will make a moment later — a DNS
answer can legitimately differ between this check and that later lookup
(DNS rebinding), so this does not fully close the TOCTOU window. Fully
closing it means pinning each fetch to the exact IP validated here (e.g. a
custom transport that dials that address directly instead of letting the
HTTP client re-resolve the hostname). Worth doing before this stops being a
single-user PoC; not implemented here.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

_ALLOWED_SCHEMES = {"http", "https"}

_DEFAULT_MAX_BYTES = 5_000_000
_DEFAULT_MAX_REDIRECTS = 5
_DEFAULT_TIMEOUT = 10.0


def assert_public_url(url: str) -> None:
    """Raise ValueError unless `url` is http(s) and every IP its hostname
    resolves to is a plain public, routable unicast address.
    """
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"URL scheme {parts.scheme!r} is not allowed; only http/https may be fetched"
        )

    host = parts.hostname
    if not host:
        raise ValueError(f"URL {url!r} has no hostname to validate")

    try:
        addr_infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError) as e:
        # Broadened beyond socket.gaierror (review pass 2): a different
        # resolver-raised OSError (e.g. a sandboxed/restricted-network
        # environment) must still fail closed into this guard's own
        # ValueError, not escape uncaught as an unhandled 500.
        raise ValueError(f"host {host!r} could not be resolved: {e}") from e
    if not addr_infos:
        raise ValueError(f"host {host!r} did not resolve to any address")

    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        raw_ip = sockaddr[0].split("%", 1)[0]  # drop an IPv6 zone id, if present
        ip = ipaddress.ip_address(raw_ip)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"host {host!r} resolves to {ip}, a non-public address — refusing to fetch it"
            )


def safe_fetch_html(
    url: str,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    """Fetch `url` and return its decoded body text, redirect-safely.

    Unlike a plain `httpx.get(url, follow_redirects=True)` (the SSRF hole this
    closes — see the module docstring above), this:

    1. Validates `url` itself with `assert_public_url` before touching the
       network.
    2. Disables the HTTP client's own redirect-following and instead follows
       redirects one hop at a time: on a 3xx response, it resolves the
       `Location` header against the current URL and re-runs
       `assert_public_url` on THAT before ever requesting it. A redirect to a
       disallowed host raises right there — the disallowed host is never
       requested. More than `max_redirects` hops also raises.
    3. Reads the final 2xx response body via a stream, stopping as soon as
       `max_bytes` have been read rather than buffering an arbitrarily large
       body fully in memory first (resource-exhaustion hardening — the
       previous httpx fallback had no size cap at all).

    Raises ValueError for: a disallowed URL/host at any hop (fail-closed, same
    as `assert_public_url`), a redirect with no `Location` header, more than
    `max_redirects` hops, a non-2xx final response, or any underlying `httpx`
    transport error (connect/timeout/protocol failures) — a single exception
    type so callers that need extract_text's "never raises" contract
    (`app.brain.extract._extract_url`) only need to catch one thing.
    """
    current = url
    for _hop in range(max_redirects + 1):
        assert_public_url(current)
        try:
            with httpx.stream("GET", current, follow_redirects=False, timeout=timeout) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise ValueError(
                            f"redirect response from {current!r} had no Location header"
                        )
                    # Resolve (possibly relative/protocol-relative) against the
                    # CURRENT hop's URL, then loop: the next iteration's
                    # assert_public_url call is what actually guards this.
                    current = urljoin(current, location)
                    continue

                if not (200 <= resp.status_code < 300):
                    raise ValueError(
                        f"fetch of {current!r} failed with status {resp.status_code}"
                    )

                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        break  # stop reading now — never buffer the rest of a huge body
                raw = b"".join(chunks)[:max_bytes]
                encoding = resp.encoding or "utf-8"
                try:
                    return raw.decode(encoding, errors="replace")
                except LookupError:
                    # An unrecognized/unsupported declared charset — fall back
                    # rather than raise on otherwise-good, size-capped content.
                    return raw.decode("utf-8", errors="replace")
        except httpx.HTTPError as e:
            raise ValueError(f"fetch of {current!r} failed: {e}") from e

    raise ValueError(f"too many redirects (> {max_redirects}) starting from {url!r}")
