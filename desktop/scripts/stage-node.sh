#!/usr/bin/env bash
# Fetch the Node 22 LTS dist for TARGET from nodejs.org (checksum-verified
# against SHASUMS256.txt) and stage bin/node only into resources/node/bin/node.
#
# Usage: TARGET=linux-x64 ./stage-node.sh   [NODE_VERSION=22.14.0 to override]
# shellcheck source=./common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
target_init "${1:-}"

NODE_VERSION="${NODE_VERSION:-22.14.0}"

case "$TARGET" in
  darwin-arm64) PLAT="darwin-arm64" ;;
  linux-x64)    PLAT="linux-x64" ;;
esac

TARBALL="node-v$NODE_VERSION-$PLAT.tar.gz"
BASE="https://nodejs.org/dist/v$NODE_VERSION"
CACHE="$CACHE_DIR/node"

fetch "$BASE/$TARBALL" "$CACHE/$TARBALL"
fetch "$BASE/SHASUMS256.txt" "$CACHE/SHASUMS256-v$NODE_VERSION.txt"

EXPECTED="$(awk -v t="$TARBALL" '$2 == t {print $1}' "$CACHE/SHASUMS256-v$NODE_VERSION.txt")"
[ -n "$EXPECTED" ] || die "$TARBALL not found in SHASUMS256.txt"
ACTUAL="$(sha256_of "$CACHE/$TARBALL")"
[ "$ACTUAL" = "$EXPECTED" ] || die "checksum mismatch for $TARBALL: got $ACTUAL want $EXPECTED"
log "checksum ok: $TARBALL"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar -xzf "$CACHE/$TARBALL" -C "$TMP"

rm -rf "$RES_DIR/node"
mkdir -p "$RES_DIR/node/bin"
install -m 755 "$TMP/node-v$NODE_VERSION-$PLAT/bin/node" "$RES_DIR/node/bin/node"

log "node staged: $RES_DIR/node/bin/node ($(du -sh "$RES_DIR/node" | awk '{print $1}'))"
