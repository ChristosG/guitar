# Bundling sketch — shipping this to the client's iMac

*(2026-07-16, from a code-level packaging audit. Not this session's scope to
build — this is the map for when we do.)*

## Recommendation

**Thin Tauri shell over a client-profile docker-compose backend.** The shell's
webview loads `http://localhost:8790` (NOT a `tauri://` bundled asset — the
API-base derivation and the host-only session cookie both key off a literal
`localhost` origin). The shell is ~200 lines: run `docker compose up -d`,
poll `/health/live`, show the window.

The zero-cost fallback is the same backend + a browser bookmark — identical
work minus the shell, fine for the first client test.

## Why the codebase is already 80% packaged

- **Embeddings are offline by construction**: e5-small ONNX weights baked into
  the api image at build time, `HF_HUB_OFFLINE=1`. No first-run downloads.
- **BM25 is in-process** (PyStemmer was chosen *explicitly* for a bundled
  desktop app), warmed at boot.
- **All jobs run in-process** in `api` (BackgroundTasks + ThreadPoolExecutor);
  the compose `worker` is a stub and is DELETED from the client bundle.
- **Startup is self-healing**: orphaned jobs failed, interrupted lessons
  re-queued, media swept, retention pruned, migrations run at boot
  (`alembic upgrade head` in the api CMD — added tonight), embedder warmed.
- The **only external service is api.anthropic.com** with the client's own
  key (Fernet-encrypted in the DB, pasted in Settings).

`docker-compose.client.yml` (added tonight, repo root) is the shippable
backend: no dev network, no worker, no claude-bridge (it mounts *your*
`~/.claude` OAuth!), `restart: unless-stopped` everywhere, auth baked on.

## What must be confirmed / done before shipping

1. **iMac CPU arch** — Intel (amd64) vs Apple Silicon (arm64). Images must be
   built for the target; amd64-under-Rosetta multiplies ONNX/OCR latency.
2. Docker Desktop on the iMac: install, "start at login", and accept its
   ~1.5GB footprint / ~1-2GB idle RAM. This is the main tradeoff of this path.
3. Update story: `docker compose pull` from ghcr *or* an air-gapped
   `docker load < bundle.tar` script. Schema updates ride alembic at boot.
   `pgdata`/`media` volumes persist across image swaps.
4. Schedule `scripts/backup.sh` (launchd) and point it at an external disk or
   cloud-synced folder.
5. Tauri shell (2-4 days): spawn compose, health-poll, window, a menu item
   for "logs" and "check for updates".

## Why NOT the alternatives

- **PyInstaller sidecars + SQLite** (3-5 weeks, rejected): pgvector column +
  cosine_distance queries bind the schema to Postgres; the whole alembic
  history imports pgvector; Postgres ILIKE is Unicode-aware but SQLite LIKE
  folds ASCII only, so Greek title search **silently** breaks with all ~1000
  tests green (the exact "Greek IS the product" failure class); FOR UPDATE
  becomes a no-op (the draft fan-out's double-billing guard); SQLite's single
  writer contends with concurrent 32K-token drafts. If Docker is ever vetoed,
  embed **Postgres as a sidecar** (code unchanged) — never SQLite.
- **Electron**: solves nothing (the packaging problem is Python+Postgres+ONNX,
  not the web UI) and adds 200MB of Chromium.
