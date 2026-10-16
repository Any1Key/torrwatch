#!/bin/sh
set -eu

# Safe project maintenance: backups are created inside the mounted data volume
# and only explicitly named backup-* directories are eligible for pruning.
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
data_dir="$project_dir/data"
keep_count=${TORRWATCH_BACKUP_KEEP:-7}

case "$keep_count" in
  ''|*[!0-9]*) echo "TORRWATCH_BACKUP_KEEP must be a positive integer" >&2; exit 2 ;;
esac
[ "$keep_count" -ge 1 ] || { echo "TORRWATCH_BACKUP_KEEP must be at least 1" >&2; exit 2; }

stamp=$(date -u +%Y%m%d-%H%M%S)
backup_dir="/data/backup-$stamp"
docker compose -f "$project_dir/docker-compose.yml" exec -T web torrwatch backup "$backup_dir"

backup_count=$(find "$data_dir" -mindepth 1 -maxdepth 1 -type d -name 'backup-*' -print 2>/dev/null | wc -l | tr -d ' ')
if [ "$backup_count" -gt "$keep_count" ]; then
  remove_count=$((backup_count - keep_count))
  find "$data_dir" -mindepth 1 -maxdepth 1 -type d -name 'backup-*' -printf '%T@ %p\n' \
    | sort -n \
    | head -n "$remove_count" \
    | cut -d' ' -f2- \
    | while IFS= read -r old_backup; do
        [ -n "$old_backup" ] && rm -rf -- "$old_backup"
      done
fi

# Remove only dangling images carrying the TorrWatch label; running images are
# never targeted by this command.
docker image prune -f --filter 'label=org.opencontainers.image.title=torrwatch'
echo "PASS maintenance completed; retained backups: $keep_count"
