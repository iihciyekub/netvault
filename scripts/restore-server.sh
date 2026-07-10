#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 2 || "$2" != "--force" ]]; then
  echo "Usage: $0 /absolute/path/to/backup --force" >&2
  exit 2
fi

BACKUP_DIR="$(realpath "$1")"
REPO_DIR="${NETVAULT_REPO_DIR:-/root/iiaide/netvault}"
COMPOSE_FILE="${NETVAULT_COMPOSE_FILE:-docker-compose.iiaide.yml}"
[[ -s "$BACKUP_DIR/netvault.pg.dump" && -d "$BACKUP_DIR/storage/objects" ]] || {
  echo "Backup is incomplete: $BACKUP_DIR" >&2
  exit 1
}
expected_sha="$(awk -F= '$1 == "database_sha256" {print $2}' "$BACKUP_DIR/manifest.txt")"
actual_sha="$(sha256sum "$BACKUP_DIR/netvault.pg.dump" | awk '{print $1}')"
[[ -n "$expected_sha" && "$expected_sha" == "$actual_sha" ]] || {
  echo "Database dump checksum verification failed." >&2
  exit 1
}

cd "$REPO_DIR"
docker compose -f "$COMPOSE_FILE" stop server
trap 'docker compose -f "$COMPOSE_FILE" up -d server >/dev/null 2>&1 || true' EXIT
docker compose -f "$COMPOSE_FILE" exec -T db sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges' \
  < "$BACKUP_DIR/netvault.pg.dump"
rsync -a --delete "$BACKUP_DIR/storage/" storage/
docker compose -f "$COMPOSE_FILE" up -d server
trap - EXIT
echo "NetVault restored from $BACKUP_DIR"
