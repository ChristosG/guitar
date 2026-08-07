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
import tempfile
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


def test_a_bad_timeout_does_not_leak_the_system_prompt_file(monkeypatch):
    """`timeout_s: "abc"` must not leave the tutor's system prompt in /tmp forever.

    A REGRESSION FROM THIS TASK'S OWN REFACTOR, and a good argument for the rule
    it broke. `run_claude` used to parse the timeout on its FIRST line — before
    `build_invocation` had written anything — so a bad one raised harmlessly.
    Folding the parse into the `_invoke(...)` call moved it AFTER the temp file was
    created and BEFORE anything could clean it up: the file survived, holding
    whatever was in the system prompt, once per request."""
    before = set(Path(tempfile.gettempdir()).glob("claude-sys-*"))

    out = bridge.run_claude({"prompt": "hi", "system": "SECRET SYSTEM PROMPT",
                             "timeout_s": "abc"})

    assert out["ok"] is False
    leaked = set(Path(tempfile.gettempdir()).glob("claude-sys-*")) - before
    assert not leaked, f"leaked system-prompt file(s): {leaked}"


def test_a_system_prompt_that_fails_mid_write_is_still_cleaned_up():
    """`tmp.append(path)` must follow `mkstemp`, not the write. Registered after,
    a write that raises leaves a file `cleanup()` has never heard of."""
    before = set(Path(tempfile.gettempdir()).glob("claude-sys-*"))

    out = bridge.run_claude({"prompt": "hi", "system": 12345})   # not a str -> TypeError

    assert out["ok"] is False
    leaked = set(Path(tempfile.gettempdir()).glob("claude-sys-*")) - before
    assert not leaked, f"leaked system-prompt file(s): {leaked}"


def test_classify_reads_error_text_and_never_the_payloads_numbers():
    """THE TAXONOMY WAS A COIN FLIP ON A TIMESTAMP.

    This used to grep `json.dumps(payload)`, and a CLI error payload is mostly
    NUMBERS: `duration_ms`, `duration_api_ms`, a `session_id` uuid, and ~8 `usage`
    counters. `"429" in "4291"` is True — so a 4.291-second upstream failure came
    back `rate_limit` and `jobs/runner.py` requeued it forever, while a session
    uuid containing "401" came back `auth`. It needed no attacker and no unusual
    input, just an unlucky duration, which is every error eventually.

    Classification reads the fields that carry an error MESSAGE, and the CLI's own
    `api_error_status`. Never the whole blob."""
    # A 4.291s failure is not a rate limit.
    assert bridge._classify(1, "boom", {"duration_ms": 4291, "is_error": True}) == "upstream"
    # A session id is not an auth failure.
    assert bridge._classify(
        1, "boom", {"session_id": "9f2c-401e-bd33", "is_error": True}
    ) == "upstream"
    # Usage counters are not status codes.
    assert bridge._classify(
        1, "boom", {"usage": {"input_tokens": 429, "output_tokens": 401}, "is_error": True}
    ) == "upstream"
    # The real signals still land.
    assert bridge._classify(1, "", {"result": "Claude usage limit reached", "is_error": True}) == "rate_limit"
    assert bridge._classify(1, "", {"api_error_status": 429, "is_error": True}) == "rate_limit"
    assert bridge._classify(1, "", {"api_error_status": 401, "is_error": True}) == "auth"
    assert bridge._classify(0, "", {"duration_ms": 4291}) is None       # success stays success


# --- /v1/vision -------------------------------------------------------------
#
# `/v1/complete` runs `claude -p --tools ""` — every built-in tool disabled,
# deliberately: "Without it this is a coding agent."
#
# Vision cannot work that way: `claude -p` has no image parameter, so the only
# route to a page scan is the `Read` tool plus a real file. That is a genuine hole
# in the bridge's posture, so it is opened exactly once, as narrowly as possible:
# a SEPARATE endpoint, `Read` and nothing else, and a path that must resolve
# inside the read-only media root. `/v1/complete` is untouched.


def test_vision_enables_read_and_nothing_else():
    argv = bridge._vision_argv("/media/abc/0001.jpg")
    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == "Read"
    assert '--tools ""' not in " ".join(argv)
    # `--tools <tools...>` is VARIADIC. Exactly one tool may follow it: the next
    # element must be the start of another option, or the CLI silently gains a
    # second tool nobody reviewed.
    assert argv[argv.index("--tools") + 2].startswith("--")


def test_complete_still_disables_every_tool():
    """The hole is opened in `/v1/vision` ONLY. If a future edit "unifies" the two
    invocations, this is the test that fails."""
    argv, _stdin, cleanup = bridge.build_invocation({"prompt": "hi"})
    try:
        assert argv[argv.index("--tools") + 1] == ""
    finally:
        cleanup()


def test_vision_gates_the_read_tool_on_a_permission_mode_that_cannot_say_yes():
    """`--tools Read` ON ITS OWN IS AN ARBITRARY FILE READ. This is the line that
    is actually load-bearing, and it took a live probe to find out — because the
    two things that LOOK like the fence are not:

      - the cwd is not a fence. Claude Code trusts its working directory, but it
        does not stop there.
      - `--add-dir` is not a fence. It ADDS to what Read may touch.

    Probed against the pinned CLI (2.1.209), cwd inside the media mount, --add-dir
    set, asked to read /etc/hostname — a file outside both:

        "The exact string contained in /etc/hostname is: 7c3faf5bdcb3"

    It read it. An earlier probe asking for `.credentials.json` outright came back
    clean, which proved nothing: that was the model declining, not the sandbox
    refusing. Model goodwill is not a security boundary.

    With `--permission-mode manual` the same probe returns:

        "Claude requested permissions to read from /etc/hostname, but you haven't
         granted it yet."

    `-p` is non-interactive, so there is nobody to grant it — the gate is a wall.
    The page itself still reads fine, because the workspace (cwd + --add-dir) is
    pre-granted. That asymmetry is the whole design: allow-list shaped, not a
    blocklist of things we thought to name."""
    argv = bridge._vision_argv("/media/abc/0001.jpg")
    assert argv[argv.index("--permission-mode") + 1] == "manual"


def test_vision_also_denies_the_credential_directory_outright():
    """The SECOND, independent fence, and the reason for a belt as well as braces:
    `--permission-mode manual` is a fact about how this CLI version treats an
    ungranted path, and the asset behind it is the tutor's OAuth session plus live
    refresh tokens for every MCP server he has ever authorised. If a future version
    reinterprets "manual", this rule still names the crown jewels and says no.

    Derived from `Path.home()` rather than hardcoded: HOME is what decides where
    the CLI actually keeps the credential, so a rule that did not track it would
    silently stop matching the day the Dockerfile changed."""
    argv = bridge._vision_argv("/media/abc/0001.jpg")
    rules = json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
    joined = " ".join(rules)
    assert str(Path.home()) in joined, "the ~/.claude credential dir is not denied"
    assert "/proc" in joined, "/proc/self/environ carries CLAUDE_BRIDGE_TOKEN"
    assert all(r.startswith("Read(") for r in rules)


def test_vision_uses_safe_mode_and_never_bare():
    """`--bare` LOOKS like the tighter choice and would break the entire premise.

    Read its help text: "Anthropic auth is strictly ANTHROPIC_API_KEY or
    apiKeyHelper ... OAuth and keychain are NEVER read". The subscription IS an
    OAuth token, so `--bare` fails asking for an API key the tutor does not have.
    `--safe-mode` strips the same customisations and leaves auth working. This is
    the same trap `build_invocation` documents at length; vision must not fall into
    it independently."""
    argv = bridge._vision_argv("/media/abc/0001.jpg")
    assert "--bare" not in argv
    assert "--safe-mode" in argv


@pytest.mark.parametrize("evil", [
    "../../etc/passwd",
    "/etc/passwd",
    "abc/../../../root/.ssh/id_rsa",
    "abc/0001.jpg\x00.png",
])
def test_traversal_is_rejected_before_claude_runs(evil):
    """A 400, not a `claude` invocation. The subprocess must never see it."""
    with pytest.raises(bridge.MediaPathError):
        bridge._resolve_media_path(evil, media_root="/media")


def test_legitimate_path_resolves():
    assert bridge._resolve_media_path("abc/0001.jpg", media_root="/media") == "/media/abc/0001.jpg"


def test_a_sibling_directory_cannot_masquerade_as_the_media_root():
    """WHY `commonpath` AND NOT `startswith`. `"/media-evil/x".startswith("/media")`
    is True, and that one-character mistake is an arbitrary-file-read with the
    tutor's OAuth session attached."""
    with pytest.raises(bridge.MediaPathError):
        bridge._resolve_media_path("../media-evil/x.jpg", media_root="/media")


class _FakeProc:
    returncode = 0
    stdout = json.dumps({"result": "the ¼-inch jack", "total_cost_usd": 0.02})
    stderr = ""


def _capture(monkeypatch) -> dict:
    """Run `run_vision` without spending a real call. Records the exec."""
    seen: dict = {}

    def _run(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _run)
    return seen


def _staged(tmp_path) -> str:
    (tmp_path / "0001.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    return "0001.jpg"


def test_the_vision_prompt_travels_on_stdin_not_argv(monkeypatch, tmp_path):
    """THE VARIADIC TRAP — probed live against the pinned CLI (2.1.209):

        $ claude -p --tools Read --add-dir /some/dir "say hi"
        Error: Input must be provided either through stdin or as a prompt argument

    `--add-dir <directories...>` is VARIADIC. It swallows every following
    non-option argument, so a trailing positional prompt silently becomes a
    DIRECTORY NAME and the prompt is simply gone. The CLI then blocks on stdin.

    Putting the prompt on stdin sidesteps the trap AND keeps the rule the rest of
    this file was rewritten to obey: nothing unbounded is ever an argv element.
    Task 9's OCR prompt merges the publisher's existing text layer into the
    request — that is caller-supplied and it grows."""
    monkeypatch.setattr(bridge, "MEDIA_ROOT", str(tmp_path))
    seen = _capture(monkeypatch)

    out = bridge.run_vision({"image_path": _staged(tmp_path), "prompt": "transcribe it"})

    assert out["ok"] is True
    assert "transcribe it" not in " ".join(seen["argv"])
    assert "transcribe it" in seen["kw"]["input"]
    # The model is told where the page is — it has no image parameter, only Read.
    assert str(tmp_path / "0001.jpg") in seen["kw"]["input"]
    # No positional prompt at all: every argv element after `-p` is a flag or a
    # flag's value, so no variadic option can eat anything.
    assert seen["argv"][seen["argv"].index("-p") + 1].startswith("--")


def test_vision_runs_with_its_cwd_inside_the_media_root(monkeypatch, tmp_path):
    """The cwd decides what counts as the WORKSPACE — the paths that need no grant.

    It is NOT itself the fence (that is `--permission-mode manual`, above): a cwd
    does not stop the Read tool from going elsewhere, which was probed the hard
    way. But it decides what the fence lets through WITHOUT asking, and this
    container declares no `WORKDIR` (see the Dockerfile), so the cwd `claude`
    inherits is `/`. A workspace of `/` pre-grants the entire filesystem and turns
    the fence into a no-op — `/home/node/.claude/.credentials.json` included.

    So: pinned to the page's own directory inside the read-only mount, which is
    what leaves the fence with something to refuse."""
    monkeypatch.setattr(bridge, "MEDIA_ROOT", str(tmp_path))
    seen = _capture(monkeypatch)

    bridge.run_vision({"image_path": _staged(tmp_path), "prompt": "x"})

    assert seen["kw"]["cwd"] == str(tmp_path)
    assert seen["kw"]["cwd"] != "/"


def test_the_media_root_is_never_client_supplied(monkeypatch, tmp_path):
    """The sandbox is a fact about the deployment, not a request field. If a body
    could carry `media_root`, the path check would be theatre:
    `{"media_root": "/", "image_path": "home/node/.claude/.credentials.json"}`."""
    monkeypatch.setattr(bridge, "MEDIA_ROOT", str(tmp_path))
    seen = _capture(monkeypatch)

    bridge.run_vision({"image_path": _staged(tmp_path), "prompt": "x", "media_root": "/"})

    assert seen["kw"]["cwd"] == str(tmp_path)


def test_run_vision_rejects_a_traversal_without_starting_the_subprocess(monkeypatch, tmp_path):
    """The whole point of validating BEFORE the fork."""
    monkeypatch.setattr(bridge, "MEDIA_ROOT", str(tmp_path))
    seen = _capture(monkeypatch)

    with pytest.raises(bridge.MediaPathError):
        bridge.run_vision({"image_path": "../../etc/passwd", "prompt": "x"})

    assert "argv" not in seen, "claude was invoked on a traversal attempt"


def test_a_missing_image_is_a_clean_error_not_a_wasted_subscription_call(monkeypatch, tmp_path):
    """A page that is not there must cost nothing. Without this check `claude`
    boots Node, burns ~40s and an agentic turn, and answers "I couldn't find the
    file" as PROSE — which the caller would then store as a page transcript."""
    monkeypatch.setattr(bridge, "MEDIA_ROOT", str(tmp_path))
    seen = _capture(monkeypatch)

    with pytest.raises(bridge.MediaPathError):
        bridge.run_vision({"image_path": "nope.jpg", "prompt": "x"})

    assert "argv" not in seen


@pytest.mark.parametrize("body", ['[]', '"hello"', '5', 'null', '{"prompt": 5}'])
def test_a_body_that_is_not_an_object_is_a_400_not_a_dropped_connection(body):
    """THE ORIGINAL SIN OF THIS FILE, still reachable through the front door.

    Both handlers did `req.get("prompt")` OUTSIDE the try that parsed the body. A
    JSON array parses fine and then `[].get` is an AttributeError — which escapes
    into socketserver, kills the handler thread, and closes the socket with NO
    RESPONSE. Live, before the fix:

        -d '[]'   -> curl: (52) Empty reply from server

    That is byte-for-byte the symptom the E2BIG bug wore for a day: the client can
    only report "the bridge is unreachable", so the next debugger goes looking at
    the network while the traceback sits in a log nobody is reading. A bridge that
    cannot understand a request must still ANSWER."""
    parsed = json.loads(body)

    # The parse+shape guard the handlers rely on, exercised directly: the handler
    # itself needs a live socket, but this is the check that was missing.
    if not isinstance(parsed, dict):
        with pytest.raises(ValueError):
            bridge._require_object(parsed)
    else:
        assert bridge._require_object(parsed) == parsed
        # `{"prompt": 5}` — parses, IS an object, and `(5 or "").strip()` still
        # explodes. A non-string prompt is a 400, not an AttributeError.
        assert bridge._prompt_of(parsed) is None


def test_a_real_prompt_survives_the_shape_guards():
    assert bridge._prompt_of({"prompt": "  transcribe it  "}) == "  transcribe it  "
    assert bridge._prompt_of({"prompt": "   "}) is None
    assert bridge._prompt_of({}) is None


def test_vision_returns_the_text_and_the_would_be_cost(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "MEDIA_ROOT", str(tmp_path))
    _capture(monkeypatch)

    out = bridge.run_vision({"image_path": _staged(tmp_path), "prompt": "x"})

    assert out["ok"] is True
    assert out["text"] == "the ¼-inch jack"
    assert out["cost_usd"] == 0.02
