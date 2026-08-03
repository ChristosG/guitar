#!/usr/bin/env bash
# HOST BOX ONLY (the machine running the docker-compose deployment) — never CI.
#
# Snapshot the live deployment into seed.tar.gz and publish it to the
# `seed-data` GitHub release, where the desktop CI builds pick it up:
#
#   seed.tar.gz
#   ├── db.dump         pg_dump -Fc of the guitar DB (custom format → pg_restore)
#   ├── media/          the guitar_tutor_media volume (page scans)
#   └── manifest.json   {created, app_commit, pg_major, schema}
#
# The desktop CI unpacks this into Resources/seed/; the app's first run does
# pg_restore --no-owner --no-privileges + a plain copy of media/. The same
# shape is what the future /backup endpoints will emit — keep it stable.
#
# READ-ONLY against production: pg_dump + a tar out of the media volume, exactly
# the invocations scripts/backup.sh has proven. Nothing is stopped or restarted.
#
# ── BEFORE CUTTING A SEED, PRUNE THE SOURCE DB ──────────────────────────────
# The dump is a faithful copy of whatever the live DB holds, and a seed ships
# to EVERY fresh install — so tutor-specific rows become everybody's rows:
#   * `student` and `assignment` rows — the currently shipped seed carries two
#     historical students; the UI no longer surfaces them, but the next cut
#     must not keep smuggling them along.
#   * stale OPEN `curriculum_interview` rows — a half-finished interview from
#     the live box would greet a brand-new install mid-conversation.
# Delete those from the source DB (or a scratch restore of it) before running
# this script. Nothing here can do it automatically: this script is read-only
# against production BY DESIGN, and that is not the invariant to spend.
# ────────────────────────────────────────────────────────────────────────────
#
# Usage: ./make-seed.sh    (requires docker + gh auth on the host)
# shellcheck source=./common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

[ -z "${CI:-}" ] || die "make-seed.sh is for the host box, never CI"
command -v docker >/dev/null || die "docker not found"
command -v gh >/dev/null || die "gh not found"

PG_CONTAINER="guitar_tutor-postgres-1"
MEDIA_VOLUME="guitar_tutor_media"

# Verify the container actually exists under the name backup.sh relies on.
docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER" \
  || die "container '$PG_CONTAINER' is not running. docker ps names: $(docker ps --format '{{.Names}}' | tr '\n' ' ')"

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

log "pg_dump -Fc (read-only)"
docker exec "$PG_CONTAINER" pg_dump -Fc -U guitar -d guitar > "$BUILD/db.dump"
[ -s "$BUILD/db.dump" ] || die "db.dump is empty"

log "archiving media volume"
# Stream the volume out via a throwaway alpine (backup.sh's proven pattern) and
# extract on the host so file ownership lands on the current user.
#
# vision-scratch/ is EXCLUDED: it holds the OCR pipeline's working JPEGs, which
# no DB row ever points at. Shipped in a seed they are pure ballast — the
# orphan-media sweep quarantines only UUID-named dirs, so they would just sit
# there forever, unowned. One pattern suffices: busybox tar's fnmatch treats
# it as a leading-dir match, dropping the directory entry AND its contents
# (verified against alpine's tar; it also matches a NESTED
# …/media/vision-scratch, which the {media_dir}/{uuid}/{page}.jpg layout can
# never produce).
docker run --rm -v "$MEDIA_VOLUME":/media:ro alpine \
  tar cf - --exclude='media/vision-scratch' -C / media \
  | tar xf - -C "$BUILD"
[ -d "$BUILD/media" ] || die "media/ extraction failed"

log "manifest.json"
cat > "$BUILD/manifest.json" <<EOF
{
  "created": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "app_commit": "$(git -C "$REPO_ROOT" rev-parse HEAD)",
  "pg_major": 16,
  "schema": "alembic"
}
EOF

OUT="$DESKTOP_DIR/seed.tar.gz"
tar czf "$OUT" -C "$BUILD" db.dump media manifest.json
log "seed built: $OUT ($(du -sh "$OUT" | awk '{print $1}'))"

log "publishing to the seed-data release"
gh release create seed-data --notes "data snapshot" || true
gh release upload seed-data "$OUT" --clobber

log "done — CI picks it up with: gh release download seed-data -p seed.tar.gz"
