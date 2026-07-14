"""The gate: one pure-ASGI middleware in front of every route.

THREE THINGS HERE ARE LOAD-BEARING AND NONE OF THEM IS OBVIOUS.

1. PURE ASGI, NOT `BaseHTTPMiddleware`. Starlette's `BaseHTTPMiddleware` adapts
   the ASGI response into a `Response` object, and in doing so it BUFFERS a
   streaming body. `POST /chat/{id}/messages/stream` is an SSE endpoint whose
   entire product value is that tokens arrive as they are generated — the same
   dead pause `proxy_buffering off` exists to kill in the nginx config. A
   BaseHTTPMiddleware gate would silently reintroduce it, and the symptom (chat
   feels frozen, then dumps a wall of text) looks nothing like "we added auth".
   A pure-ASGI middleware passes `send` through untouched, so bytes flow.

2. `if scope["type"] != "http": return await self.app(...)` IS LINE ONE. ASGI
   hands this callable the `lifespan` scope at boot and a `websocket` scope if
   one ever appears. Neither has `scope["method"]`, and neither has HTTP
   headers — reaching for them crashes the app at STARTUP, before a single
   request exists, which is a genuinely confusing failure to debug.

3. THIS MIDDLEWARE MUST BE ADDED *BEFORE* `CORSMiddleware` IN `main.py`.
   Starlette PREPENDS on `add_middleware`, so the LAST one added is the
   OUTERMOST. CORS has to be outermost, because a 401 emitted from inside it
   would carry no `Access-Control-Allow-Origin` header — and a cross-origin
   response with no ACAO is not readable by JS at all. The browser reports
   `TypeError: Failed to fetch`, with no status code, and the API log shows a
   perfectly ordinary 401. There is nothing to grep. `tests/test_auth.py`
   asserts the ACAO header is present ON THE 401 for exactly this reason.

Exemptions: `/health/*` (the container's own liveness probe has no cookie),
`/auth/*` (you cannot require a session to create one), and every `OPTIONS`
(the CORS preflight is unauthenticated by specification — the browser sends it
WITHOUT credentials, so gating it makes every cross-origin call fail before the
real request is ever sent).
"""
from __future__ import annotations

import json
from http.cookies import SimpleCookie

from app.auth.session import is_authenticated
from app.config import settings

EXEMPT_PREFIXES = ("/health", "/auth")

_UNAUTHORIZED = json.dumps(
    {"detail": {"code": "unauthenticated", "message": "Sign in to continue."}}
).encode()


def _cookies(scope) -> dict[str, str]:
    jar = SimpleCookie()
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            # A browser may send several `Cookie:` headers; SimpleCookie.load is
            # additive, so loading each in turn is correct.
            jar.load(value.decode("latin-1"))
    return {k: v.value for k, v in jar.items()}


class SessionAuthMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":          # see point 2 — do not move this
            return await self.app(scope, receive, send)

        # Read off `settings` per request, not at construction: the tests flip
        # `auth_enabled` with monkeypatch on an app that is already built.
        if not settings.auth_enabled:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if scope.get("method") == "OPTIONS" or path.startswith(EXEMPT_PREFIXES):
            return await self.app(scope, receive, send)

        if is_authenticated(_cookies(scope)):
            return await self.app(scope, receive, send)

        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_UNAUTHORIZED)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": _UNAUTHORIZED})
