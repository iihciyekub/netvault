#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${NETVAULT_REPO_DIR:-/root/iiaide/netvault}"
BACKUP_ROOT="${NETVAULT_BACKUP_ROOT:-/root/netvault-data-backups}"
KEEP_DAYS="${NETVAULT_BACKUP_KEEP_DAYS:-14}"
COMPOSE_FILE="${NETVAULT_COMPOSE_FILE:-docker-compose.iiaide.yml}"

command -v docker >/dev/null
command -v rsync >/dev/null
mkdir -p "$BACKUP_ROOT"
exec 9>"$BACKUP_ROOT/.backup.lock"
flock -n 9 || { echo "A NetVault backup is already running." >&2; exit 1; }

cd "$REPO_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="$BACKUP_ROOT/$stamp"
previous="$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort | tail -1 || true)"
mkdir -p "$destination/storage"
link_args=()
if [[ -n "$previous" && -d "$BACKUP_ROOT/$previous/storage" ]]; then
  link_args=(--link-dest="$BACKUP_ROOT/$previous/storage")
fi

dump_tmp="$destination/netvault.pg.dump.tmp"
docker compose -f "$COMPOSE_FILE" exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$dump_tmp"
mv "$dump_tmp" "$destination/netvault.pg.dump"
rsync -a --delete "${link_args[@]}" storage/ "$destination/storage/"

object_count="$(find "$destination/storage/objects" -type f -name '*.pdf' | wc -l | tr -d ' ')"
object_bytes="$(find "$destination/storage/objects" -type f -name '*.pdf' -printf '%s\n' | awk '{sum += $1} END {printf "%.0f", sum}')"
{
  printf 'created_utc=%s\n' "$stamp"
  printf 'git_commit=%s\n' "$(git rev-parse HEAD)"
  printf 'object_count=%s\n' "$object_count"
  printf 'object_bytes=%s\n' "$object_bytes"
  printf 'database_sha256=%s\n' "$(sha256sum "$destination/netvault.pg.dump" | awk '{print $1}')"
} > "$destination/manifest.txt"
chmod -R go-rwx "$destination"

find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime "+$KEEP_DAYS" -exec rm -rf -- {} +
echo "NetVault backup completed: $destination"
