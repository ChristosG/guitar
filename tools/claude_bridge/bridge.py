#!/usr/bin/env python3
"""claude-bridge — run `claude -p`, serve it to the API container.

Runs as its OWN container (`claude-bridge` in docker-compose.yml, built from the
Dockerfile beside this file). `docker compose up -d --build` starts it with
everything else; there is nothing to run by hand.

WHY ITS OWN CONTAINER, AND NOT `claude` INSIDE `apps/api`. This is a security
boundary, not packaging taste. The credential it needs is not one token:

  `~/.claude/.credentials.json` holds `claudeAiOauth` AND live OAuth access +
  refresh tokens for every MCP server the user has ever authorised — on this
  machine that is Linear, GitLab, Postman, HuggingFace, PagerDuty, Rootly, Apollo
  and Logfire.

The `api` container is the web-facing one: it parses uploads, serves a chat box,
and drives an agent loop over text a user typed. It is the thing worth attacking,
and it must not hold any of that. So the credential is mounted HERE instead — into
a container that runs exactly one program (`claude -p`, with every built-in tool
disabled via `--tools ""`), holds no database, serves no browser, and publishes no
port to the host. `api` reaches it over the internal compose network only.

AND WHY THE REAL `~/.claude` DIRECTORY, read-write (see the compose file's mount):

  - A COPY of the credential would give you two independent OAuth sessions off one
    refresh token. The access token expires in hours and Claude Code rewrites the
    file when it refreshes; whichever copy rotates first can invalidate the other,
    and the symptom is the user's own terminal `claude` demanding a re-login for no
    visible reason.
  - A single-FILE bind mount breaks on the first refresh: the CLI writes a new file
    and renames it over the old one, so the host gets a fresh inode while the
    container stays bound to the stale one. They diverge silently.

Mounting the DIRECTORY means host and container share one file — exactly what two
`claude` terminals already do, which is normal and supported. `user: "1000:1000"`
in the compose file is what keeps the files it writes owned by the human.

WHAT THIS IS NOT. It is not the Anthropic API and does not pretend to be. It
speaks one small verb (`/v1/complete`) shaped for `app/llm/claude_cli.py`, the
only client that exists. When the tutor buys an API key, `LLM_PROVIDER=claude`
(the real SDK, `app/llm/claude.py`) takes over and this whole directory becomes
dead weight — which is the intended end state, not a regret.

Stdlib only, on purpose: no pip, no venv, no requirements to drift.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

log = logging.getLogger("claude-bridge")

# --- configuration ----------------------------------------------------------

TOKEN = os.environ.get("CLAUDE_BRIDGE_TOKEN", "")
HOST = os.environ.get("CLAUDE_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("CLAUDE_BRIDGE_PORT", "8799"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# How many `claude` processes may run at once. Each is a Node process (~1s of
# boot, a few hundred MB), and the API's own `draft_concurrency` fan-out can ask
# for several at once. This is the backstop that keeps a curriculum draft from
# forking twelve Node runtimes and swapping the machine.
MAX_CONCURRENCY = int(os.environ.get("CLAUDE_BRIDGE_MAX_CONCURRENCY", "3"))

CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

_slots = threading.Semaphore(MAX_CONCURRENCY)


# --- running the CLI --------------------------------------------------------

# The kernel's cap on ONE argv element: MAX_ARG_STRLEN, 32 pages, 128 KiB. It is NOT
# `getconf ARG_MAX` (~2MB) — that is the cap on the argv+envp TOTAL, it is the number
# everyone checks, and checking it tells you nothing about this one.
#
# THIS LIMIT COST US THE CURRICULUM INTERVIEW. The first version of this file passed
# the prompt as `claude -p "<prompt>"`. Chat turns are ~16KB and it worked for a day.
# Then the interview's source-selection step — the one whose own UI promises "Τις
# διαβάζω ΟΛΟΚΛΗΡΕΣ", *I read them WHOLE* — put the tutor's real 359,011-character
# library into the prompt and `execve` refused it: OSError(E2BIG), before a single
# token was billed. `full_context_budget` is 600K TOKENS, i.e. megabytes of text, so
# this was never a corner case; it was the flagship feature.
#
# Nothing unbounded may ever be an argument again. Prompt -> stdin. System -> a file.
MAX_ARG_STRLEN = 32 * 4096


def build_invocation(req: dict) -> tuple[list[str], str, "Callable[[], None]"]:
    """-> (argv, stdin_text, cleanup). Every flag here is load-bearing.

    THE PROMPT GOES ON STDIN. `claude -p` with no positional argument reads the
    prompt from stdin (probed live), and a pipe has no size limit. This is the fix
    for the E2BIG above and it is not optional: a positional prompt reintroduces a
    128KB ceiling on the tutor's library.

    THE SYSTEM PROMPT GOES IN A FILE. `--system-prompt` is argv and carries the exact
    same ceiling — and the corpus can land in the system prompt too, depending on how
    a caller shapes its messages. `--system-prompt-file` takes a path instead. It is
    absent from `claude --help`'s option list (it appears only inside the `--bare`
    blurb) but it is real, and it was probed against this CLI before being relied on.

    `--safe-mode` AND NOT `--bare`. They look interchangeable — both strip CLAUDE.md,
    hooks, plugins, MCP servers, skills — and choosing the wrong one breaks the entire
    premise of this file. Read `--bare`'s own help text: "Anthropic auth is strictly
    ANTHROPIC_API_KEY or apiKeyHelper ... OAuth and keychain are NEVER read". `--bare`
    therefore disables the subscription auth that is the only reason the bridge
    exists, and fails asking for an API key. `--safe-mode` strips the same
    customisations and says "Auth ... work[s] normally".

    `--tools ""` disables every built-in tool. Without it this is a coding agent that
    can read and write files, driven by whatever text a user typed into a chat box.
    With it, it is a plain LLM.

    `--json-schema` is real server-side structured-output validation, not a prompt
    asking nicely for JSON — the reply comes back in `structured_output` already
    parsed. Note it makes `stop_reason` `"tool_use"` rather than `"end_turn"`
    (structured output is implemented as an internal tool), so do NOT treat a
    non-`end_turn` stop as a failure.

    `--no-session-persistence` keeps a server from writing a session transcript to
    disk for every chat message the tutor sends.
    """
    argv = [
        CLAUDE_BIN, "-p",                     # BARE `-p`. No positional prompt. See above.
        "--model", req.get("model") or "sonnet",
        "--safe-mode",
        "--tools", "",
        "--no-session-persistence",
        "--output-format", "json",
    ]

    tmp: list[str] = []

    def cleanup() -> None:
        for path in tmp:
            try:
                os.unlink(path)
            except OSError:
                pass

    if req.get("system"):
        fd, path = tempfile.mkstemp(prefix="claude-sys-", suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(req["system"])
        tmp.append(path)
        argv += ["--system-prompt-file", path]

    if req.get("effort"):
        argv += ["--effort", req["effort"]]

    if req.get("json_schema"):
        # Still argv — there is no `--json-schema-file`. No schema this app owns is
        # remotely close to 128KB, but an unchecked one would crash the handler and
        # drop the connection, which is precisely the failure mode that made the
        # E2BIG bug so hard to read. Bound it and say so.
        schema = json.dumps(req["json_schema"])
        if len(schema.encode()) >= MAX_ARG_STRLEN:
            cleanup()
            raise ValueError(
                f"json_schema is too large to pass as an argument "
                f"({len(schema.encode())} bytes, limit {MAX_ARG_STRLEN})"
            )
        argv += ["--json-schema", schema]

    return argv, req["prompt"], cleanup


def _classify(exit_code: int, stderr: str, payload: dict | None) -> str | None:
    """CLI failure -> the app's `LLMError.kind` taxonomy, or None if it worked.

    The kinds are not decoration: `app/jobs/runner.py` branches on them, and a
    `rate_limit` puts a half-drafted lesson back to `queued` while an `upstream`
    marks it `failed`. Misclassify a subscription cap as `upstream` and the
    tutor loses half a curriculum to a limit that would have cleared on its own.
    """
    blob = f"{stderr}\n{json.dumps(payload or {})}".lower()

    if exit_code != 0 or (payload or {}).get("is_error"):
        # The 5-hour / weekly subscription cap. This is THE failure mode that
        # separates a subscription from an API key, and the one the tutor will
        # actually hit. It is temporary by definition — never `failed`.
        if any(s in blob for s in ("rate limit", "rate_limit", "429",
                                   "usage limit", "quota")):
            return "rate_limit"
        if any(s in blob for s in ("authentication", "unauthorized", "401",
                                   "not logged in", "please run /login",
                                   "invalid api key", "oauth")):
            return "auth"
        if any(s in blob for s in ("overloaded", "529", "500", "502", "503")):
            return "upstream"
        return "upstream"
    return None


def run_claude(req: dict) -> dict:
    timeout = float(req.get("timeout_s") or 600)

    try:
        argv, stdin_text, cleanup = build_invocation(req)
    except ValueError as e:
        return {"ok": False, "kind": "upstream", "message": str(e)}

    with _slots:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,      # stdout and stderr as SEPARATE pipes.
                text=True,
                # NEVER stderr=STDOUT. The CLI writes warnings to stderr ("no
                # stdin data received in 3s", node deprecations); merged into
                # stdout they land in front of the JSON and every parse dies with
                # "Expecting value: line 1 column 1". It works right up until the
                # first day Node prints a warning, which is the worst kind of bug.
                input=stdin_text,
                # THE PROMPT, ON STDIN. Not argv — a single argv element is capped at
                # 128KB by the kernel and the tutor's library is 359KB. See
                # `build_invocation`. `input=` also closes the pipe when it is done,
                # so the CLI never sits waiting on stdin the way it does with an
                # inherited descriptor.
                timeout=timeout,
                env={**os.environ, "CI": "1"},
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "kind": "timeout",
                    "message": f"claude -p exceeded {timeout:.0f}s"}
        except OSError as e:
            # ANY failure to even START the process — E2BIG (argument too long),
            # ENOENT (not on PATH), EACCES, ENOMEM. It MUST become a response.
            #
            # The narrow `except FileNotFoundError` this replaces is what turned the
            # E2BIG bug into a whodunnit: the OSError escaped, killed the handler
            # thread, and the socket closed with no reply. The client reported
            # "Server disconnected without sending a response", the app said "could
            # not reach claude-bridge — is it running?", and the bridge was up the
            # whole time, healthy, logging a traceback nobody thought to read.
            # A bridge that cannot run the CLI must still ANSWER.
            log.exception("claude -p could not be started")
            return {"ok": False, "kind": "upstream",
                    "message": f"could not start `{CLAUDE_BIN}`: {e}"}
        finally:
            cleanup()
        elapsed_ms = int((time.monotonic() - started) * 1000)

    payload: dict | None = None
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = None

    kind = _classify(proc.returncode, proc.stderr, payload)
    if kind:
        detail = (proc.stderr or "").strip() or (payload or {}).get("result") or ""
        log.warning("claude -p failed (%s): exit=%s %s", kind, proc.returncode, detail[:300])
        return {"ok": False, "kind": kind, "message": detail[:500] or f"claude -p exit {proc.returncode}"}

    if payload is None:
        return {"ok": False, "kind": "upstream",
                "message": f"claude -p produced no JSON on stdout (exit {proc.returncode})"}

    return {
        "ok": True,
        "text": payload.get("result") or "",
        # Present only when json_schema was sent. Already a dict — the CLI parses
        # and validates it against the schema server-side, so the client does not
        # re-parse `text` and does not need a repair loop.
        "structured": payload.get("structured_output"),
        "stop_reason": payload.get("stop_reason"),
        "session_id": payload.get("session_id"),
        "usage": payload.get("usage") or {},
        # What the same call WOULD have cost on an API key. On a subscription it
        # is not billed — it is the number that tells the tutor whether buying a
        # key is worth it. Reported, never enforced.
        "cost_usd": payload.get("total_cost_usd"),
        "duration_ms": elapsed_ms,
    }


# --- credentials (health only; never returned) -------------------------------

def credential_status() -> dict:
    """Is there a usable subscription behind this bridge — WITHOUT spending a call.

    `health()` is polled by `/health/ready`. A probe that did a real `claude -p`
    would spend the tutor's rate limit on the question "are we up", which on a
    5-hour rolling cap is a genuinely bad trade. So this reads the token's own
    expiry and reports it. It cannot prove the token is *accepted* — only that
    one exists and has not expired. That is the honest limit of a free probe, and
    it catches the failure that actually happens (logged out / expired).

    Returns NO token material. Not the token, not a prefix, not a length.
    """
    try:
        raw = json.loads(CREDENTIALS.read_text())
    except FileNotFoundError:
        return {"present": False, "reason": "no ~/.claude/.credentials.json — run `claude` and log in"}
    except Exception as e:
        return {"present": False, "reason": f"unreadable credentials: {e}"}

    oauth = raw.get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        return {"present": False, "reason": "no Claude OAuth token — run `claude` and log in"}

    expires_at = oauth.get("expiresAt")  # epoch ms
    expired = bool(expires_at and expires_at / 1000 < time.time())
    return {
        "present": True,
        "expired": expired,
        "subscription": oauth.get("subscriptionType"),
        "rate_limit_tier": oauth.get("rateLimitTier"),
        "expires_at": expires_at,
    }


# --- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-bridge"

    def log_message(self, fmt, *args):   # noqa: A002 - stdlib signature
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self) -> bool:
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_GET(self) -> None:
        if self.path != "/health":
            return self._send(404, {"error": "not found"})
        # Health is UNAUTHENTICATED on purpose: it spends nothing, returns no
        # token material, and a container that cannot get past the door has no
        # way to tell "bridge is down" from "my token is wrong" — which is the
        # exact confusion this endpoint exists to resolve.
        self._send(200, {
            "ok": True,
            "cli": shutil.which(CLAUDE_BIN) or None,
            "credentials": credential_status(),
            "max_concurrency": MAX_CONCURRENCY,
        })

    def do_POST(self) -> None:
        if self.path != "/v1/complete":
            return self._send(404, {"error": "not found"})
        if not self._authed():
            return self._send(401, {"ok": False, "kind": "auth",
                                    "message": "bad or missing bridge token"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"ok": False, "kind": "upstream", "message": f"bad request: {e}"})

        if not (req.get("prompt") or "").strip():
            return self._send(400, {"ok": False, "kind": "upstream", "message": "prompt is required"})

        # BELT TO run_claude's BRACES. `run_claude` maps every failure it can foresee,
        # but an unforeseen exception here would escape into socketserver, kill the
        # handler thread, and close the socket with NO RESPONSE — which the client can
        # only report as "the bridge is unreachable", pointing the next debugger at the
        # network while the real traceback sits in a log nobody is reading. That is
        # exactly how the E2BIG bug hid. Whatever happens, this endpoint ANSWERS.
        try:
            out = run_claude(req)
        except Exception as e:  # noqa: BLE001 - deliberately total
            log.exception("unhandled error in run_claude")
            out = {"ok": False, "kind": "upstream", "message": f"bridge error: {e!r}"}

        # ONE LINE PER CALL, WITH THE DURATION AND THE SHAPE OF THE INPUT. Without
        # this, "the app feels slow" is unattributable: a chat turn is several
        # `claude -p` calls (the ReAct loop makes at least two), each forks a Node
        # runtime, and the prompt grows with every tool result fed back. Logging
        # only "200 OK" tells you none of that, and you end up guessing which call
        # was the expensive one. `in=` is characters, not tokens — a cheap proxy
        # that is enough to spot a prompt that has quietly ballooned.
        if out.get("ok"):
            log.info(
                "claude -p model=%s effort=%s schema=%s in=%dch -> %dms out=%dch cost≈$%s",
                req.get("model"), req.get("effort"), bool(req.get("json_schema")),
                len(req.get("prompt") or "") + len(req.get("system") or ""),
                out.get("duration_ms") or 0, len(out.get("text") or ""),
                out.get("cost_usd"),
            )

        # 200 even when the model failed. The HTTP transaction SUCCEEDED; the
        # failure is a fact about the model, carried in `kind` so the provider can
        # map it to LLMError without having to reverse-engineer a status code.
        # Only a bad bridge token is a 401 — that is a fact about the transport.
        self._send(200, out)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not TOKEN:
        raise SystemExit(
            "CLAUDE_BRIDGE_TOKEN is not set.\n\n"
            "This bridge spends your Claude subscription. It binds a TCP port, and an\n"
            "unauthenticated port that spends money is not a thing to ship, even on a\n"
            "home LAN. Generate one and put the SAME value in the app's .env:\n\n"
            "    export CLAUDE_BRIDGE_TOKEN=$(openssl rand -hex 32)\n"
        )
    if not shutil.which(CLAUDE_BIN):
        raise SystemExit(f"`{CLAUDE_BIN}` not found on PATH. Install Claude Code, or set CLAUDE_BIN.")

    cred = credential_status()
    if not cred["present"]:
        log.warning("NO USABLE CREDENTIAL: %s", cred["reason"])
    else:
        log.info("subscription=%s tier=%s expired=%s",
                 cred["subscription"], cred["rate_limit_tier"], cred["expired"])

    log.info("claude-bridge on http://%s:%d (max_concurrency=%d)", HOST, PORT, MAX_CONCURRENCY)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
