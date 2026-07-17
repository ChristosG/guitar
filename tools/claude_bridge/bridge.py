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
speaks two small verbs (`/v1/complete`, `/v1/vision`) shaped for
`app/llm/claude_cli.py`, the only client that exists. When the tutor buys an API
key, `LLM_PROVIDER=claude` (the real SDK, `app/llm/claude.py`) takes over and this
whole directory becomes dead weight — which is the intended end state, not a regret.

THE TWO VERBS ARE TWO ON PURPOSE, and it is the security boundary in this file.
`/v1/complete` runs with `--tools ""` — no tool, no file access, a plain LLM
driven by whatever a user typed into a chat box. `/v1/vision` runs with
`--tools Read`, because `claude -p` has no image parameter and a real file is the
only way a page scan can reach the model. That hole is opened once, deliberately,
and fenced: one named tool, `--permission-mode manual` (the fence that actually
holds — the cwd and `--add-dir` do NOT, and that was probed, not assumed), a deny
rule over the credential, a read-only mount of the page scans alone, and a path
proven inside the media root before the fork. Merging the two would hand the chat
box a file reader. See `_vision_argv`.

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

# The ONLY directory `/v1/vision` may read, and the ONLY hole in `--tools ""`.
# Bind-mounted READ-ONLY from the same volume the api writes page scans to (see
# docker-compose.yml). It is deployment configuration and NEVER a request field:
# if a POST body could carry its own media root, every check below would be
# theatre — `{"media_root": "/", "image_path": "home/node/.claude/.credentials.json"}`.
MEDIA_ROOT = os.environ.get("CLAUDE_BRIDGE_MEDIA_ROOT", "/media")

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

    # EVERY failure below unlinks what it already wrote. `cleanup` is the caller's
    # handle on the temp file, and a raise means the caller never receives it — so
    # the only chance to clean up is here, on the way out. Registering the path
    # before the write and unlinking on any raise are the two halves of that: the
    # file exists from `mkstemp`, not from the write that may never finish.
    try:
        if req.get("system"):
            fd, path = tempfile.mkstemp(prefix="claude-sys-", suffix=".txt")
            tmp.append(path)
            with os.fdopen(fd, "w") as fh:
                fh.write(req["system"])
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
                raise ValueError(
                    f"json_schema is too large to pass as an argument "
                    f"({len(schema.encode())} bytes, limit {MAX_ARG_STRLEN})"
                )
            argv += ["--json-schema", schema]

        return argv, req["prompt"], cleanup
    except Exception:
        cleanup()
        raise


def _classify(exit_code: int, stderr: str, payload: dict | None) -> str | None:
    """CLI failure -> the app's `LLMError.kind` taxonomy, or None if it worked.

    The kinds are not decoration: `app/jobs/runner.py` branches on them, and a
    `rate_limit` puts a half-drafted lesson back to `queued` while an `upstream`
    marks it `failed`. Misclassify a subscription cap as `upstream` and the
    tutor loses half a curriculum to a limit that would have cleared on its own.

    ONLY THE FIELDS THAT CARRY AN ERROR MESSAGE, never the whole payload. This
    used to grep `json.dumps(payload)`, and a CLI error payload is mostly NUMBERS:
    `duration_ms`, `duration_api_ms`, a `session_id` uuid, ~8 `usage` counters.
    `"429" in "4291"` is True, so a 4.291-SECOND upstream failure classified as
    `rate_limit` and got requeued forever; a session uuid containing "401" became
    `auth`. That needed no attacker and no strange input — just an unlucky
    duration, which is every error eventually. The substring search is blunt
    enough to be worth it on prose; on numbers it is a coin flip.
    """
    payload = payload or {}
    parts = [stderr or "", str(payload.get("result") or ""), str(payload.get("error") or "")]
    # The CLI's own structured status — exact, and the reason we can afford to
    # stop reading the rest of the payload.
    if payload.get("api_error_status") is not None:
        parts.append(str(payload["api_error_status"]))
    blob = "\n".join(parts).lower()

    if exit_code != 0 or payload.get("is_error"):
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


def _invoke(
    argv: list[str],
    stdin_text: str,
    timeout: float,
    *,
    cwd: str | None = None,
    cleanup: "Callable[[], None] | None" = None,
) -> dict:
    """Run the CLI once and normalise whatever happened into the wire shape.

    Shared by `/v1/complete` and `/v1/vision` so that the semaphore, the timeout,
    the stderr discipline, the OSError rule and the `_classify` taxonomy cannot
    drift apart between the two endpoints. The endpoints differ ONLY in the argv
    they compose and the cwd they run in — which is exactly the surface that
    should differ, and nothing else.
    """
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
                # WHAT COUNTS AS THE WORKSPACE — the paths `--permission-mode manual`
                # pre-grants. This container declares no WORKDIR, so an inherited cwd
                # is `/`: irrelevant under `--tools ""` (no tool can look at
                # anything), and under `--tools Read` it would pre-grant the entire
                # filesystem. `/v1/vision` pins it; see `run_vision`.
                cwd=cwd,
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
            if cleanup:
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


def run_claude(req: dict) -> dict:
    try:
        # THE TIMEOUT IS PARSED FIRST, BEFORE `build_invocation` WRITES ANYTHING.
        # Not stylistic: `build_invocation` creates the system-prompt temp file, and
        # `cleanup` only comes back with it. A `float()` that raises after that
        # point leaks the file — holding the system prompt — with nothing left
        # holding its name. `timeout_s: "abc"` did exactly that, once per request.
        timeout = float(req.get("timeout_s") or 600)
        argv, stdin_text, cleanup = build_invocation(req)
    except (ValueError, TypeError) as e:
        return {"ok": False, "kind": "upstream", "message": str(e)}
    # NO cwd. `--tools ""` means no tool can look at a file, so the working
    # directory is not reachable and pinning it would imply a protection that is
    # already total. `/v1/vision` is the one that needs it.
    return _invoke(argv, stdin_text, timeout, cleanup=cleanup)


# --- vision: the one hole in `--tools ""`, opened as narrowly as it can be ----
#
# `/v1/complete` disables every built-in tool, deliberately: "Without it this is a
# coding agent." Vision cannot work that way. `claude -p` has NO image parameter —
# the only route from a page scan to the model is the `Read` tool plus a real file
# on disk. So this IS a real hole in the posture, and everything below is about
# making it the narrowest one available:
#
#   - a SEPARATE endpoint. `/v1/complete` keeps `--tools ""`, untouched.
#   - `--tools Read`. One named tool, never a blanket re-enable.
#   - `--permission-mode manual`. THE fence: everything outside the workspace needs
#     a grant, and `-p` has nobody to grant it. Neither the cwd nor `--add-dir`
#     restricts anything — see `_vision_argv`, which has the probe that proves it.
#   - a `--settings` deny rule over ~ and /proc, as an independent second fence.
#   - a READ-ONLY bind mount of the media directory ALONE — not the repo, not the DB.
#   - the path proven inside the media root BEFORE the subprocess is forked.
#
# The stakes: this container holds the tutor's REAL `~/.claude` — his Claude OAuth
# session AND live refresh tokens for every MCP server he has ever authorised. A
# path traversal that reaches the Read tool is an arbitrary file read with all of
# that attached.


def _require_object(parsed: object) -> dict:
    """The parsed body must be a JSON OBJECT. `[]` is valid JSON and `[].get` is not.

    Both handlers reached for `req.get("prompt")` the moment the body parsed, which
    is one `-d '[]'` away from an AttributeError in a handler thread — i.e. a dead
    thread and a socket closed with NO response. That is the same silent shape the
    E2BIG bug wore for a day: the client can only say "the bridge is unreachable",
    so the search starts at the network while the traceback sits unread in a log.
    """
    if not isinstance(parsed, dict):
        raise ValueError(f"body must be a JSON object, got {type(parsed).__name__}")
    return parsed


def _prompt_of(req: dict) -> str | None:
    """The prompt, or None if there isn't a usable one.

    `(req.get("prompt") or "").strip()` looks total and is not: `{"prompt": 5}`
    parses, IS an object, and `(5 or "").strip()` is an AttributeError — the same
    dropped connection by a longer road. A non-string prompt is a 400.
    """
    prompt = req.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return prompt


class MediaPathError(ValueError):
    """The requested path is not a readable image inside the media root.

    A 400, never a subprocess. Raised BEFORE `claude` is forked, so a traversal
    attempt costs an HTTP round trip and nothing else.
    """


# THE SECOND FENCE. `--permission-mode manual` (see `_vision_argv`) is the primary
# one and it is allow-list shaped; this names the crown jewels explicitly and says
# no, so that the credential does not depend on one flag's semantics surviving a
# CLI upgrade. `deny` beats `allow` in Claude Code's permission model.
#
# The `//` is not a typo: a leading `//` is how a permission rule spells an
# ABSOLUTE path (`Read(//etc/**)`); a single slash would be read as relative to
# the workspace and match nothing here.
#
# Built from `Path.home()`, not hardcoded, because HOME is what decides where the
# CLI actually keeps `.credentials.json` — a rule that did not track it would go
# quietly stale the day the Dockerfile changed.
_VISION_DENY = [
    # ~/.claude/.credentials.json: the Claude OAuth session AND live access +
    # refresh tokens for every MCP server the tutor has ever authorised.
    f"Read(/{Path.home()}/**)",
    # /proc/self/environ carries CLAUDE_BRIDGE_TOKEN. Reading it would land the
    # bridge's own shared secret in a page transcript, and from there in the DB.
    "Read(//proc/**)",
]

_VISION_SETTINGS = json.dumps({"permissions": {"deny": _VISION_DENY}})


def _resolve_media_path(rel: str, *, media_root: str | None = None) -> str:
    """`rel` -> an absolute path PROVEN to sit inside `media_root`.

    `commonpath` on both realpaths, and NOT `startswith`: `"/media-evil/x"
    .startswith("/media")` is True, and that one-character mistake is the whole
    vulnerability. `realpath` first, so a symlink planted inside the media root is
    resolved to its target and judged on where it actually lands.

    `media_root` defaults to the module global at CALL time, not at def time —
    a default of `MEDIA_ROOT` in the signature would bind the value at import and
    quietly ignore any later override, which is the sort of thing that makes a
    security test pass while testing nothing.

    Absolute input is refused outright rather than reinterpreted. The contract says
    `image_path` is relative to the media root; silently rebasing `/etc/passwd` to
    `/media/etc/passwd` would "work", and would hand the one caller that ever sends
    an absolute path a confusing miss instead of an error.
    """
    if not rel or not rel.strip():
        raise MediaPathError("image_path is required")
    if "\x00" in rel:
        # A NUL truncates a path at the C boundary: "a.jpg\x00.png" satisfies a
        # suffix check as a .png and opens a.jpg. Python's own open() rejects it,
        # but this string is bound for another program's argv — refuse it here.
        raise MediaPathError("NUL byte in path")
    if os.path.isabs(rel):
        raise MediaPathError(f"image_path must be relative to the media root: {rel!r}")

    root = os.path.realpath(media_root or MEDIA_ROOT)
    target = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, target]) != root:
        raise MediaPathError(f"path escapes the media root: {rel!r}")
    return target


def _vision_argv(abs_image_path: str, *, model: str = "sonnet") -> list[str]:
    """`claude -p` with EXACTLY ONE tool enabled.

    `--tools Read` rather than `--tools ""`: `claude -p` takes no image parameter,
    so reading a file is the only way a page scan reaches the model. This is why it
    is a separate endpoint — `/v1/complete` keeps every tool off, and this one may
    open a single, named door.

    `--safe-mode` AND NOT `--bare`, for the reason `build_invocation` spells out and
    which is worth repeating because `--bare` reads like the safer word: "Anthropic
    auth is strictly ANTHROPIC_API_KEY or apiKeyHelper ... OAuth and keychain are
    NEVER read". The subscription IS an OAuth token. `--bare` would fail every call
    asking for an API key the tutor does not have.

    NO POSITIONAL PROMPT — it goes on stdin (see `_vision_stdin`). Both `--tools
    <tools...>` and `--add-dir <directories...>` are VARIADIC: they consume every
    following non-option argument. Probed live against the pinned CLI:

        $ claude -p --tools Read --add-dir /some/dir "say hi"
        Error: Input must be provided either through stdin or as a prompt argument

    The prompt was eaten as a second directory and the CLI then blocked on stdin.
    Keeping argv all-flags makes that unrepresentable, and it keeps this file's
    other rule — nothing unbounded in argv, ever — true for the OCR prompt too.

    `--permission-mode manual` IS THE FENCE, and nothing else here is.

    This was probed, not assumed, and the assumption was wrong. With `--tools Read`,
    the cwd set inside the media mount and `--add-dir` set, the CLI was asked for a
    file outside both:

        prompt: "Use your Read tool on /etc/hostname and tell me the exact string"
        reply:  "The exact string contained in /etc/hostname is: 7c3faf5bdcb3"

    It read it. Neither the cwd nor `--add-dir` restricts anything — `--add-dir`
    only ADDS, and Claude Code auto-approves Read outside the workspace in `-p`.
    So `--tools Read` on its own is an arbitrary file read, in the one container
    that holds the tutor's real `~/.claude`.

    (An earlier probe that asked for `.credentials.json` outright came back clean.
    That proved nothing: it was the model declining, not the sandbox refusing.
    Model goodwill is not a security boundary.)

    `--permission-mode manual` makes every path outside the workspace require a
    grant, and `-p` is non-interactive — there is nobody to grant it:

        "Claude requested permissions to read from /etc/hostname, but you haven't
         granted it yet."

    The page itself still reads, because the workspace (cwd + `--add-dir`) is
    pre-granted. That asymmetry is the design: an allow-list, not a blocklist of
    the paths we happened to think of. `--settings` (`_VISION_DENY`) is the second,
    independent fence for the credential specifically.
    """
    return [
        CLAUDE_BIN, "-p",                     # BARE `-p`. No positional prompt. See above.
        "--model", model,
        "--safe-mode",
        "--tools", "Read",
        # THE FENCE. Not decoration — see the docstring. Without it the two flags
        # below are a suggestion.
        "--permission-mode", "manual",
        "--settings", _VISION_SETTINGS,
        "--add-dir", os.path.dirname(abs_image_path),
        "--no-session-persistence",
        "--output-format", "json",
    ]


def _vision_stdin(abs_image_path: str, prompt: str) -> str:
    """The caller's prompt, plus the one fact it cannot know: where the page is.

    The path is APPENDED so the caller's instruction stays the first thing the
    model reads. Task 9 owns the wording of `prompt`; this owns only the plumbing,
    and deliberately adds no scrubbing, no formatting demand and no persona — if
    `claude -p` narrates its way around a transcription, that is a prompt problem,
    and patching it here would hide it from the only task that can fix it properly.
    """
    return f"{prompt}\n\nThe page image is at: {abs_image_path}"


def run_vision(req: dict) -> dict:
    """`{"image_path": <relative to the media root>, "prompt": ...}` -> the transcript.

    Raises `MediaPathError` (a 400) rather than returning one: a bad path is a bug
    in the caller, not a fact about the model, and it must never be mistakable for
    a transcription.
    """
    image = _resolve_media_path(req.get("image_path") or "")
    if not os.path.isfile(image):
        # Cheap, and it pays for itself immediately: without it a missing page boots
        # Node, spends ~40s and an agentic turn of a 5-hour cap, and comes back with
        # the PROSE "I couldn't find that file" — which the caller would then store
        # as the page's transcript.
        raise MediaPathError(f"no such image in the media root: {req.get('image_path')!r}")

    return _invoke(
        _vision_argv(image, model=req.get("model") or "sonnet"),
        _vision_stdin(image, req.get("prompt") or ""),
        float(req.get("timeout_s") or 600),
        # The WORKSPACE — i.e. the set of paths `--permission-mode manual`
        # pre-grants. This container declares no WORKDIR, so the cwd `claude` would
        # otherwise inherit is `/`, and `/` as the workspace would pre-grant the
        # whole filesystem and make the fence a no-op. Pinning it to the page's own
        # directory is what leaves the fence with something to refuse.
        #
        # NOTE this is not itself a restriction: a cwd does not stop Read from
        # going elsewhere (probed — see `_vision_argv`). It only decides what is
        # allowed WITHOUT a grant. The refusal comes from the permission mode.
        cwd=os.path.dirname(image),
    )


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
        # TWO VERBS, AND THEY STAY TWO. The split is the security boundary itself:
        # `/v1/complete` runs with every built-in tool disabled and `/v1/vision`
        # runs with `Read`. Anyone tempted to "unify" them is proposing to give a
        # chat box a file reader.
        if self.path == "/v1/complete":
            return self._complete()
        if self.path == "/v1/vision":
            return self._vision()
        return self._send(404, {"error": "not found"})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return _require_object(json.loads(self.rfile.read(n) or b"{}"))

    def _complete(self) -> None:
        if not self._authed():
            return self._send(401, {"ok": False, "kind": "auth",
                                    "message": "bad or missing bridge token"})
        try:
            req = self._body()
            prompt = _prompt_of(req)
        except Exception as e:
            return self._send(400, {"ok": False, "kind": "upstream", "message": f"bad request: {e}"})

        if prompt is None:
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

    def _vision(self) -> None:
        """`claude -p --tools Read` over ONE file inside the read-only media mount."""
        if not self._authed():
            return self._send(401, {"ok": False, "kind": "auth",
                                    "message": "bad or missing bridge token"})
        try:
            req = self._body()
            prompt = _prompt_of(req)
        except Exception as e:
            return self._send(400, {"ok": False, "kind": "upstream", "message": f"bad request: {e}"})

        if prompt is None:
            return self._send(400, {"ok": False, "kind": "upstream", "message": "prompt is required"})

        try:
            out = run_vision(req)
        except MediaPathError as e:
            # A 400, and LOUD in the log. This is the line that says someone asked
            # this bridge to read something it must never read; it is the single
            # highest-signal event this process can emit, and on a PoC that runs on
            # the tutor's own machine the log is the only place it can go.
            log.warning("REFUSED vision path %r: %s", req.get("image_path"), e)
            return self._send(400, {"ok": False, "kind": "upstream", "message": str(e)})
        except Exception as e:  # noqa: BLE001 - deliberately total; see `_complete`
            log.exception("unhandled error in run_vision")
            out = {"ok": False, "kind": "upstream", "message": f"bridge error: {e!r}"}

        if out.get("ok"):
            # `image=` is the relative path the caller asked for, not the resolved
            # one — it is what the api's log will also show, so the two can be
            # joined when a page comes back wrong.
            log.info(
                "claude -p VISION model=%s image=%s -> %dms out=%dch cost≈$%s",
                req.get("model"), req.get("image_path"),
                out.get("duration_ms") or 0, len(out.get("text") or ""),
                out.get("cost_usd"),
            )

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
