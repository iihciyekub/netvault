# NetVault Full Migration Runbook

This runbook moves the complete NetVault service to another VPS while keeping the PostgreSQL database and PDF object store consistent. Treat it as a controlled change: name an operator, record every checkpoint, and keep the old server intact until acceptance is complete.

For the verified production architecture and audit findings, see `docs/operations-audit-2026-07-13.md`.

## State That Must Move

| State | Current location | Transfer method |
| --- | --- | --- |
| PostgreSQL | Docker volume `netvault_postgres-data` | `pg_dump -Fc` / `pg_restore`; never copy a live raw volume |
| PDF objects | `/root/iiaide/netvault/storage` | `rsync -a` via a backup snapshot |
| Production secrets | `/root/iiaide/netvault/.env` | Separate encrypted transfer, mode `0600` |
| Application version | Git commit recorded in backup manifest | Deploy the same commit first |
| Proxy/network setup | Traefik plus external network `iiaide-data` | Recreate or integrate on target |

Current scale at the 2026-07-13 audit was about 97 MB of PostgreSQL data and 26 GB of storage. Measure again before scheduling. Most transfer time will be storage, not PostgreSQL.

## Migration Guarantees and Downtime

The safest procedure has two phases:

1. Pre-seed the target from a recent backup while the old service remains live.
2. Freeze writes, create a final backup, synchronize the final delta, restore, validate, and switch traffic.

The write freeze is mandatory for an exact database/storage pairing. Stopping the old `server` container is the currently supported freeze mechanism, so the service will be unavailable during the final cutover. Do not allow uploads to either server until the target passes validation.

## 1. Plan and Record

Before the change:

- Confirm target hostname/IP, disk capacity, DNS/Cloudflare access, Docker, Compose, Traefik, and firewall rules.
- Ensure target free space is at least two times current storage plus database and Docker image headroom.
- Choose a maintenance window and lower DNS TTL in advance if DNS will change.
- Confirm SSH public-key access to both hosts from two authorized operator sessions.
- Record the deployed Git commit and current row/file counts.
- Confirm an off-host copy of a recent backup exists.
- Decide the rollback deadline and do not delete the old server before it.

Source inventory:

```bash
ssh root@OLD_SERVER
cd /root/iiaide/netvault
git rev-parse HEAD
docker compose -f docker-compose.iiaide.yml --env-file .env ps
df -h
du -sh storage /root/netvault-data-backups
```

Record database counts:

```bash
docker compose -f docker-compose.iiaide.yml --env-file .env exec -T db \
  sh -c 'psql -X -At -F "|" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "select (select count(*) from users), (select count(*) from pdfs),
          (select count(*) from upload_records), (select count(*) from download_records);"'
```

## 2. Prepare the Target

On the new VPS:

```bash
mkdir -p /root/iiaide
git clone git@github.com:iihciyekub/netvault.git /root/iiaide/netvault
cd /root/iiaide/netvault
git checkout SOURCE_COMMIT
docker network inspect iiaide-data >/dev/null 2>&1 || docker network create iiaide-data
```

Transfer `.env` through an encrypted, operator-approved channel. Do not commit it or place it in the ordinary backup tree:

```bash
install -m 600 /secure/incoming/netvault.env /root/iiaide/netvault/.env
```

Review only variable names, not secret values:

```bash
sed -nE 's/^([A-Za-z_][A-Za-z0-9_]*)=.*/\1=<redacted>/p' .env
```

At minimum it must define `APP_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `NETVAULT_SECRET_KEY`, and `NETVAULT_BOOTSTRAP_ADMIN_PASSWORD`. Retaining the existing secret key preserves active token/session compatibility; rotating it intentionally invalidates them.

Do not start the public application yet. Start only PostgreSQL:

```bash
docker compose -f docker-compose.iiaide.yml --env-file .env up -d db
docker compose -f docker-compose.iiaide.yml --env-file .env ps
```

Ensure the target firewall exposes only required services such as SSH and HTTP/HTTPS. PostgreSQL port 5432 must not be public.

## 3. Optional Pre-seed

For a large object store, transfer the newest completed backup before the maintenance window. Copy one snapshot; `-H` preserves any hard links if the transfer scope contains multiple incremental snapshots.

```bash
rsync -aH --numeric-ids --info=progress2 \
  root@OLD_SERVER:/root/netvault-data-backups/BACKUP_ID/ \
  /root/netvault-restore/BACKUP_ID/
```

Verify the dump checksum on the target:

```bash
cd /root/netvault-restore/BACKUP_ID
expected="$(awk -F= '$1 == "database_sha256" {print $2}' manifest.txt)"
actual="$(sha256sum netvault.pg.dump | awk '{print $1}')"
test -n "$expected" && test "$expected" = "$actual"
```

This pre-seed reduces downtime but is not the final consistent snapshot.

## 4. Freeze Writes and Create the Final Backup

At the start of the maintenance window, stop the source application while leaving PostgreSQL running:

```bash
ssh root@OLD_SERVER
cd /root/iiaide/netvault
docker compose -f docker-compose.iiaide.yml --env-file .env stop server
```

Confirm the public readiness endpoint is unavailable, then create the final snapshot:

```bash
NETVAULT_BACKUP_KEEP_DAYS=30 ./scripts/backup-server.sh
```

Record the final `BACKUP_ID` printed by the script. Validate it:

```bash
BACKUP_ID=YYYYMMDDTHHMMSSZ
BACKUP_DIR="/root/netvault-data-backups/$BACKUP_ID"
test -s "$BACKUP_DIR/netvault.pg.dump"
test -s "$BACKUP_DIR/manifest.txt"
test -d "$BACKUP_DIR/storage/objects"
expected="$(awk -F= '$1 == "database_sha256" {print $2}' "$BACKUP_DIR/manifest.txt")"
actual="$(sha256sum "$BACKUP_DIR/netvault.pg.dump" | awk '{print $1}')"
test -n "$expected" && test "$expected" = "$actual"
./scripts/verify-storage.sh
```

Keep the source application stopped from this point until cutover succeeds or rollback is declared.

## 5. Transfer the Final Snapshot

From the target, synchronize the final backup. Re-running `rsync` transfers only changed data when the destination was pre-seeded:

```bash
mkdir -p "/root/netvault-restore/$BACKUP_ID"
rsync -aH --numeric-ids --delete --info=progress2 \
  "root@OLD_SERVER:/root/netvault-data-backups/$BACKUP_ID/" \
  "/root/netvault-restore/$BACKUP_ID/"
```

Repeat the checksum verification from step 3 on the target. Also retain the source and target `manifest.txt` files in the change record.

## 6. Restore on the Target

The restore script is destructive to the target database and storage. Verify the hostname and backup path before adding `--force`:

```bash
hostname
cd /root/iiaide/netvault
NETVAULT_REPO_DIR=/root/iiaide/netvault \
  ./scripts/restore-server.sh "/root/netvault-restore/$BACKUP_ID" --force
```

The script stops the target application, verifies the database checksum, restores PostgreSQL with `--clean --if-exists`, replaces storage with `rsync --delete`, and starts the application.

Inspect startup:

```bash
docker compose -f docker-compose.iiaide.yml --env-file .env ps
docker compose -f docker-compose.iiaide.yml --env-file .env logs --tail=200 server db
```

## 7. Validate Before Switching Traffic

If public DNS still points at the old host, test the target locally or with a temporary host override. Required acceptance checks:

1. Both containers are healthy.
2. `/health` and `/ready` succeed.
3. Database row counts match the source freeze-time counts.
4. `scripts/verify-storage.sh` reports zero missing/orphaned objects.
5. Storage bytes and object count match the final manifest.
6. An administrator can log in, search, download and open a known PDF.
7. A controlled upload and download works on the target; remove the test item if appropriate.
8. Traefik routes HTTPS correctly and certificates are valid.

Commands:

```bash
curl -fsS https://TARGET_HOST/nv/health
curl -fsS https://TARGET_HOST/nv/ready
cd /root/iiaide/netvault
./scripts/verify-storage.sh
find storage/objects -type f -name '*.pdf' | wc -l
find storage/objects -type f -name '*.pdf' -printf '%s\n' | awk '{sum += $1} END {printf "%.0f\n", sum}'
```

Compare the last two values with `object_count` and `object_bytes` in the final manifest.

## 8. Switch Traffic and Observe

Update DNS/Cloudflare or the upstream proxy to the target. Keep the source `server` stopped to prevent split-brain writes. After the switch:

```bash
curl -fsS https://iiaide.com/nv/health
curl -fsS https://iiaide.com/nv/ready
```

Monitor application/database logs, HTTP error rate, disk space, uploads, and downloads closely for at least one full business cycle. Install and test the maintenance cron and log rotation on the new server:

```bash
install -m 644 deploy/netvault-maintenance.cron /etc/cron.d/netvault-maintenance
install -m 644 deploy/netvault-maintenance.logrotate /etc/logrotate.d/netvault-maintenance
./scripts/backup-server.sh
```

Copy the first target backup off-host and record its backup ID.

## Rollback

Rollback is safe only while no accepted writes exist solely on the new server. If validation fails before reopening writes:

1. Stop the target `server` container.
2. Restore DNS/proxy routing to the old VPS if it was changed.
3. Start the old application:

```bash
ssh root@OLD_SERVER
cd /root/iiaide/netvault
docker compose -f docker-compose.iiaide.yml --env-file .env up -d server
```

4. Verify old `/health`, `/ready`, login, search, and download.
5. Record the failure and retain target logs and the final snapshot for diagnosis.

If users have already written data to the new server, do not simply point traffic back: that would lose those writes. Freeze both sides and make an explicit reconciliation plan first.

## Decommissioning

Do not delete the old repository, `.env`, Docker volume, storage, or backups during the rollback window. After formal acceptance:

- retain the final source snapshot according to the data-retention policy;
- verify at least one successful off-host target backup;
- revoke obsolete SSH keys and credentials;
- remove old DNS records and server access;
- securely destroy the old VPS only after written approval;
- complete the change record template in the operations audit document.
