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

from fastapi import Response
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


def _cookie_kwargs() -> dict:
    kw: dict = {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.cookie_secure,
        "path": "/",
    }
    if settings.cookie_domain:
        kw["domain"] = settings.cookie_domain
    return kw


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=settings.session_max_age, **_cookie_kwargs()
    )


def clear_session_cookie(response: Response) -> None:
    """`delete_cookie` must be given the SAME domain/path the cookie was set
    with, or the browser keeps the original and logout silently does nothing."""
    kw = _cookie_kwargs()
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
