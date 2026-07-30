#!/usr/bin/env bash
# Stage Postgres 16.9 + pgvector 0.8.0 into resources/pg/, then PROVE the tree.
#
# DELIBERATE DEVIATION from the original plan's zonky jar: zonky's
# embedded-postgres-binaries txz ships ONLY initdb/pg_ctl/postgres (verified
# 2026-07-30 against embedded-postgres-binaries-linux-amd64-16.9.0.jar), but the
# desktop shell NEEDS psql (Greek-collation assertion), createdb, pg_restore
# (seed restore) and pg_isready (the postgres watchdog). theseus-rs/
# postgresql-binaries publishes the same-purpose embedded builds as the FULL
# distribution (all client tools + server headers), sha256-signed, for exactly
# our two targets — and its pg_config is RELOCATABLE, so pgvector is built
# against the BUNDLED tree itself (perfect ABI match, `make install` drops
# vector.* straight into the right dirs) with no brew/PGDG host postgres at all.
#
#   1. fetch postgresql-16.9.0-<triple>.tar.gz (+.sha256) from
#      github.com/theseus-rs/postgresql-binaries, verify, unpack → resources/pg/
#   2. build pgvector v0.8.0 with PG_CONFIG=resources/pg/bin/pg_config and
#      OPTFLAGS="" (pgvector defaults to -march=native — a CI-built .so would
#      SIGILL on older user CPUs), `make install` into the tree
#   3. ACCEPTANCE: initdb a temp cluster from the ASSEMBLED tree, start it,
#      CREATE EXTENSION vector, vector(384) insert + `<=>` ORDER BY, and the
#      Greek collation assertion. Any miss fails the script — and therefore
#      the CI cache for this tree.
#
# Usage: TARGET=linux-x64 ./stage-postgres.sh
# shellcheck source=./common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
target_init "${1:-}"
assert_host_matches_target

PG_VERSION="16.9.0"
PGVECTOR_TAG="v0.8.0"
PG_OUT="$RES_DIR/pg"

case "$TARGET" in
  darwin-arm64) TRIPLE="aarch64-apple-darwin" ;;
  linux-x64)    TRIPLE="x86_64-unknown-linux-gnu" ;;
esac

TARBALL="postgresql-$PG_VERSION-$TRIPLE.tar.gz"
BASE_URL="https://github.com/theseus-rs/postgresql-binaries/releases/download/$PG_VERSION"

fetch "$BASE_URL/$TARBALL" "$CACHE_DIR/pg/$TARBALL"
fetch "$BASE_URL/$TARBALL.sha256" "$CACHE_DIR/pg/$TARBALL.sha256"
EXPECTED="$(awk '{print $1}' "$CACHE_DIR/pg/$TARBALL.sha256")"
ACTUAL="$(sha256_of "$CACHE_DIR/pg/$TARBALL")"
[ "$ACTUAL" = "$EXPECTED" ] || die "checksum mismatch for $TARBALL: got $ACTUAL want $EXPECTED"
log "checksum ok: $TARBALL"

TMP="$(mktemp -d)"
PGCTL_STARTED=""
cleanup() {
  if [ -n "$PGCTL_STARTED" ]; then
    "$PG_OUT/bin/pg_ctl" -D "$TMP/data" -m immediate stop >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

# ---- 1. unpack --------------------------------------------------------------
log "unpacking postgresql $PG_VERSION ($TRIPLE)"
tar -xzf "$CACHE_DIR/pg/$TARBALL" -C "$TMP"
[ -x "$TMP/postgresql-$PG_VERSION-$TRIPLE/bin/initdb" ] || die "unexpected archive layout"
rm -rf "$PG_OUT"
mkdir -p "$(dirname "$PG_OUT")"
mv "$TMP/postgresql-$PG_VERSION-$TRIPLE" "$PG_OUT"
for tool in initdb pg_ctl postgres psql createdb pg_restore pg_isready pg_config; do
  [ -x "$PG_OUT/bin/$tool" ] || die "bundled tree is missing bin/$tool"
done

# ---- 2. build + install pgvector against the bundled tree -------------------
PGV_SRC="$CACHE_DIR/pgvector-$PGVECTOR_TAG"
if [ ! -d "$PGV_SRC" ]; then
  log "cloning pgvector $PGVECTOR_TAG"
  git clone --depth 1 --branch "$PGVECTOR_TAG" https://github.com/pgvector/pgvector.git "$PGV_SRC"
fi
log "building pgvector against the bundled pg_config (portable: OPTFLAGS='')"
# PG_CONFIG on `clean` too: pgvector's Makefile resolves PGXS via pg_config
# even for clean, and a mac runner has no host pg_config to fall back on.
# On darwin the theseus tree's Makefile.global bakes the -isysroot of the SDK
# it was BUILT with (e.g. MacOSX15.4.sdk) — override with the SDK that is
# actually on this machine, or every compile dies on 'stdio.h' not found.
MAKE_VARS=(PG_CONFIG="$PG_OUT/bin/pg_config" OPTFLAGS="")
case "$TARGET" in
  darwin-*) MAKE_VARS+=(PG_SYSROOT="$(xcrun --show-sdk-path)") ;;
esac
make -C "$PGV_SRC" clean "${MAKE_VARS[@]}" >/dev/null
make -C "$PGV_SRC" -j"$(getconf _NPROCESSORS_ONLN)" "${MAKE_VARS[@]}"
make -C "$PGV_SRC" install "${MAKE_VARS[@]}" >/dev/null

PKGLIBDIR="$("$PG_OUT/bin/pg_config" --pkglibdir)"
SHAREDIR="$("$PG_OUT/bin/pg_config" --sharedir)"
# Postgres loadable modules are .so on Linux and .dylib on macOS.
case "$TARGET" in darwin-*) PGV_MOD="vector.dylib" ;; *) PGV_MOD="vector.so" ;; esac
[ -f "$PKGLIBDIR/$PGV_MOD" ] || die "$PGV_MOD did not land in $PKGLIBDIR"
[ -f "$SHAREDIR/extension/vector.control" ] || die "vector.control did not land in $SHAREDIR/extension"
log "pgvector installed into the tree"

# ---- 3. acceptance ----------------------------------------------------------
# Binaries carry RUNPATH $ORIGIN/../lib, but export the lib dir anyway (belt).
log "acceptance: initdb + start + vector(384) + Greek collation"
case "$TARGET" in
  darwin-arm64) export DYLD_LIBRARY_PATH="$PG_OUT/lib" ;;
  linux-x64)    export LD_LIBRARY_PATH="$PG_OUT/lib" ;;
esac

PORT="$(free_port 54390 54400)" || die "no free acceptance port 54390-54400"
printf 'guitar\n' > "$TMP/pw"
if ! "$PG_OUT/bin/initdb" -U guitar --pwfile="$TMP/pw" --auth-host=scram-sha-256 \
     --auth-local=trust -E UTF8 --locale=en_US.UTF-8 -D "$TMP/data" >"$TMP/initdb.log" 2>&1; then
  log "initdb with en_US.UTF-8 failed; retrying with C.UTF-8"
  rm -rf "$TMP/data"
  "$PG_OUT/bin/initdb" -U guitar --pwfile="$TMP/pw" --auth-host=scram-sha-256 \
    --auth-local=trust -E UTF8 --locale=C.UTF-8 -D "$TMP/data" >"$TMP/initdb2.log" 2>&1 \
    || { cat "$TMP/initdb.log" "$TMP/initdb2.log" >&2; die "initdb failed twice"; }
fi

# NOTE: -k needs a SHORT socket dir — unix sockets cap the path at ~107 bytes.
# mktemp -d under /tmp is short; never point -k at a deep tree.
"$PG_OUT/bin/pg_ctl" -w -t 60 -D "$TMP/data" -l "$TMP/pg.log" \
  -o "-p $PORT -k $TMP -c listen_addresses=127.0.0.1" start \
  || { cat "$TMP/pg.log" >&2; die "acceptance cluster did not start"; }
PGCTL_STARTED=1

psql_t() { "$PG_OUT/bin/psql" -h "$TMP" -p "$PORT" -U guitar -d postgres -v ON_ERROR_STOP=1 -tA -c "$1"; }

psql_t "CREATE EXTENSION vector" >/dev/null || die "CREATE EXTENSION vector failed"

VEC_NEAR="$(python3 -c "print('[' + ','.join(['0.01']*384) + ']')")"
VEC_FAR="$(python3 -c "print('[' + ','.join(['0.99']*384) + ']')")"
psql_t "CREATE TABLE accept_t (id int, embedding vector(384))" >/dev/null
psql_t "INSERT INTO accept_t VALUES (1, '$VEC_NEAR'), (2, '$VEC_FAR')" >/dev/null

NEAREST="$(psql_t "SELECT id FROM accept_t ORDER BY embedding <=> '$VEC_NEAR' LIMIT 1")"
[ "$NEAREST" = "1" ] || die "vector <=> ORDER BY returned '$NEAREST', expected 1"

GREEK="$(psql_t "SELECT lower('ΚΙΘΑΡΑ') = 'κιθαρα'")"
[ "$GREEK" = "t" ] || die "Greek collation assertion failed (lower('ΚΙΘΑΡΑ') != 'κιθαρα')"

"$PG_OUT/bin/pg_ctl" -w -t 20 -D "$TMP/data" -m fast stop
PGCTL_STARTED=""

# ---- 4. slim the proven tree ------------------------------------------------
# Headers were only needed to BUILD pgvector; static libs and docs are dead
# weight. bin/ stays whole — the client tools are the point of this source.
rm -rf "$PG_OUT/include" "$PG_OUT/share/doc" "$PG_OUT/share/man"
find "$PG_OUT/lib" -name '*.a' -delete 2>/dev/null || true

printf '{"postgresql": "%s", "pgvector": "%s", "target": "%s", "source": "theseus-rs/postgresql-binaries"}\n' \
  "$PG_VERSION" "$PGVECTOR_TAG" "$TARGET" > "$PG_OUT/.acceptance-ok"
log "postgres staged AND acceptance-proven: $PG_OUT ($(du -sh "$PG_OUT" | awk '{print $1}'))"
