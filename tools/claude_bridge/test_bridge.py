"""Regression tests for `bridge.py` — the kernel limit that broke the interview.

    ./apps/api/.venv/bin/python -m pytest tools/claude_bridge/ -q

THE BUG THESE EXIST TO PREVENT. The first version passed the prompt as a COMMAND-LINE
ARGUMENT (`claude -p "<the whole prompt>"`). Linux caps a single argv element at
MAX_ARG_STRLEN = 32 pages = 131072 bytes. That is a DIFFERENT, far smaller limit than
the ~2MB `getconf ARG_MAX` that everyone quotes and that most people check — which is
exactly why it looked fine.

Chat turns are ~16KB and sailed through it for a day. Then the curriculum interview's
source-selection step — the one whose own UI says "Τις διαβάζω ΟΛΟΚΛΗΡΕΣ", *I read them
WHOLE* — put the tutor's real 359,011-character library into the prompt, and the CLI
died with `OSError: [Errno 7] Argument list too long` before a single token was billed.

So the test is not "does a big prompt work" (that costs a live call and a rate limit).
It is: NO ARGV ELEMENT MAY EXCEED THE KERNEL'S PER-ARGUMENT CAP, EVER. That is the
actual invariant, it is checkable for free, and it is the one that was violated.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import bridge  # noqa: E402

# 32 pages. The cap on ONE argv element — not on their total.
MAX_ARG_STRLEN = 32 * 4096

# The tutor's real library, measured 2026-07-14: 13 sources, 359,011 chars. The
# interview reads them WHOLE. Test above it, not at it.
REAL_LIBRARY_CHARS = 359_011


def _big(n: int) -> str:
    return "σελίδα κειμένου. " * (n // 17 + 1)


def test_no_argv_element_can_exceed_the_kernel_per_argument_cap(tmp_path):
    """THE regression test. Every element, not the total."""
    req = {
        "prompt": _big(REAL_LIBRARY_CHARS * 2),      # 700KB+ — well past any real library
        "system": _big(200_000),
        "model": "sonnet",
        "effort": "high",
        "json_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
    }
    argv, stdin, cleanup = bridge.build_invocation(req)
    try:
        for i, arg in enumerate(argv):
            assert len(arg.encode()) < MAX_ARG_STRLEN, (
                f"argv[{i}] is {len(arg.encode())} bytes, over the {MAX_ARG_STRLEN}-byte "
                f"per-argument kernel cap. This is the exact OSError(E2BIG) that killed "
                f"the curriculum interview. Big text goes on stdin or in a file, never argv."
            )
    finally:
        cleanup()


def test_the_prompt_travels_on_stdin_not_argv(tmp_path):
    """`claude -p` with no positional arg reads the prompt from stdin — verified live.
    That is the only channel with no size limit."""
    req = {"prompt": "USER: γεια", "model": "haiku"}
    argv, stdin, cleanup = bridge.build_invocation(req)
    try:
        assert stdin == "USER: γεια"
        assert "USER: γεια" not in argv        # must NOT also be an argument
        # `-p` must still be present, but bare — a positional prompt would reintroduce
        # the bug for anyone who "helpfully" adds it back.
        assert "-p" in argv
        assert argv[argv.index("-p") + 1].startswith("--")
    finally:
        cleanup()


def test_a_large_system_prompt_goes_to_a_file_not_argv(tmp_path):
    """`--system-prompt` is argv and has the same 128KB ceiling. The corpus can land in
    the system prompt too, so it gets `--system-prompt-file` (undocumented in --help,
    but real — probed live)."""
    system = _big(300_000)
    argv, stdin, cleanup = bridge.build_invocation({"prompt": "hi", "system": system})
    try:
        assert "--system-prompt-file" in argv
        path = Path(argv[argv.index("--system-prompt-file") + 1])
        assert path.read_text() == system      # the content survives intact
        assert "--system-prompt" not in argv   # not BOTH — the CLI would take one
    finally:
        cleanup()
    assert not path.exists(), "the temp system-prompt file must be cleaned up"


def test_an_oversized_json_schema_is_a_clean_error_not_a_crash():
    """`--json-schema` is also argv. No app schema is anywhere near 128KB, but a crash
    here would again drop the connection with no response — so it must be a typed
    failure, not an OSError."""
    out = bridge.run_claude({"prompt": "hi", "json_schema": {"x": _big(200_000)}})
    assert out["ok"] is False
    assert out["kind"] == "upstream"
    assert "too large" in out["message"].lower()


def test_a_subprocess_OSError_becomes_a_response_never_a_dropped_connection(monkeypatch):
    """THE SECOND BUG, and the reason the first one was so hard to read.

    `subprocess.run` raised OSError; `run_claude` caught only FileNotFoundError and
    TimeoutExpired; the handler thread died WITHOUT writing a response. The client saw
    a bare socket close ("Server disconnected without sending a response") and the app
    reported "Could not reach claude-bridge. Is it running?" — while the bridge sat
    there, healthy, logging a traceback nobody was looking at.

    Any OSError must become an `{ok: false}` body. A bridge that cannot run the CLI
    must still ANSWER."""
    def _boom(*a, **kw):
        raise OSError(7, "Argument list too long", "claude")

    monkeypatch.setattr(subprocess, "run", _boom)
    out = bridge.run_claude({"prompt": "hi"})
    assert out["ok"] is False
    assert out["kind"] == "upstream"
    assert "argument list too long" in out["message"].lower()


def test_classify_maps_a_subscription_cap_to_rate_limit():
    """`jobs/runner.py` requeues a `rate_limit` lesson and buries an `upstream` one. On a
    subscription the 5-hour cap is routine — misclassifying it loses half a curriculum."""
    assert bridge._classify(1, "Claude usage limit reached", None) == "rate_limit"
    assert bridge._classify(1, "Not logged in · Please run /login", None) == "auth"
