#!/usr/bin/env bash
# Stage the API's runtime: astral-sh/python-build-standalone CPython 3.12
# (install_only_stripped) with apps/api pip-installed into its site-packages
# → resources/python/, plus the API source tree (app/, alembic/, alembic.ini)
# → resources/api/.
#
# pip install runs the TARGET interpreter, so the host must match TARGET.
#
# CI caches resources/python keyed on the pyproject.toml hash; the marker file
# .staged.json makes a cache hit a no-op here (the cheap resources/api restage
# always runs, so API source changes never need a cache bust).
#
# Usage: TARGET=linux-x64 ./stage-python.sh
#   PBS_TAG=<release tag> pins a python-build-standalone release ("latest" default).
# shellcheck source=./common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
target_init "${1:-}"

PBS_TAG="${PBS_TAG:-latest}"
API_SRC="$REPO_ROOT/apps/api"
PY_OUT="$RES_DIR/python"
API_OUT="$RES_DIR/api"
MARKER="$PY_OUT/.staged.json"
PYPROJECT_SHA="$(sha256_of "$API_SRC/pyproject.toml")"

case "$TARGET" in
  darwin-arm64) TRIPLE="aarch64-apple-darwin" ;;
  linux-x64)    TRIPLE="x86_64-unknown-linux-gnu" ;;
esac

stage_api_source() {
  rm -rf "$API_OUT"
  mkdir -p "$API_OUT"
  cp -R "$API_SRC/app" "$API_OUT/app"
  cp -R "$API_SRC/alembic" "$API_OUT/alembic"
  cp "$API_SRC/alembic.ini" "$API_OUT/alembic.ini"
  strip_pycache "$API_OUT"
  log "api source staged: $API_OUT"
}

if [ -f "$MARKER" ] && [ -x "$PY_OUT/bin/python3.12" ] \
   && grep -q "\"pyproject_sha256\": \"$PYPROJECT_SHA\"" "$MARKER"; then
  log "resources/python already staged for this pyproject.toml — skipping fetch+install"
  stage_api_source
  exit 0
fi

assert_host_matches_target

# ---- resolve the python-build-standalone asset ------------------------------
if [ "$PBS_TAG" = "latest" ]; then
  API_URL="https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
else
  API_URL="https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/$PBS_TAG"
fi
AUTH_ARGS=()
[ -n "${GITHUB_TOKEN:-}" ] && AUTH_ARGS=(-H "Authorization: Bearer $GITHUB_TOKEN")

log "resolving python-build-standalone release ($PBS_TAG) for $TRIPLE"
RELEASE_JSON="$(curl -fsSL --retry 3 "${AUTH_ARGS[@]}" "$API_URL")"
ASSET_URL="$(
  printf '%s' "$RELEASE_JSON" | python3 -c "
import json, re, sys
rel = json.load(sys.stdin)
pat = re.compile(r'^cpython-3\.12\.\d+\+\d+-$TRIPLE-install_only_stripped\.tar\.gz$')
for a in rel['assets']:
    if pat.match(a['name']):
        print(a['browser_download_url'])
        break
"
)"
[ -n "$ASSET_URL" ] || die "no cpython-3.12 install_only_stripped asset for $TRIPLE in release $PBS_TAG"
ASSET_NAME="$(basename "$ASSET_URL")"

fetch "$ASSET_URL" "$CACHE_DIR/python/$ASSET_NAME"

# ---- unpack + install -------------------------------------------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar -xzf "$CACHE_DIR/python/$ASSET_NAME" -C "$TMP"
[ -x "$TMP/python/bin/python3.12" ] || die "unexpected archive layout: no python/bin/python3.12"

rm -rf "$PY_OUT"
mkdir -p "$(dirname "$PY_OUT")"
mv "$TMP/python" "$PY_OUT"

PY="$PY_OUT/bin/python3.12"
"$PY" -m pip --version >/dev/null 2>&1 || "$PY" -m ensurepip --upgrade

log "pip install apps/api into the staged interpreter"
PIP_DISABLE_PIP_VERSION_CHECK=1 "$PY" -m pip install --no-compile --no-cache-dir "$API_SRC"

# ---- slim down --------------------------------------------------------------
strip_pycache "$PY_OUT"
SITE="$PY_OUT/lib/python3.12/site-packages"
# Package test suites are dead weight in a bundle (top-level only — never
# touch package internals beyond the conventional tests/ dirs).
for d in "$SITE"/*/tests "$SITE"/*/test; do
  [ -d "$d" ] && rm -rf "$d"
done
rm -rf "$PY_OUT/share" 2>/dev/null || true

printf '{"pyproject_sha256": "%s", "asset": "%s"}\n' "$PYPROJECT_SHA" "$ASSET_NAME" > "$MARKER"

stage_api_source
log "python staged: $PY_OUT ($(du -sh "$PY_OUT" | awk '{print $1}'))"
