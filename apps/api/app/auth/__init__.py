"""The password gate (Plan 13 Task 3.1).

One tutor, one password, one signed cookie. No user table, no OAuth, no
sessions in Postgres — the cookie IS the session, and it is stateless because
there is nothing to revoke that logging out (or waiting 14 days) doesn't
already handle.

Two modules:
  `session.py`    — mint/verify the `gt_session` cookie; check the password.
  `middleware.py` — the PURE-ASGI gate. Read its docstring before touching it;
                    two of the three things in it are load-bearing in ways that
                    are invisible until production.
"""
from app.auth.session import (  # noqa: F401
    SESSION_COOKIE,
    check_password,
    clear_session_cookie,
    is_authenticated,
    issue_token,
    set_session_cookie,
    verify_token,
)
