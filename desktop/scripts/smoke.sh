#!/usr/bin/env bash
# Smoke-test a STAGED resources tree (the same one the bundle ships): boot the
# bundled postgres, run alembic with the bundled python, start uvicorn, poll
# /health/ready until db AND embed are true, then shut everything down cleanly.
# Exits nonzero on any miss. CI runs this against the built bundle's resources.
#
# SCOPE: the API tier only — bundled postgres + bundled python + apps/api + the
# embed model. It does NOT boot node/web and it does NOT stand in for the Tauri
# shell's runtime wiring (the shell picks two free ports at start, injects
# window.__GT_API_BASE__ into the webview and passes the api child a
# CORS_ORIGINS naming the chosen web origin). No browser is involved here, so
# none of that is observable from this script; it lives in desktop/src-tauri.
# What this proves is narrower and still the thing CI needs: the bundled tree,
# as shipped, boots and reaches a ready database + embedder.
#
# NOTE the LLM_API_KEY: with LLM_PROVIDER=claude and NO key at all, the API
# answers /health/ready with 409 llm_not_configured INSTEAD of the JSON body
# (get_provider() raises before the probe dict is built — see app/main.py's
# exception handler). A dummy env key routes us onto the 200 path, where db and
# embed report their REAL state and llm is honestly false. Nothing is ever sent
# to Anthropic — health() only does a models.retrieve that fails locally-cheap.
#
# Usage: ./smoke.sh [resources-dir]   (default: desktop/src-tauri/resources)
# shellcheck source=./common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

RES="${1:-$RES_DIR}"
RES="$(cd "$RES" && pwd)" || die "resources dir not found: ${1:-$RES_DIR}"

# web/server.js and node/bin/node are CHECKED here but never BOOTED — the
# scope above still holds. They are on the list because absence is exactly
# what the one real shipping failure looked like: a bad resources glob once
# produced a bundle with zero web assets, and a smoke that only exercised
# pg+python+api waved it through (see README's CI section). Both paths are
# layout-invariant: stage-web.sh guarantees a root-level web/server.js on
# every layout (it shims one in for nested standalone trees), and
# stage-node.sh installs exactly node/bin/node on macOS and Linux alike.
for f in pg/bin/initdb pg/bin/pg_ctl python/bin/python3.12 api/alembic.ini \
         api/app/main.py models/e5-small/model.onnx models/e5-small/tokenizer.json \
         web/server.js node/bin/node; do
  [ -e "$RES/$f" ] || die "staged tree incomplete: missing $RES/$f"
done

case "$(uname -s)" in
  Darwin) export DYLD_LIBRARY_PATH="$RES/pg/lib" ;;
  *)      export LD_LIBRARY_PATH="$RES/pg/lib" ;;
esac

TMP="$(mktemp -d)"
UVICORN_PID=""
PG_STARTED=""
STATUS=1
cleanup() {
  if [ -n "$UVICORN_PID" ]; then
    kill -TERM "$UVICORN_PID" 2>/dev/null || true
    for _ in $(seq 1 50); do kill -0 "$UVICORN_PID" 2>/dev/null || break; sleep 0.2; done
    kill -KILL "$UVICORN_PID" 2>/dev/null || true
  fi
  if [ -n "$PG_STARTED" ]; then
    "$RES/pg/bin/pg_ctl" -w -t 20 -D "$TMP/data" -m fast stop >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
  [ "$STATUS" -eq 0 ] && log "SMOKE OK" || echo "SMOKE FAILED — logs were in $TMP (removed); see output above" >&2
  exit "$STATUS"
}
trap cleanup EXIT

# Both ports are SCANNED, never fixed, and both ranges sit deliberately far
# from anything the product uses: the app's own web/api pair is itself chosen at
# runtime from 8790/8791 upwards, and a dev compose stack holds 5434/8790/8791.
# So a smoke run can neither collide with a running app nor accidentally probe
# one. Nothing below may hardcode 8791 — this script proves that the STAGED
# TREE boots, not which port any particular shell run happened to get.
PG_PORT="$(free_port 54410 54430)" || die "no free postgres port"
API_PORT="$(free_port 18791 18811)" || die "no free api port"
log "pg on :$PG_PORT, api on :$API_PORT"

printf 'guitar\n' > "$TMP/pw"
if ! "$RES/pg/bin/initdb" -U guitar --pwfile="$TMP/pw" --auth-host=scram-sha-256 \
     --auth-local=trust -E UTF8 --locale=en_US.UTF-8 -D "$TMP/data" >"$TMP/initdb.log" 2>&1; then
  rm -rf "$TMP/data"
  "$RES/pg/bin/initdb" -U guitar --pwfile="$TMP/pw" --auth-host=scram-sha-256 \
    --auth-local=trust -E UTF8 --locale=C.UTF-8 -D "$TMP/data" >>"$TMP/initdb.log" 2>&1 \
    || { cat "$TMP/initdb.log" >&2; die "initdb failed"; }
fi

"$RES/pg/bin/pg_ctl" -w -t 60 -D "$TMP/data" -l "$TMP/pg.log" \
  -o "-p $PG_PORT -k $TMP -c listen_addresses=127.0.0.1" start \
  || { cat "$TMP/pg.log" >&2; die "postgres did not start"; }
PG_STARTED=1

"$RES/pg/bin/createdb" -h "$TMP" -p "$PG_PORT" -U guitar guitar \
  || die "createdb failed"

mkdir -p "$TMP/media"
export DATABASE_URL="postgresql+psycopg://guitar:guitar@127.0.0.1:$PG_PORT/guitar"
export MEDIA_DIR="$TMP/media"
export EMBED_BACKEND="local-e5"
export EMBED_MODEL_DIR="$RES/models/e5-small"
export HF_HUB_OFFLINE=1
export LLM_PROVIDER="claude"
export LLM_API_KEY="smoke-test-not-a-real-key"
export AUTH_ENABLED=0
export APP_SECRET="smoke-app-secret"
export ENCRYPTION_SECRET="smoke-encryption-secret"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
# The backup endpoints shell out to pg_dump/pg_restore from THIS tree, exactly
# as the desktop shell points them (supervisor.rs sets the same variable).
export PG_BIN_DIR="$RES/pg/bin"

PY="$RES/python/bin/python3.12"

log "alembic upgrade head"
(cd "$RES/api" && "$PY" -m alembic upgrade head) || die "alembic failed"

log "starting uvicorn"
# `exec` so $! is uvicorn itself, not a wrapper subshell — the TERM in cleanup
# must reach the server.
(cd "$RES/api" && exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" \
  >"$TMP/uvicorn.log" 2>&1) &
UVICORN_PID=$!

log "polling /health/ready for db+embed (llm stays false — no real key)"
DEADLINE=$(( $(date +%s) + 150 ))
while :; do
  BODY="$(curl -fsS --max-time 5 "http://127.0.0.1:$API_PORT/health/ready" 2>/dev/null || true)"
  case "$BODY" in
    *'"db":true'*'"embed":true'*) log "ready: $BODY"; break ;;
  esac
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "--- uvicorn.log (tail) ---" >&2; tail -50 "$TMP/uvicorn.log" >&2 || true
    die "timed out waiting for db+embed (last body: ${BODY:-<none>})"
  fi
  kill -0 "$UVICORN_PID" 2>/dev/null || { tail -50 "$TMP/uvicorn.log" >&2 || true; die "uvicorn exited early"; }
  sleep 2
done

# ---- backup round trip ------------------------------------------------------
# The tutor's Mac (2026-09-13) could not restore an archive that his Linux box
# restored fine, and nothing in CI had ever exercised /backup on the bundled
# tree: export needs pg_dump, restore needs pg_restore + alembic + a media swap,
# and all four run OUT of the bundle on the desktop (PG_BIN_DIR). So: export,
# restore that export, then restore the bundled seed (a real archive with page
# scans) and prove rows AND files came back. Any miss fails the smoke.
# Counts the rows in a list body or a {"items": [...]} body. Anything else —
# an error object, a string, a non-JSON page — prints 0 rather than dying with
# a traceback inside a `$(...)` whose failure `set -e` would report as a bare
# exit code.
api_json() {
  "$PY" -c 'import sys,json
try:
    d = json.load(sys.stdin)
except Exception:
    print(0); raise SystemExit(0)
if isinstance(d, list):
    print(len(d))
elif isinstance(d, dict) and isinstance(d.get("items"), list):
    print(len(d["items"]))
else:
    print(0)'
}

# Any of the checks below failing under `set -e` would otherwise kill the
# script with no explanation at all; say what broke and show the server log.
smoke_die() {
  echo "--- uvicorn.log (tail) ---" >&2; tail -40 "$TMP/uvicorn.log" >&2 || true
  die "$1"
}

log "backup: export"
curl -fsS --max-time 300 -o "$TMP/export.tar.gz" "http://127.0.0.1:$API_PORT/backup/export" \
  || { tail -30 "$TMP/uvicorn.log" >&2; die "/backup/export failed (pg_dump missing from the bundle?)"; }
# List ONCE into a file, then grep the file. `tar tzf ... | grep -q` closes the
# pipe on the first match, tar takes SIGPIPE, and `pipefail` then fails the
# pipeline — on a VALID archive, as soon as it carries more members than the
# one being matched (a real backup carries media). The bug only appears once
# there is data to back up, which is exactly when it matters.
tar tzf "$TMP/export.tar.gz" > "$TMP/members" \
  || { tail -30 "$TMP/uvicorn.log" >&2; die "export archive is not readable as a tar.gz"; }
grep -qx 'db.dump' "$TMP/members" || die "export archive has no db.dump"
grep -qx 'manifest.json' "$TMP/members" || die "export archive has no manifest.json"

# Real JSON, not a substring match: `*'"ok":true'*` also matches an error body
# that happens to quote one (`{"detail":{"message":"expected \"ok\":true"}}`),
# and misses the same true value pretty-printed with a space after the colon.
# A body that is not JSON at all (an HTML 502, an empty response) is a failure.
restore_ok() {
  printf '%s' "$1" | "$PY" -c \
    'import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") is True else 1)' \
    2>/dev/null
}

restore_archive() {
  local archive="$1" what="$2" body
  body="$(curl -sS --max-time 600 -X POST -F "file=@$archive" "http://127.0.0.1:$API_PORT/backup/restore" || true)"
  if restore_ok "$body"; then
    log "backup: restore of $what ok"
  else
    echo "--- uvicorn.log (tail) ---" >&2; tail -40 "$TMP/uvicorn.log" >&2 || true
    die "restore of $what failed: ${body:-<no body>}"
  fi
}

restore_archive "$TMP/export.tar.gz" "our own export"

if [ -f "$RES/seed/db.dump" ] && [ -f "$RES/seed/manifest.json" ]; then
  # Re-pack the bundled seed exactly as an exported archive is laid out.
  tar czf "$TMP/seed.tar.gz" -C "$RES/seed" db.dump manifest.json $( [ -d "$RES/seed/media" ] && echo media )
  restore_archive "$TMP/seed.tar.gz" "the bundled seed"
  CURRICULA="$(curl -fsS --max-time 30 "http://127.0.0.1:$API_PORT/curricula" || true)"
  [ -n "$CURRICULA" ] || smoke_die "GET /curricula returned nothing after the seed restore"
  N="$(printf '%s' "$CURRICULA" | api_json)" \
    || smoke_die "could not parse GET /curricula: $CURRICULA"
  [ "${N:-0}" -ge 1 ] || smoke_die "after restoring the seed, /curricula lists $N curricula"
  if [ -d "$RES/seed/media" ]; then
    WANT="$(find "$RES/seed/media" -type f | wc -l | tr -d ' ')" \
      || smoke_die "could not count the seed's media files"
    GOT="$(find "$MEDIA_DIR" -type f | wc -l | tr -d ' ')" \
      || smoke_die "could not count the media files in $MEDIA_DIR"
    [ "$GOT" = "$WANT" ] || smoke_die "media swap: seed has $WANT files, MEDIA_DIR has $GOT"
  fi
  log "backup: seed restored — $N curricula, media files match"
else
  log "backup: no bundled seed — round trip proven on the export only"
fi

log "clean shutdown"
# `|| true`: under `set -e` a server that has ALREADY exited cleanly (it is
# about to be waited on anyway) would otherwise fail the whole smoke at the
# very last step, after everything it tests has passed.
kill -TERM "$UVICORN_PID" || true
wait "$UVICORN_PID" 2>/dev/null || true
UVICORN_PID=""
"$RES/pg/bin/pg_ctl" -w -t 20 -D "$TMP/data" -m fast stop
PG_STARTED=""
STATUS=0
