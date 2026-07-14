"""`/auth/*` — the only three endpoints outside the gate (see
`app.auth.middleware.EXEMPT_PREFIXES`: you cannot require a session in order to
create one).

BRUTE FORCE. There is exactly one password and exactly one person who knows it,
so the defence is a per-IP exponential delay on consecutive failures, held in a
process-local dict. Deliberately NOT a lockout table: a lockout is a denial-of-
service against the only user (an attacker who can guess wrong can lock the
tutor out of his own app on demo day), and it needs a row, a migration, and a
reset path. A delay costs an attacker real wall-clock time per attempt and costs
the tutor nothing — his first attempt is never delayed, and one success clears
the counter. It resets on restart, which is fine: the attacker has to still be
there, and the delay compounds again from scratch.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from app.auth.session import (
    SESSION_COOKIE,
    check_password,
    clear_session_cookie,
    is_authenticated,
    issue_token,
)
from app.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])

# ip -> (consecutive failures, unix ts of the last one). Entries older than
# _FORGET_AFTER are treated as absent, so a stale attacker's counter does not
# punish a later, legitimate login from the same NAT'd address.
_failures: dict[str, tuple[int, float]] = {}
_FORGET_AFTER = 15 * 60
_MAX_DELAY_S = 8.0


def _delay_for(ip: str) -> float:
    fails, last = _failures.get(ip, (0, 0.0))
    if fails == 0 or time.time() - last > _FORGET_AFTER:
        return 0.0
    return min(0.25 * (2 ** (fails - 1)), _MAX_DELAY_S)


def _record_failure(ip: str) -> None:
    fails, last = _failures.get(ip, (0, 0.0))
    if time.time() - last > _FORGET_AFTER:
        fails = 0
    _failures[ip] = (fails + 1, time.time())


class LoginIn(BaseModel):
    password: str


class AuthState(BaseModel):
    authenticated: bool
    # The web app needs to know whether the gate is even on: with
    # `AUTH_ENABLED=0` (dev, and every pytest run) there is no password to type,
    # and a login screen would be an unpassable door.
    auth_enabled: bool


@router.get("/me", response_model=AuthState)
def me(request: Request) -> AuthState:
    return AuthState(
        authenticated=is_authenticated(request.cookies),
        auth_enabled=settings.auth_enabled,
    )


@router.post("/login", response_model=AuthState)
async def login(payload: LoginIn, request: Request, response: Response) -> AuthState:
    ip = request.client.host if request.client else "unknown"

    delay = _delay_for(ip)
    if delay:
        await asyncio.sleep(delay)

    if not check_password(payload.password):
        _record_failure(ip)
        # 401 with a CODE, not prose: the browser never renders a server string
        # here. The tutor's language is chosen in the browser, and the one
        # sentence he reads is `login.wrongPassword` from his own locale file.
        response.status_code = 401
        return AuthState(authenticated=False, auth_enabled=settings.auth_enabled)

    _failures.pop(ip, None)
    from app.auth.session import set_session_cookie

    set_session_cookie(response, issue_token())
    return AuthState(authenticated=True, auth_enabled=settings.auth_enabled)


@router.post("/logout", response_model=AuthState)
def logout(response: Response) -> AuthState:
    clear_session_cookie(response)
    return AuthState(authenticated=False, auth_enabled=settings.auth_enabled)


__all__ = ["router", "SESSION_COOKIE"]
