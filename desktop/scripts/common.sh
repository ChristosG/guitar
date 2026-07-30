# shellcheck shell=bash
# Shared helpers for the desktop staging scripts. Source, don't execute.
#
# Every stage-*.sh is parameterized by TARGET=darwin-arm64|linux-x64 (env var or
# first argument). Downloads are cached under desktop/.cache (override:
# CACHE_DIR); staging output goes to desktop/src-tauri/resources (override:
# RES_DIR — used by dry runs into a scratch dir).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DESKTOP_DIR="$REPO_ROOT/desktop"
RES_DIR="${RES_DIR:-$DESKTOP_DIR/src-tauri/resources}"
CACHE_DIR="${CACHE_DIR:-$DESKTOP_DIR/.cache}"

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "==> $*"; }

# TARGET from env or $1; validates the two supported values.
target_init() {
  TARGET="${TARGET:-${1:-}}"
  case "$TARGET" in
    darwin-arm64|linux-x64) ;;
    *) die "TARGET must be darwin-arm64 or linux-x64 (got '${TARGET:-<unset>}')" ;;
  esac
}

# The host must match TARGET for steps that execute target binaries
# (pip install into the staged python, the postgres acceptance test).
assert_host_matches_target() {
  local os arch host
  os="$(uname -s)"; arch="$(uname -m)"
  case "$os-$arch" in
    Darwin-arm64) host="darwin-arm64" ;;
    Linux-x86_64) host="linux-x64" ;;
    *) host="unsupported($os-$arch)" ;;
  esac
  [ "$host" = "$TARGET" ] || die "this step runs TARGET binaries: host is $host, TARGET is $TARGET"
}

# fetch URL DEST — cached, atomic. A present DEST is trusted (delete to refetch).
fetch() {
  local url="$1" dest="$2"
  if [ -f "$dest" ]; then
    log "cached: $dest"
    return 0
  fi
  mkdir -p "$(dirname "$dest")"
  log "fetch: $url"
  curl -fL --retry 3 --retry-delay 2 --connect-timeout 30 -o "$dest.part" "$url"
  mv "$dest.part" "$dest"
}

# sha256_of FILE — prints the digest, portable across macOS/Linux.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# free_port START END — first TCP port in [START,END] free on 127.0.0.1.
free_port() {
  python3 - "$1" "$2" <<'PY'
import socket, sys
start, end = int(sys.argv[1]), int(sys.argv[2])
for p in range(start, end + 1):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
    except OSError:
        continue
    finally:
        s.close()
    print(p)
    sys.exit(0)
sys.exit(1)
PY
}

# strip_pycache DIR — drop every __pycache__ (they are per-interpreter litter).
strip_pycache() {
  find "$1" -type d -name '__pycache__' -prune -exec rm -rf {} +
}
