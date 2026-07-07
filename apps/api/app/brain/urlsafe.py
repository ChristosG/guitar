"""SSRF guard for the Knowledge Brain's URL-ingestion path.

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

Known limitation, accepted for this PoC: this validates the *name*, not the
connection the fetcher (`trafilatura`/`httpx`, in `app.brain.extract`) will
actually make a moment later — a DNS answer can legitimately differ between
this check and that later lookup (DNS rebinding), so this does not fully
close the TOCTOU window. Fully closing it means pinning the fetch itself to
the exact IP validated here (e.g. a custom transport that dials that address
directly instead of letting the HTTP client re-resolve the hostname). Worth
doing before this stops being a single-user PoC; not implemented here.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = {"http", "https"}


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
    except socket.gaierror as e:
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
