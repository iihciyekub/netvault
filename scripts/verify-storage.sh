#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${NETVAULT_REPO_DIR:-/root/iiaide/netvault}"
STORAGE_ROOT="${NETVAULT_STORAGE_ROOT:-$REPO_DIR/storage}"
DB_CONTAINER="${NETVAULT_DB_CONTAINER:-netvault-db-1}"
MODE="${1:-report}"

objects() {
  find "$STORAGE_ROOT/objects" -type f -name '*.pdf' -printf '%f\n' | sed 's/\.pdf$//' | sort
}

dbhashes() {
  docker exec "$DB_CONTAINER" sh -c \
    'psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT sha256 FROM pdfs"' | sort
}

missing_count="$(comm -13 <(objects) <(dbhashes) | wc -l | tr -d ' ')"
orphan_count="$(comm -23 <(objects) <(dbhashes) | wc -l | tr -d ' ')"
tmp_count="$(find "$STORAGE_ROOT/tmp" -type f -mmin +60 | wc -l | tr -d ' ')"
printf 'database_without_object=%s\nobjects_without_database=%s\nstale_temp_files=%s\n' \
  "$missing_count" "$orphan_count" "$tmp_count"

if [[ "$MODE" == "--quarantine-orphans" && "$orphan_count" -gt 0 ]]; then
  quarantine="$STORAGE_ROOT/quarantine/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$quarantine"
  moved=0
  while read -r sha; do
    source="$STORAGE_ROOT/objects/${sha:0:2}/$sha.pdf"
    if [[ -f "$source" ]] && [[ -n "$(find "$source" -mmin +360 -print)" ]]; then
      mv "$source" "$quarantine/"
      moved=$((moved + 1))
    fi
  done < <(comm -23 <(objects) <(dbhashes))
  chmod -R go-rwx "$quarantine"
  if [[ "$moved" -gt 0 ]]; then
    echo "$moved orphan objects older than six hours quarantined at $quarantine"
  else
    rmdir "$quarantine"
    echo "No orphan objects were old enough to quarantine."
  fi
fi

[[ "$missing_count" -eq 0 ]]
