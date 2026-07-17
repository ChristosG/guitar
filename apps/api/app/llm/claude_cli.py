"""`ClaudeCLIProvider` — Claude on the tutor's SUBSCRIPTION, via `claude -p`.

The third implementation of the `LLMProvider` seam, and the only one that costs
nothing per token. `QwenVLLM` is a local 9B (free, weak at Greek).
`ClaudeProvider` is the Anthropic SDK (excellent, needs a paid API key — a
SEPARATE wallet from a Claude subscription; a Max plan buys you zero API
credits). This one gets the quality of the second at the price of the first, by
shelling out to the Claude Code CLI, which authenticates with the OAuth token
`claude` writes to `~/.claude` when you log in.

It is a PROOF OF CONCEPT and should be read as one. The intended end state is
`LLM_PROVIDER=claude` with a real key; this exists to answer "is Claude actually
better enough at Greek to be worth paying for" without paying first. Three
things are worse here than on the API, all of them stated below rather than
discovered later.

WHAT IS WORSE THAN THE API, AND WHY WE ACCEPT IT

1. NO NATIVE TOOL CALLING. `claude -p` cannot call THIS APP's tools — it only
   has Claude Code's own built-ins (which we disable outright; see the bridge).
   So `chat_tools` EMULATES tool calling on top of structured output: the tools
   go into the system prompt, and `--json-schema` forces the reply into
   `{content, tool_calls:[...]}`. That is not a hack for its own sake — the
   schema is validated SERVER-SIDE, so the shape is guaranteed in a way a
   "please reply with JSON" prompt never is. It is still weaker than real tool
   calling: no `tool_choice: required` enforcement (we ask, in words), and the
   model never sees a formal tool schema, only a prose rendering of one.

2. NO TOKEN-LEVEL STREAMING. `chat_tools_stream` is deliberately NOT
   implemented, so the ABC's `NotImplementedError` stands and
   `routers/chat.py` falls back to the REST turn — which `base.py` already
   blesses as an honest degradation. The CLI *can* stream (`--output-format
   stream-json`), but streaming AND emulated tool calls AND the loop's
   `done`-event contract is three moving parts for a PoC that a real API key
   deletes anyway. The tutor sees a pause instead of a typewriter. Named, not
   hidden.

3. ~1 SECOND OF NODE BOOT, PER CALL. Every call forks a Node process. Measured
   steady state is ~3s wall for a short Haiku turn, of which ~2s is the model
   and ~1s is the runtime starting up. Irrelevant next to a 40s lesson draft;
   very noticeable on a one-line chat reply.

WHAT IS NOT WORSE
  - `guided_json` uses `--json-schema`, which is REAL server-side structured
    output — the same feature `ClaudeProvider` reaches via `output_config.format`,
    reached through a different door. Same schema sanitizer (`llm/schema.py`),
    same guarantees. This is the one place the CLI is not a compromise at all.

OCR IS CLAUDE NOW — SEE `vision()`. It used to delegate to Qwen, and that hybrid
died on a fact: the tutor's books are scans carrying someone else's Tesseract
OCR, and it lost every fraction glyph — Kahn p63 says "4-inch stereo cables",
which is not a typo but a wrong fact, since ¼-inch is what the paragraph is
about. Qwen is a local 9B that loses the same class of detail. All 888 pages are
being re-read, so this provider had to grow real eyes.

That is also the ONE place the bridge's `--tools ""` posture is relaxed, because
`claude -p` has no image parameter and the `Read` tool plus a real file is the
only way in. It is a separate endpoint (`/v1/vision`) with one tool, a read-only
mount of the page scans alone, and a path validated before the subprocess starts.
See `vision()` and `tools/claude_bridge/bridge.py::run_vision`.

THE PROCESS DOES NOT RUN HERE. This class is an HTTP client. The subprocess runs
in the sibling `claude-bridge` container (`tools/claude_bridge/`, wired up in
docker-compose.yml), which is the ONLY container that ever sees `~/.claude`.

That split is deliberate and it is a security boundary, not a packaging detail.
THIS container is the web-facing one — it parses uploads, serves a chat box, and
runs an agent loop over text a user typed. It is the thing worth attacking, and it
holds no credential. The bridge container runs exactly one thing (`claude -p`, with
every built-in tool disabled), talks to nothing but this process, and publishes no
port to the host. The mounted `~/.claude/.credentials.json` is not one token —
alongside the Claude OAuth token it holds live access AND refresh tokens for every
MCP server the tutor has ever authorised — which is precisely why it is not mounted
into an app container. See `tools/claude_bridge/README.md`.
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.llm.base import LLMProvider
from app.llm.errors import GuidedJSONError, LLMError
from app.llm.schema import to_anthropic_schema
from app.llm.tools_types import AssistantTurn, ToolCall

log = logging.getLogger(__name__)

# App model id -> the alias `claude -p --model` wants. The app's Settings screen
# and `models/setting.py::MODELS` speak in full ids; the CLI is happy with either,
# but the aliases track "latest" and are what the CLI documents.
_MODEL_ALIAS = {
    "claude-sonnet-5": "sonnet",
    "claude-haiku-4-5": "haiku",
}

# Role -> how hard to think. Mirrors `claude.py::_ROLES` in intent, but there is
# no `_CAPABILITIES` twin here and that is not an oversight: on the raw API,
# Haiku 4.5 *400s* on `output_config.effort`, which is why `claude.py` needs a
# per-model capability table to avoid composing an illegal request. Through the
# CLI, `--effort high --model haiku` was probed and simply works (the CLI
# reconciles it). One less table to keep in sync.
#
# `max_tokens` has no equivalent flag at all — the CLI decides the output cap
# from the model. That removes `claude.py`'s single sharpest failure mode (a
# Greek lesson truncated at `max_tokens`, surfacing as a JSON parse error), and
# replaces it with no control whatsoever. A net win for a PoC; a real
# constraint if a draft ever comes back cut off.
_EFFORT: dict[str, str] = {
    "default": "medium",
    "chat":    "medium",
    "plan":    "high",
    "draft":   "high",
    "spec":    "medium",
    # NOT USED BY `vision()`, which is the only OCR path here. `/v1/vision` sends
    # no `--effort`: the bridge composes its own argv for the one invocation that
    # has a tool enabled, and adding a knob to that argv means widening the
    # narrowest surface in the system to tune something that has never been the
    # bottleneck (a page is ~40s, nearly all of it reading pixels). Kept so the
    # role table still matches `claude.py`'s.
    "ocr":     "low",
    # Reading a WHOLE book into the concept canon. Effort is `medium` — the same the
    # compile has always run at (it used role="spec") — so the ONLY thing this role
    # changes is the timeout below, not the model's behaviour. See `_GUIDED_TIMEOUT_S`.
    "compile": "medium",
}

# Role -> the timeout `guided_json` grants, where it differs from the default. This
# is a DIFFERENT axis from `_EFFORT`: "how hard to think" is not "how long the read
# may take". `compile` reads a WHOLE book in one `guided_json` call — the LONGEST
# call type in the app. Gallagher is 366K tokens (~1.75x Hunter's 209K, which
# already took ~290s via `claude -p`), and that variable, agentic read overran the
# 600s a curriculum draft gets and died with a `ReadTimeout`. 1800s is ~2x the
# worst per-token rate we measured applied to 366K (Getting Great compiled at
# ~2.4s/1K tokens → ~875s for Gallagher, already past 600s) — headroom for the
# variance, not a blank cheque: a genuine hang still dies at this ceiling and the
# bridge classifies it `timeout`, which `canon_compile.py` records as an honest
# failure. Scoped to `compile` on purpose — a stuck chat/spec turn still fails fast
# at the default below.
_GUIDED_TIMEOUT_S: dict[str, float] = {
    "compile": 1800.0,
}
_DEFAULT_GUIDED_TIMEOUT_S = 600.0

# Where `vision()` stages a page for the bridge to read, RELATIVE to
# `settings.media_dir` — the volume both containers mount (this one rw, the bridge
# ro). Deliberately not a UUID: `brain/media.py::sweep_orphaned_media` purges
# UUID-named directories with no matching source and leaves everything else alone.
_VISION_SCRATCH = "vision-scratch"

_VISION_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

# One page is ~40s: Node boots (~1s), then the model reads an 11-megapixel scan and
# may spend an agentic turn or two zooming into it. This is deliberately ~7x that
# — a page that dies at 60s looks like a bug and is really a stopwatch — while
# still being well under the 600s a curriculum draft gets, because 888 pages behind
# a hung one is a different kind of bad day.
_VISION_TIMEOUT_S = 300.0

# The reply shape `chat_tools` forces. `arguments` is a STRING, not an object,
# and that is the load-bearing detail of this whole emulation.
#
# The obvious schema types `arguments` as an object — but Claude's structured
# outputs reject open-ended objects (`additionalProperties` must be `false`, so
# every key must be declared up front), and the arguments differ per tool. The
# alternatives are a `oneOf` branch per registered tool (rebuilt on every call,
# and `TOOLS` has fourteen entries) or... typing it as a JSON string, which is
# exactly what the OpenAI wire format does. `QwenVLLM.chat_tools` already
# `json.loads`es a string here, and `ToolArgsError` already exists for when that
# string is malformed. The emulation lands on the same contract as the real
# thing, for free.
_TOOL_TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "Prose for the tutor. Empty string if only calling tools.",
        },
        "tool_calls": {
            "type": "array",
            "description": "Tools to call now. Empty if answering directly.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {
                        "type": "string",
                        "description": "A JSON OBJECT encoded as a STRING, e.g. "
                                       '"{\\"query\\": \\"tube screamer\\"}"',
                    },
                },
                "required": ["name", "arguments"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["content", "tool_calls"],
    "additionalProperties": False,
}


class ClaudeCLIProvider(LLMProvider):
    def __init__(self, *, model: str | None = None, bridge_url: str | None = None) -> None:
        self._model = model or settings.llm_model
        if self._model not in _MODEL_ALIAS:
            raise ValueError(
                f"Unknown Claude model {self._model!r}. Known: {sorted(_MODEL_ALIAS)}"
            )
        self._alias = _MODEL_ALIAS[self._model]
        self._url = (bridge_url or settings.claude_bridge_url).rstrip("/")
        self._token = settings.claude_bridge_token
        # Same purpose as `ClaudeProvider.last_usage`, with a twist: `cost_usd` is
        # what this call WOULD have cost on an API key. On the subscription it is
        # not billed. It is the number that answers "is a key worth buying yet".
        self.last_usage: dict[str, Any] = {}

    # -- the bridge ----------------------------------------------------------

    def _call(
        self,
        prompt: str,
        *,
        system: str | None = None,
        role: str = "default",
        json_schema: dict | None = None,
        timeout_s: float = 600.0,
    ) -> dict:
        return self._post(
            "/v1/complete",
            {
                "prompt": prompt,
                "system": system,
                "model": self._alias,
                "effort": _EFFORT.get(role, _EFFORT["default"]),
                "json_schema": json_schema,
                "timeout_s": timeout_s,
            },
            timeout_s=timeout_s,
        )

    def _post(self, path: str, body: dict, *, timeout_s: float) -> dict:
        """One HTTP call to the bridge, with the whole failure taxonomy applied.

        Shared by `/v1/complete` and `/v1/vision` so the two cannot drift: every
        distinction below was paid for once (see the comments), and a second
        hand-rolled `httpx.post` in `vision()` would inherit none of them.
        """
        try:
            resp = httpx.post(
                f"{self._url}{path}",
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
                # The bridge's own subprocess timeout is `timeout_s`; give the HTTP
                # client headroom on top so a slow-but-working draft is never killed
                # by the CLIENT while the model is still writing. The failure we want
                # is the bridge's honest "claude -p exceeded 600s", not an ambiguous
                # socket timeout that could equally mean the bridge died.
                timeout=timeout_s + 30,
            )
        except httpx.ConnectError as e:
            raise LLMError(
                "timeout",
                f"Could not connect to claude-bridge at {self._url} — the service is "
                f"down or not on this network. `docker compose ps claude-bridge`. ({e})",
            ) from e
        except httpx.HTTPError as e:
            # NOT the same thing as "it isn't running", and the difference is a wasted
            # afternoon. A RemoteProtocolError ("Server disconnected without sending a
            # response") means the bridge ACCEPTED the request and then died on it —
            # the container is up and healthy, and the real story is in ITS log, not in
            # the network. The first version of this message said "Is it running on the
            # host?" for both cases, and that sentence sent the debugging of a crashed
            # subprocess (OSError E2BIG) off towards DNS and firewalls.
            raise LLMError(
                "upstream",
                f"claude-bridge accepted the request and then failed to answer it "
                f"({type(e).__name__}: {e}). The service is reachable — the failure is "
                f"inside it. Check: docker compose logs claude-bridge",
            ) from e

        if resp.status_code == 401:
            raise LLMError(
                "auth",
                "claude-bridge rejected the token. CLAUDE_BRIDGE_TOKEN in .env must "
                "match the one the bridge was started with.",
            )

        data = resp.json()
        if not data.get("ok"):
            # The bridge already did the classification — it can see the CLI's exit
            # code and stderr, which we cannot. Trust its `kind`; that is the whole
            # point of it returning one. `rate_limit` in particular MUST survive
            # intact: `jobs/runner.py` puts a rate-limited lesson back to `queued`
            # and a `failed` one stays dead, and on a subscription the 5-hour cap is
            # not an exception — it is a Tuesday.
            raise LLMError(data.get("kind") or "upstream",
                           data.get("message") or "claude -p failed")

        self.last_usage = {
            "usage": data.get("usage") or {},
            "cost_usd": data.get("cost_usd"),
            "duration_ms": data.get("duration_ms"),
        }
        return data

    # -- OpenAI transcript -> one prompt string ------------------------------

    @staticmethod
    def _render(messages: list[dict]) -> tuple[str | None, str]:
        """`[{role, content, ...}]` -> (system prompt, one flattened user prompt).

        `claude -p` takes ONE string, not a message array. Everything above this
        seam speaks OpenAI-shaped transcripts (`agent/loop.py` builds one, and
        the database stores one — see `llm/anthropic_wire.py` for why that shape
        stayed), so the transcript is rendered into text.

        This is genuinely lossy and worth being precise about what is lost: the
        model can no longer tell a real `assistant` turn from a line of text that
        merely SAYS "ASSISTANT:". A user who types that into the chat box is
        injecting into the transcript. That is acceptable here — the app is a
        single-tenant tool behind a password, and the tools this can reach are the
        app's own, gated by the loop's HITL approval for every mutation — but it
        is exactly the property a real message array preserves and this does not.
        Do not reach for this renderer in a multi-tenant context.
        """
        system_parts: list[str] = []
        lines: list[str] = []

        for m in messages:
            role = m.get("role")
            content = m.get("content")

            if role == "system":
                if content:
                    system_parts.append(str(content))
            elif role == "user":
                lines.append(f"USER: {content}")
            elif role == "assistant":
                if content:
                    lines.append(f"ASSISTANT: {content}")
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", tc)
                    args = fn.get("arguments")
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    lines.append(f"ASSISTANT (tool call): {fn.get('name')}({args})")
            elif role == "tool":
                # The result of a tool the loop already ran. Feeding it back is what
                # makes the next turn a ReAct step rather than a fresh question.
                lines.append(f"TOOL RESULT ({m.get('name') or m.get('tool_call_id')}): {content}")

        return ("\n\n".join(system_parts) or None), "\n\n".join(lines)

    # -- the ABC -------------------------------------------------------------

    def chat(self, messages, *, temperature: float = 0.3, enable_thinking: bool = False,
             role: str = "chat") -> str:
        # `temperature` is accepted and DISCARDED, exactly as in `claude.py` — the
        # ABC's ten call sites pass it, and there is no CLI flag for it. Same for
        # `enable_thinking`: the CLI's `--effort` is the knob, and `role` picks it.
        system, prompt = self._render(messages)
        return self._call(prompt, system=system, role=role)["text"]

    def guided_json(self, messages, schema, *, temperature: float = 0.2,
                    role: str = "spec", max_tokens: int | None = None) -> dict:
        """Structured output via `--json-schema` — server-side VALIDATED, not a
        prompt asking politely for JSON.

        Same sanitizer as `ClaudeProvider` (`llm/schema.py`), because it is the
        same restriction underneath: `minItems`/`minLength`/`minimum` are not
        supported, and an unsanitized schema fails *after* the model has run.

        `max_tokens` is accepted and ignored — the CLI exposes no such flag. On
        `claude.py` this parameter is what stops a long Greek lesson truncating;
        here the model's own default cap applies and we cannot raise it. The
        `stop_reason` check below is therefore the only line of defence left.
        """
        system, prompt = self._render(messages)
        data = self._call(
            prompt,
            system=system,
            role=role,
            json_schema=to_anthropic_schema(schema),
            # A Greek lesson draft was live-measured at 49-179s on the Qwen path and
            # is not faster here, so 600s is the deliberately-generous default; a
            # draft that dies at 60s looks like a bug and is a stopwatch. The one
            # exception is `compile`, which reads a whole book in one call and gets a
            # far larger ceiling — see `_GUIDED_TIMEOUT_S` for why 600s times out on
            # the tutor's largest book.
            timeout_s=_GUIDED_TIMEOUT_S.get(role, _DEFAULT_GUIDED_TIMEOUT_S),
        )

        structured = data.get("structured")
        if structured is None:
            # `--json-schema` was sent, so `structured_output` should always come
            # back. Its absence means the model produced nothing usable (a refusal,
            # or a cap hit mid-object). Raise the app's own truncation-aware error
            # rather than let a `None` propagate into a caller expecting a dict.
            raise GuidedJSONError(
                f"claude -p returned no structured output (stop_reason="
                f"{data.get('stop_reason')!r}). The response was empty, refused, or "
                f"cut off before the JSON closed."
            )
        if not isinstance(structured, dict):
            raise GuidedJSONError(
                f"claude -p returned structured output of type "
                f"{type(structured).__name__}, expected an object"
            )
        return structured

    def chat_tools(self, messages, tools, *, tool_choice: str = "auto",
                   temperature: float = 0.3, role: str = "chat") -> AssistantTurn:
        """Tool calling EMULATED on top of structured output. See the module
        docstring, point 1.

        Returns the same `AssistantTurn` the other two providers return, so
        `agent/loop.py` cannot tell the difference — including `content=None`
        (never `""`) on a tools-only turn, which is the contract `tools_types.py`
        states and the loop relies on.
        """
        system, prompt = self._render(messages)
        instructions = _tool_system_prompt(tools, tool_choice)
        system = f"{system}\n\n{instructions}" if system else instructions

        data = self._call(prompt, system=system, role=role, json_schema=_TOOL_TURN_SCHEMA)
        turn = data.get("structured")
        if not isinstance(turn, dict):
            raise LLMError("upstream", "claude -p returned no structured tool turn")

        calls: list[ToolCall] = []
        for raw in turn.get("tool_calls") or []:
            name = raw.get("name") or "?"
            args_raw = raw.get("arguments") or "{}"
            try:
                arguments = json.loads(args_raw) if args_raw.strip() else {}
            except json.JSONDecodeError as e:
                # The SAME typed error `QwenVLLM.chat_tools` raises, for the same
                # reason and with the same payload — so the loop's bounded-repair
                # branch (feed the error back, let the model self-correct, cap the
                # streak) works here with no changes. The emulation inherits the
                # failure handling along with the contract.
                from app.llm.errors import ToolArgsError

                raise ToolArgsError(tool_name=name, raw=args_raw) from e
            if not isinstance(arguments, dict):
                from app.llm.errors import ToolArgsError

                raise ToolArgsError(tool_name=name, raw=args_raw)
            # The model never sees a call id (there is no tool protocol here to
            # carry one), so we mint it. The loop only needs it to pair a result
            # back to its call within the turn it built — it is never persisted as
            # anything Anthropic will later validate.
            calls.append(ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=arguments))

        content = (turn.get("content") or "").strip() or None
        return AssistantTurn(content=content, tool_calls=calls)

    # `chat_tools_stream` is NOT implemented — the ABC's NotImplementedError stands
    # and `routers/chat.py` falls back to the REST turn, which `base.py` explicitly
    # blesses. See module docstring, point 2.

    def vision(self, image_bytes: bytes, prompt: str, *, media_type: str = "image/jpeg") -> str:
        """A page scan -> Claude, through a file on a shared volume.

        THIS USED TO DELEGATE TO QWEN, and the reason it no longer does is the
        reason this method is on the critical path. The tutor's books are scans
        carrying someone else's Tesseract OCR, and it lost every fraction glyph:
        page 63 of Kahn reads "4-inch stereo cables", which is not a typo but a
        WRONG FACT — 4-inch cables do not exist, ¼-inch ones are the entire
        subject of the paragraph. Qwen is a local 9B measured losing the same
        class of detail. All 888 pages are being re-read by Claude, so `vision()`
        has to actually be Claude.

        THE PAGE TRAVELS AS A FILE, NOT AS BASE64. `claude -p` has NO image
        parameter — the only route from a scan to the model is the `Read` tool
        plus a real file on disk. Both containers already mount the media volume
        (this one writes it, the bridge reads it read-only), so the page is staged
        there and the PATH is posted. Base64 would mean ~15MB of JSON per page to
        move bytes that are already on the other side of the wall.

        THE STAGED PAGE IS SCRATCH. A unique name because the bridge runs three
        `claude` processes at once and a fixed one would let page 2 overwrite page
        1 in the window before its Read — a transcript of the wrong page, which is
        silent corruption that reads like a bad model. Deleted in a `finally`,
        because 888 leaked scans is a second copy of the library on his disk.

        WHAT IS NOT HANDLED HERE. If `claude -p` wraps the transcription in chatty
        markdown (it may narrate that it zoomed in), that is a PROMPT problem and
        it belongs to the caller that owns the prompt. Scrubbing it here would hide
        it from the only place that can fix it properly, and would risk eating a
        line of a real page along with it.

        THE FLIP POINT is unchanged: with a real API key `ClaudeProvider.vision()`
        takes over with native image blocks, no staging and no `Read` tool, and
        this method plus the whole bridge become dead weight — the intended end
        state, not a regret.
        """
        media_root = Path(settings.media_dir)
        scratch = media_root / _VISION_SCRATCH
        # Not a UUID name, and that is load-bearing: `brain/media.py`'s boot-time
        # `sweep_orphaned_media` deletes UUID-named directories under the media
        # root that have no `KnowledgeSource`, and skips everything else. A scratch
        # dir named like a source id would be swept out from under a live OCR run.
        scratch.mkdir(parents=True, exist_ok=True)
        staged = scratch / f"{uuid.uuid4().hex}{_VISION_EXT.get(media_type, '.jpg')}"

        try:
            staged.write_bytes(image_bytes)
            data = self._post(
                "/v1/vision",
                {
                    # RELATIVE to the media root. The bridge resolves it against
                    # its own root and refuses anything that escapes — it will not
                    # accept an absolute path, because rebasing one would be the
                    # same bug as trusting it.
                    "image_path": f"{_VISION_SCRATCH}/{staged.name}",
                    "prompt": prompt,
                    "model": self._alias,
                    "timeout_s": _VISION_TIMEOUT_S,
                },
                timeout_s=_VISION_TIMEOUT_S,
            )
        finally:
            # The page is scratch even when the call raised — and ESPECIALLY then:
            # the run that fails is the one that hit the 5-hour cap, and it is the
            # one that gets retried 888 times.
            staged.unlink(missing_ok=True)

        return data.get("text") or ""

    def health(self) -> dict:
        """Is the bridge up and is there a live credential behind it — for FREE.

        NOT a `claude -p` ping. `/health/ready` is polled, and a probe that spent
        a real call would burn the tutor's 5-hour rate limit to answer "are we
        up". The bridge reads the OAuth token's own `expiresAt` instead. That
        cannot prove the token is ACCEPTED — only that one exists and has not
        expired — which is the honest ceiling of a zero-cost probe, and it does
        catch the failure that actually happens (logged out).
        """
        try:
            resp = httpx.get(f"{self._url}/health", timeout=5)
            resp.raise_for_status()
            cred = resp.json().get("credentials") or {}
            return {"llm": bool(cred.get("present") and not cred.get("expired"))}
        except Exception:
            log.warning("claude-bridge health probe failed at %s", self._url, exc_info=True)
            return {"llm": False}

    # `count_tokens` is NOT overridden: the CLI has no token-counting endpoint, so
    # the ABC's pessimistic chars/3 heuristic stands. It feeds one decision — does
    # the tutor's library fit whole in the prompt (`curriculum/corpus.py`, 600K
    # budget) — and over-estimating only costs an earlier, visible fallback to
    # retrieval. `ClaudeProvider` overrides it with the free, exact
    # `messages.count_tokens`; this provider cannot, and says so.


def _tool_system_prompt(tools: list[dict], tool_choice: str) -> str:
    """Render OpenAI tool schemas into the prose the model actually sees.

    This is the emulation's weakest joint and deserves naming: on the real API
    the tool schema is a first-class, validated object. Here it is TEXT, and the
    model's compliance is a matter of instruction-following rather than protocol.
    What claws most of that back is that the *reply* is still schema-validated —
    the model cannot return a malformed turn even if it misreads a tool.
    """
    lines = ["AVAILABLE TOOLS — call them by name, with arguments matching the JSON Schema shown."]
    for t in tools:
        fn = t.get("function", t)
        params = json.dumps(fn.get("parameters") or {}, ensure_ascii=False)
        lines.append(f"\n- {fn.get('name')}: {fn.get('description', '').strip()}\n  parameters: {params}")

    lines.append(
        "\nREPLY FORMAT. Return the JSON object required by the schema.\n"
        '  - "content": prose for the tutor. Use "" (empty string) when you are only calling tools.\n'
        '  - "tool_calls": the tools to call now. Use [] when you can answer directly.\n'
        '  - each call\'s "arguments" is a JSON object ENCODED AS A STRING.\n'
        "Call a tool only when you need information you do not have, or the tutor asked you to act. "
        "You may call several at once when they are independent."
    )
    if tool_choice == "required":
        # ASKED FOR, NOT ENFORCED. On the real API `tool_choice: {"type": "any"}` is
        # a hard constraint the server applies. Here it is a sentence, and a model
        # that ignores it returns an empty `tool_calls` and the loop sees a plain
        # answer. No caller uses "required" today; if one ever does, it must not
        # assume this is a guarantee.
        lines.append("You MUST call at least one tool in this turn. Do not answer directly.")
    elif tool_choice == "none":
        lines.append("Do NOT call any tool in this turn. Answer directly, with \"tool_calls\": [].")

    return "\n".join(lines)
