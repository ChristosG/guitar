#!/usr/bin/env bash
# Backup the ONE thing that cannot be regenerated for free: the database
# (curricula, chats, notes, settings, OCR'd text — content that cost real
# Anthropic API money) and the media volume (page scans).
#
# Usage:            ./scripts/backup.sh [target-dir]      (default: ./backups)
# Restore database: gunzip -c backups/guitar-tutor-YYYY-MM-DD.sql.gz \
#                     | docker exec -i guitar_tutor-postgres-1 psql -U guitar -d guitar
# Restore media:    docker run --rm -v guitar_tutor_media:/media -v "$PWD/backups":/b \
#                     alpine sh -c 'tar xzf /b/media-YYYY-MM-DD.tar.gz -C /'
#
# Run it by hand before updates, or schedule it (cron/launchd). Keeps the last
# 14 of each. A backup that lives on the same disk as the database protects
# against `docker compose down -v` and bad updates, NOT against disk death —
# point TARGET at an external drive or cloud-synced folder for that.
set -euo pipefail

TARGET="${1:-$(dirname "$0")/../backups}"
mkdir -p "$TARGET"
STAMP="$(date +%F)"

echo "→ dumping postgres…"
docker exec guitar_tutor-postgres-1 pg_dump -U guitar -d guitar --clean --if-exists \
  | gzip > "$TARGET/guitar-tutor-$STAMP.sql.gz"

echo "→ archiving media volume…"
docker run --rm -v guitar_tutor_media:/media -v "$(cd "$TARGET" && pwd)":/backup \
  alpine tar czf "/backup/media-$STAMP.tar.gz" /media 2>/dev/null

# Rotation: keep the newest 14 of each.
ls -1t "$TARGET"/guitar-tutor-*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm --
ls -1t "$TARGET"/media-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm --

echo "✓ backup complete:"
ls -lh "$TARGET" | tail -n +2
