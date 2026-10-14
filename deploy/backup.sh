#!/usr/bin/env bash
# Nightly Postgres dump, keeping the last 7. Cron example (as the deploy user):
#   15 3 * * * cd $HOME/loops && ./deploy/backup.sh >> $HOME/loops-backup.log 2>&1
set -euo pipefail
DEST="${BACKUP_DIR:-$HOME/loops-backups}"
mkdir -p "$DEST" && chmod 700 "$DEST"
file="$DEST/loops-$(date -u +%Y%m%d-%H%M%S).sql.gz"
docker compose exec -T db pg_dump -U loops loops | gzip > "$file"
chmod 600 "$file"
ls -1t "$DEST"/loops-*.sql.gz | tail -n +8 | xargs -r rm --
echo "backup: $file"
