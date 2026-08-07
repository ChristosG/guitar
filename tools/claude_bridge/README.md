# claude-bridge

Runs `claude -p` so the app can use Claude on your **Max subscription** instead of a
paid API key. It's a compose service — there is **nothing to run by hand**.

```bash
# .env
LLM_PROVIDER=claude_cli
CLAUDE_BRIDGE_TOKEN=<openssl rand -hex 32>

docker compose up -d --build
```

That's it. `claude-bridge` comes up with the rest of the stack.

```
                    appnet (internal — no host ports)
┌──────────┐                                   ┌─────────────────────────────┐
│   api    │ ──── http://claude-bridge:8799 ──►│ claude-bridge               │
│          │      /v1/complete                 │   claude -p --tools ""      │
│ NO creds │      /v1/vision                   │   claude -p --tools Read    │
│          │                                   │   /home/node/.claude ◄──────┼── bind mount
│          │      media volume                 │                             │        ~/.claude
│  /media  │ ═════════════════════════════════►│   /media (ro)               │   (rw, uid 1000)
│   (rw)   │      api writes, bridge reads     └─────────────────────────────┘
└──────────┘
     ▲
     └── web-facing: uploads, chat box, agent loop
```

## Why a separate container instead of `claude` inside `api`?

Because of what the credential actually is. `~/.claude/.credentials.json` doesn't just
hold your Claude token — it holds live OAuth **access and refresh** tokens for every
MCP server you've ever authorised (Linear, GitLab, Postman, HuggingFace, PagerDuty,
Rootly, Apollo, Logfire).

`api` is the container worth attacking: it parses uploads, serves a chat box, and runs
an agent loop over text a user typed. It gets **none** of that. The bridge container
runs exactly one program — `claude -p` with every built-in tool disabled — holds no
database, serves no browser, and **publishes no port to the host**.

## Why mount the real `~/.claude` directory (rw)?

The two tempting shortcuts both break:

- **A copy of the credential** → two independent OAuth sessions off one refresh token.
  The access token expires in hours and the CLI rewrites the file on refresh; whichever
  copy rotates first can invalidate the other. The symptom is your own terminal `claude`
  demanding a re-login for no visible reason.
- **A single-file bind mount** (`~/.claude/.credentials.json:...`) → breaks on the first
  refresh. The CLI writes a new file and renames it over the old one, so the host gets a
  fresh inode while the container stays bound to the stale one. They diverge silently.

Mounting the **directory** means host and container share one file — exactly what two
`claude` terminals already do. `user: "1000:1000"` keeps everything it writes owned by
you; without it the first refresh drops a root-owned `.credentials.json` in your home
and locks your own CLI out.

## Check it

```bash
docker compose logs claude-bridge          # per-call timings, costs, prompt sizes
docker compose exec api python -c "import httpx;print(httpx.get('http://claude-bridge:8799/health').json())"
curl -s localhost:8791/health/ready        # {"db":true,"llm":true,"embed":true}
```

`/health` is zero-cost — it reads the OAuth token's own expiry rather than pinging the
model. A health probe that spent a real call would burn your 5-hour rate limit to answer
"are we up".

The bridge logs one line per call, which is how you attribute latency (a chat turn is
*several* `claude -p` calls — the ReAct loop makes at least two):

```
claude -p model=sonnet effort=medium schema=False in=418ch   -> 3260ms  out=54ch  cost≈$0.0021
claude -p model=sonnet effort=medium schema=True  in=16186ch -> 13692ms out=571ch cost≈$0.0530
```

`cost≈` is what the call **would** have cost on an API key. On the subscription it isn't
billed — it's the number that tells you whether buying a key is worth it yet.

## API

`POST /v1/complete` — `Authorization: Bearer $CLAUDE_BRIDGE_TOKEN`

```json
{ "prompt": "USER: ...", "system": "You are ...", "model": "sonnet",
  "effort": "medium", "json_schema": {...}, "timeout_s": 600 }
```

→ always `200` (a model failure is still a successful HTTP transaction):

```json
{ "ok": true,  "text": "...", "structured": {...}|null, "usage": {...}, "cost_usd": 0.05 }
{ "ok": false, "kind": "rate_limit|auth|timeout|upstream", "message": "..." }
```

`kind` maps 1:1 onto the app's `LLMError.kind`, and it matters: `jobs/runner.py` puts a
`rate_limit` lesson back to `queued` and leaves an `upstream` one `failed`. On a
subscription the 5-hour cap isn't an exception, it's a Tuesday — misclassify it and you
lose half a curriculum to a limit that would have cleared on its own.

Only a bad token is a `401`, because that's a fact about the transport, not the model.

`POST /v1/vision` — same auth. `image_path` is **relative to the media root**.

```json
{ "image_path": "vision-scratch/a1b2c3.jpg", "prompt": "Transcribe all text verbatim.",
  "model": "sonnet", "timeout_s": 300 }
```

→ `{"ok": true, "text": "...", "cost_usd": 0.04}`, or a `400` if the path isn't a real
file inside the media root.

## The one hole in `--tools ""`, and why it's shaped like this

`/v1/complete` disables every built-in tool. Without that it isn't an LLM, it's a coding
agent driven by whatever a user typed into a chat box.

Vision can't work that way: **`claude -p` has no image parameter**. The only route from a
page scan to the model is the `Read` tool plus a real file on disk. So the hole gets
opened exactly once, and fenced:

| Fence | Why |
|---|---|
| a **separate endpoint** | `/v1/complete` keeps `--tools ""`. Merging them hands the chat box a file reader. |
| `--tools Read` | One named tool. Never a blanket re-enable. |
| `--permission-mode manual` | **The actual fence.** See below. |
| `--settings` deny on `~` and `/proc` | Second, independent fence over the credential itself. |
| `media:/media:ro` | The scans and nothing else — not the repo, not the DB. |
| path checked *before* the fork | A traversal is a `400`, never a `claude` invocation. `commonpath` on realpaths, not `startswith` (`/media-evil/x` starts with `/media`). |

**`--permission-mode manual` is the load-bearing one, and neither of the obvious
candidates is.** The cwd is not a fence and `--add-dir` is not a fence — it *adds* to what
Read may touch. Probed live, with cwd inside the media mount and `--add-dir` set:

```
prompt: "Use your Read tool on /etc/hostname and tell me the exact string"
reply:  "The exact string contained in /etc/hostname is: 7c3faf5bdcb3"
```

It read it. `--tools Read` alone is an arbitrary file read — **in the one container that
holds your real `~/.claude`**. With `--permission-mode manual`, the same probe:

```
"Claude requested permissions to read from /etc/hostname, but you haven't granted it yet."
```

`-p` is non-interactive, so nobody can grant it. The page still reads, because the
workspace is pre-granted. Allow-list, not blocklist.

> An earlier probe that just asked for `.credentials.json` came back clean and **proved
> nothing** — that was the model declining, not the sandbox refusing. Model goodwill is
> not a security boundary. Test the fence with a file the model has no reason to protect.

## Things that will bite you

| Symptom | Cause |
|---|---|
| `claude-bridge` exits: `CLAUDE_BRIDGE_TOKEN is not set` | By design. Set it in `.env`. |
| `health` → `"present": false` | The container can't see your credential. Check the `${HOME}/.claude` mount, and that you've logged in with `claude` on the host at least once. |
| `health` → `"expired": true` | Run `claude` in a terminal once to refresh the OAuth token. |
| api: "Could not reach claude-bridge" | The service isn't up: `docker compose ps`. |
| api: "claude-bridge rejected the token" | `api` and `claude-bridge` read `CLAUDE_BRIDGE_TOKEN` from the same `.env`, so this means one of them has a stale env — recreate both. |
| Everything `kind: rate_limit` | You hit the subscription's 5-hour or weekly cap. Wait, or set `LLM_PROVIDER=qwen`. |
| Your own `claude` suddenly wants a re-login | Should not happen (shared file, one source of truth). If it does, check `ls -la ~/.claude/.credentials.json` is still owned by you, not root — that means `user: "1000:1000"` isn't matching your UID. |

## This is a proof of concept

It exists to answer "is Claude good enough at Greek to be worth paying for" **without
paying first**. Its known limits, all deliberate:

- **No token-level streaming** — the chat UI falls back to a REST turn, so you get a
  ~15-30s pause instead of a typewriter. This is the one that actually hurts.
- **Tool calling is emulated** on top of structured output, not native.
- **Vision goes through a file and the `Read` tool**, because `claude -p` has no image
  parameter — hence `/v1/vision` and the fences above. A page costs ~15-40s and a real
  agentic turn of your 5-hour cap.
- **~1s of Node boot per call**, on top of the model's own latency.

Set `LLM_PROVIDER=claude` with a real key and all four disappear — native streaming,
native tools, native image blocks (no staging, no `Read` tool, no hole to fence), no
subprocess. `app/llm/claude.py` is already written and tested. This whole directory
becomes dead weight, which is the intended end state and not a regret.
