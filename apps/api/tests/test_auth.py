"""The password gate (Plan 13 Task 3.1).

The load-bearing test in here is `test_401_carries_cors_headers`. Everything else
in this file would be caught by a human clicking around; that one would not. A
401 that leaves the middleware stack without an `Access-Control-Allow-Origin`
header is not readable by JS at all — the browser raises
`TypeError: Failed to fetch`, with no status code, and the API log shows an
utterly ordinary 401. There is nothing to grep, nothing to correlate, and the
only way back is to already know that Starlette PREPENDS middleware and that CORS
must therefore be added LAST to end up outermost.
"""
import pytest
from fastapi.testclient import TestClient

from app.auth.session import SESSION_COOKIE, issue_token
from app.config import settings
from app.main import app

ORIGIN = "http://localhost:8790"


@pytest.fixture
def gated(monkeypatch):
    """Turn the gate on for one test. The middleware reads `settings` per
    request (not at construction), which is exactly what makes this possible on
    an app object that was imported with the gate off."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "app_password", "correct horse")
    return TestClient(app)


def test_gate_off_by_default(client):
    """The default posture, and the one ~40 other test files depend on."""
    assert client.get("/knowledge/sources").status_code == 200


def test_protected_route_401s_without_cookie(gated):
    assert gated.get("/knowledge/sources").status_code == 401


def test_401_carries_cors_headers(gated):
    """THE ONE. If this fails, the browser shows `Failed to fetch` and the API
    log shows a clean 401 — a genuinely undebuggable pair. It fails the moment
    `SessionAuthMiddleware` is added AFTER `CORSMiddleware` in `main.py`."""
    res = gated.get("/knowledge/sources", headers={"Origin": ORIGIN})
    assert res.status_code == 401
    assert res.headers.get("access-control-allow-origin") == ORIGIN
    assert res.headers.get("access-control-allow-credentials") == "true"


def test_preflight_is_never_gated(gated):
    """The browser sends `OPTIONS` WITHOUT credentials, by specification. Gating
    it would 401 the preflight and every cross-origin call would fail before the
    real, cookie-carrying request was ever sent."""
    res = gated.options(
        "/knowledge/sources",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == ORIGIN


@pytest.mark.parametrize("path", ["/health/live", "/auth/me"])
def test_exempt_paths(gated, path):
    assert gated.get(path).status_code == 200


def test_login_sets_cookie_and_unlocks(gated):
    res = gated.post("/auth/login", json={"password": "correct horse"})
    assert res.status_code == 200
    assert res.json() == {"authenticated": True, "auth_enabled": True}

    cookie = res.cookies.get(SESSION_COOKIE)
    assert cookie
    # HttpOnly is the whole reason nothing in `lib/api.ts` ever reads this value.
    set_cookie = res.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie

    assert gated.get("/knowledge/sources").status_code == 200


def test_wrong_password_401s_and_sets_no_cookie(gated, monkeypatch):
    monkeypatch.setattr("app.routers.auth._failures", {})
    res = gated.post("/auth/login", json={"password": "nope"})
    assert res.status_code == 401
    assert res.json()["authenticated"] is False
    assert SESSION_COOKIE not in res.cookies


def test_empty_configured_password_never_matches(gated, monkeypatch):
    """Fail closed. `AUTH_ENABLED=1` with `APP_PASSWORD` unset is an operator
    mistake, and the safe reading of it is a locked door — not an open one that
    any empty string opens."""
    monkeypatch.setattr(settings, "app_password", "")
    monkeypatch.setattr("app.routers.auth._failures", {})
    assert gated.post("/auth/login", json={"password": ""}).status_code == 401


def test_repeated_failures_accrue_a_delay(gated, monkeypatch):
    """Per-IP exponential backoff, not a lockout table: an attacker who can guess
    wrong must not be able to lock the tutor out of his own app on demo day."""
    monkeypatch.setattr("app.routers.auth._failures", {})
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("app.routers.auth.asyncio.sleep", fake_sleep)

    for _ in range(4):
        gated.post("/auth/login", json={"password": "nope"})

    # First attempt is never delayed; then 0.25, 0.5, 1.0 — compounding.
    assert slept == [0.25, 0.5, 1.0]

    # A success clears the counter, so the tutor is never punished for a typo.
    gated.post("/auth/login", json={"password": "correct horse"})
    slept.clear()
    gated.post("/auth/login", json={"password": "nope"})
    assert slept == []


def test_logout_clears_the_cookie(gated):
    gated.post("/auth/login", json={"password": "correct horse"})
    assert gated.get("/knowledge/sources").status_code == 200

    gated.post("/auth/logout")
    assert gated.get("/knowledge/sources").status_code == 401


def test_tampered_cookie_is_a_401_not_a_500(gated):
    gated.cookies.set(SESSION_COOKIE, "not-a-real-token")
    assert gated.get("/knowledge/sources").status_code == 401


def test_expired_cookie_is_rejected(gated, monkeypatch):
    token = issue_token()
    monkeypatch.setattr(settings, "session_max_age", -1)
    gated.cookies.set(SESSION_COOKIE, token)
    assert gated.get("/knowledge/sources").status_code == 401


def test_me_reports_the_gate_state(client):
    """With the gate off there is no password to type, so `authenticated` is
    True — the web app uses `auth_enabled` to decide whether a login screen is
    even reachable."""
    assert client.get("/auth/me").json() == {"authenticated": True, "auth_enabled": False}


def test_lifespan_scope_survives_the_middleware():
    """`if scope["type"] != "http"` is line one of `SessionAuthMiddleware.__call__`
    for a reason: the ASGI server hands it the `lifespan` scope at BOOT, and that
    scope has no `method` and no headers. Reaching for either crashes the app at
    startup, before any request exists. Entering the TestClient context runs the
    lifespan — if the guard is gone, this raises here."""
    with TestClient(app):
        pass


# ---------------------------------------------------------------------------
# The cookie DOMAIN is derived from the request host, not from a constant.
#
# ONE api container serves BOTH `localhost:8791` and (via nginx)
# `guitar-api.cgrigoriadis.online`. A single hardcoded COOKIE_DOMAIN cannot be
# right for both, and being wrong is a SILENT REDIRECT LOOP in whichever host it
# is wrong for — the login returns 200, the app flashes onto /today, and the next
# navigation has no cookie because the browser rejected it.
#
# Both directions actually happened, hours apart:
#   prod broke   — host-only cookie was invisible to the Next middleware on the
#                  OTHER subdomain, which reads it to decide whether to redirect.
#   localhost broke — after fixing prod with Domain=.cgrigoriadis.online, the
#                  browser rejected that cookie on localhost (domain mismatch,
#                  plus Secure over plain http).
# ---------------------------------------------------------------------------

def _set_cookie_header(res):
    return res.headers.get("set-cookie", "")


def test_localhost_login_gets_a_host_only_insecure_cookie(gated, monkeypatch):
    """The dev case. A `Domain=.cgrigoriadis.online` cookie is not settable from
    localhost at all, and a `Secure` cookie over plain http is silently dropped —
    either one turns dev login into a bounce back to /login."""
    from app.config import settings

    monkeypatch.setattr(settings, "cookie_domain", ".cgrigoriadis.online")
    monkeypatch.setattr(settings, "cookie_secure", True)

    res = gated.post(
        "/auth/login",
        json={"password": "correct horse"},
        headers={"Host": "localhost:8791"},
    )
    assert res.status_code == 200
    cookie = _set_cookie_header(res)
    assert "gt_session=" in cookie
    assert "Domain=" not in cookie, "a prod Domain on a localhost login is rejected by the browser"
    assert "Secure" not in cookie, "a Secure cookie over plain http is silently dropped"
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()


def test_the_deployed_host_gets_the_shared_parent_domain(gated, monkeypatch):
    """The prod case. Without the leading-dot Domain, the cookie is invisible to
    the Next.js middleware on `guitar.` — which reads it to decide whether to
    bounce to /login — and the tutor loops forever while being, in fact, logged in."""
    from app.config import settings

    monkeypatch.setattr(settings, "cookie_domain", ".cgrigoriadis.online")
    monkeypatch.setattr(settings, "cookie_secure", True)

    res = gated.post(
        "/auth/login",
        json={"password": "correct horse"},
        headers={
            "Host": "guitar-api.cgrigoriadis.online",
            "X-Forwarded-Proto": "https",   # nginx terminates TLS
        },
    )
    assert res.status_code == 200
    cookie = _set_cookie_header(res)
    assert "Domain=.cgrigoriadis.online" in cookie
    assert "Secure" in cookie
