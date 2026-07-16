# Core decisions — for Chris (2026-07-16, from the overnight session)

Everything below either needs your call or your client's answer. Nothing here
blocks daily use; the app runs with sensible defaults on all of them.

## 1. Bundling target (the big one)
**Recommendation: Tauri shell + `docker-compose.client.yml`** (added tonight,
repo root — no dev network, no worker, no claude-bridge, restart policies,
auth baked on). Full analysis in `docs/BUNDLING_SKETCH.md`.
- **Ask the client which iMac**: Intel (amd64) or Apple Silicon (arm64).
  Images must be built for the right arch — emulation multiplies OCR and
  embedding latency several-fold.
- Accept the Docker Desktop dependency (~1.5GB install, ~1-2GB idle RAM,
  "start at login" required)? If Docker is vetoed → embedded-Postgres sidecar
  (weeks of work), **never SQLite** (it silently breaks Greek search).

## 2. Backups
`scripts/backup.sh` exists and works (tested: DB 1.3MB + media 19MB, keeps
last 14). **Decide where backups live on the iMac** — the same disk protects
against bad updates and `down -v`, not disk death. External drive or a
cloud-synced folder is the real answer. Schedule via launchd when bundling.

## 3. Streaming chat still double-bills tool-calling turns (~2× on those turns)
The stream endpoint runs the full model call, then discovers the model wants a
tool, discards everything, and the client re-sends via REST — the identical
first call is billed twice. This is *by design* from the free-vLLM era. I
fixed the *transport-error* double-billing tonight, but the tool-call path
needs the loop to accept a pre-computed first response (surgical but touchy —
~1 day). **Worth it**: every «χώρισε το μάθημα...» command costs double today.

## 4. Session lifetime
Login lasts a hard 14 days, then a surprise logout (no rolling renewal). For a
single-user local app I'd set it to 365 days or disable auth entirely in the
bundle (localhost-only ports already gate access). Your call — it's one env var.

## 5. The "Today" page
It's a placeholder (scheduling was never built). Either build simple lesson
scheduling (student + weekday + time, feeding the Today cards), or drop the
page from the nav for the client build. It's the first thing he sees today.

## 6. Reader deep-link accuracy (P2, known, not fixed tonight)
Citation links open the Reader at roughly the right page, but rows above are
still loading and the viewport can drift a few pages on slow loads. Real fix:
scroll-anchoring on row-height changes (~half a day of fiddly frontend work).

## 7. Chat sidebar history
Old test conversations (English demos, "hello", etc.) are still in the DB.
Empty ones now auto-prune after 7 days, but the tutor may want a "clear all"
button, or you can wipe chat history before delivery.

## 8. `gear_card` artifact kind
Removed from the pickers tonight (it had no renderer — a paid call producing a
permanently-dead card). Decide: build its renderer, or delete the kind
server-side too.

## Remaining known issues (verified, deliberately deferred)
Full list with file/line in `docs/AUDIT_FINDINGS.md`. Highlights:
- Interview outline regeneration has no in-flight guard (double-click →
  two paid full-library calls). The dialog disables its button while
  submitting, so exposure is a re-opened dialog during a running job.
- `chat-panel`/`api.ts` stream has no AbortController (dead connections
  linger after switching conversations — cosmetic locally).
- Library-search snippets don't highlight Greek matches (results are right,
  evidence shown is the chunk head).
- Note "unlink student" silently no-ops (backend drops nulls; needs the same
  0-sentinel treatment est_minutes got, or a dedicated clear flag).
