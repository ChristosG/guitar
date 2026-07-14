"""Mint and verify the `gt_session` cookie.

`itsdangerous.URLSafeTimedSerializer` over `settings.app_secret`: the cookie
carries a timestamp and a signature, so expiry is enforced at verification with
no server-side store. There is nothing in the payload worth reading — a valid
signature IS the whole claim ("this browser typed the password") — but we still
put an issued-at marker in it so a token is not a constant string that could be
copied out of a screenshot and remain useful forever without the max-age check
firing.

COOKIE ATTRIBUTES, and why each one:

  HttpOnly       — the token is never read by JS. Nothing in the app needs it;
                   the browser attaches it automatically on `credentials:
                   "include"`.
  SameSite=Lax   — the web app and the API are DIFFERENT HOSTS in production
                   (`guitar.` vs `guitar-api.cgrigoriadis.online`) but the SAME
                   registrable site, so Lax is enough and `None` (which would
                   require Secure and widen CSRF surface) is not needed. Every
                   mutating call from the app is a same-site XHR.
  Secure         — prod only. On plain-http localhost a Secure cookie is
                   silently dropped by the browser, so dev would look like a
                   login that "does nothing".
  Domain         — EMPTY on localhost (host-only). Cookies ignore the PORT, so a
                   host-only cookie set by the API on `localhost:8791` is sent to
                   the web app on `localhost:8790` — dev works with no config. In
                   production `.cgrigoriadis.online` makes one cookie cover both
                   subdomains, which is the whole reason the gate can be a cookie
                   at all rather than a bearer token the SPA has to carry.
"""
from __future__ import annotations

import hmac
import time

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

SESSION_COOKIE = "gt_session"

# A salt distinct from the secret: it namespaces this signer, so a token minted
# here can never be replayed against some other itsdangerous signer that might
# later share `app_secret` (e.g. a password-reset link).
_SALT = "gt-session-v1"


def _serializer() -> URLSafeTimedSerializer:
    """Built per call, not memoized at import: `settings.app_secret` is read
    from the environment, and the tests (and a future secret rotation) expect a
    change to take effect without a process restart."""
    return URLSafeTimedSerializer(settings.app_secret, salt=_SALT)


def issue_token() -> str:
    return _serializer().dumps({"iat": int(time.time())})


def verify_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        _serializer().loads(token, max_age=settings.session_max_age)
    except (BadSignature, SignatureExpired):
        return False
    except Exception:  # a mangled/truncated cookie must be a 401, never a 500
        return False
    return True


def check_password(candidate: str) -> bool:
    """Constant-time compare against `settings.app_password`.

    An EMPTY configured password never matches — not even an empty candidate.
    Fail closed: an operator who turned `AUTH_ENABLED=1` on but forgot to set
    `APP_PASSWORD` must get a locked door, not an open one.
    """
    expected = settings.app_password
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def _cookie_kwargs(request: Request | None = None) -> dict:
    """Cookie attributes derived from THE REQUEST, not from a static constant.

    ONE api container serves BOTH `localhost:8791` and (via nginx)
    `guitar-api.cgrigoriadis.online`. A single hardcoded `COOKIE_DOMAIN`
    therefore cannot be right for both, and getting it wrong is a silent
    redirect loop in whichever host it is wrong for:

      - `Domain=.cgrigoriadis.online` on a localhost login -> the browser REJECTS
        the cookie outright (domain mismatch), and `Secure` alone would reject it
        anyway over plain http. Login returns 200, the app flashes onto /today,
        the next navigation carries no cookie, and `proxy.ts` bounces straight
        back to /login. Which is exactly what happened.
      - host-only on the deployed site -> the cookie is sent back to
        `guitar-api.` on every XHR (so the API authorises fine) but is INVISIBLE
        to the Next.js middleware on `guitar.`, which reads it to decide whether
        to redirect. Same loop, opposite host.

    So `settings.cookie_domain` is a CANDIDATE, not a command: it is applied only
    when the request actually arrives on that domain. `Secure` follows the real
    scheme (honouring `X-Forwarded-Proto`, since nginx terminates TLS), because a
    `Secure` cookie over http is silently discarded.

    A request-less call (logout during tests) falls back to the configured values.
    """
    host = ""
    scheme = "https" if settings.cookie_secure else "http"
    if request is not None:
        host = (request.headers.get("host") or request.url.hostname or "").split(":")[0]
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme

    kw: dict = {
        "httponly": True,
        "samesite": "lax",
        "secure": scheme == "https",
        "path": "/",
    }

    candidate = settings.cookie_domain.lstrip(".")
    if candidate and host and (host == candidate or host.endswith("." + candidate)):
        kw["domain"] = settings.cookie_domain
    elif candidate and request is None:
        kw["domain"] = settings.cookie_domain  # test/logout fallback
    return kw


def set_session_cookie(response: Response, token: str, request: Request | None = None) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=settings.session_max_age, **_cookie_kwargs(request)
    )


def clear_session_cookie(response: Response, request: Request | None = None) -> None:
    """`delete_cookie` must be given the SAME domain/path the cookie was set
    with, or the browser keeps the original and logout silently does nothing."""
    kw = _cookie_kwargs(request)
    response.delete_cookie(
        SESSION_COOKIE, path=kw["path"], domain=kw.get("domain"), samesite="lax",
        httponly=True, secure=kw["secure"],
    )


def is_authenticated(cookies: dict[str, str]) -> bool:
    """The one question the API asks. When the gate is off, everyone is in —
    that keeps `GET /auth/me` honest for a dev/local build where
    `AUTH_ENABLED=0` and there is no password to type."""
    if not settings.auth_enabled:
        return True
    return verify_token(cookies.get(SESSION_COOKIE))
