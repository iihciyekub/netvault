# NetVault SSH and PostgreSQL Operations Audit

Audit date: 2026-07-13 (Asia/Hong_Kong)
Scope: production connection path, PostgreSQL state, backups, and migration readiness
Method: repository inspection plus read-only checks over SSH. No production data or configuration was changed.

## Executive Summary

NetVault does **not** connect from a developer computer to PostgreSQL through an SSH tunnel. SSH is the administrative path to the VPS. On the VPS, Docker Compose starts the application and PostgreSQL as separate containers. The application connects directly to PostgreSQL over a private Docker network:

```text
Administrator workstation
        |
        | SSH, TCP 22, public-key authentication
        v
Production VPS (72.62.255.34, hostname srv1569006)
        |
        +-- netvault-server-1
        |      |
        |      | postgresql+psycopg://...@db:5432/netvault
        |      v
        +-- netvault-db-1 (PostgreSQL 16)
               |
               v
        netvault_postgres-data Docker volume
```

The PostgreSQL port is exposed only inside Docker (`5432/tcp`) and has no host-published port. This is the correct security posture: database administration should be performed through SSH and `docker compose exec`, not by opening port 5432 to the Internet.

## Verified Production State

The following facts were observed at 2026-07-13 03:05 UTC and are a point-in-time record:

| Item | Observed value |
| --- | --- |
| SSH endpoint | `root@72.62.255.34` |
| SSH authentication | Public key (ED25519); no password was used |
| VPS hostname | `srv1569006` |
| Repository | `/root/iiaide/netvault` |
| Deployed Git commit | `5f3e5ca` |
| Compose file | `docker-compose.iiaide.yml` |
| App container | `netvault-server-1`, healthy |
| Database container | `netvault-db-1`, healthy |
| PostgreSQL | 16.14, 64-bit |
| Database / role | `netvault` / `netvault` |
| Database size | approximately 97 MB |
| PDF storage | `/root/iiaide/netvault/storage`, approximately 26 GB |
| PostgreSQL volume | `netvault_postgres-data` |
| Volume mountpoint | `/var/lib/docker/volumes/netvault_postgres-data/_data` |
| Application network | `netvault_netvault-internal` and `iiaide-data` |
| Database network | `netvault_netvault-internal` only |
| Rows: users / PDFs / uploads / downloads | 2 / 29,203 / 29,211 / 107 |

Counts and sizes will naturally change. Re-run the commands below immediately before a migration.

## How the Application Connects

`packages/netvault-server/src/netvault_server/server/config.py` loads `NETVAULT_DATABASE_URL`. In production, `docker-compose.iiaide.yml` constructs it from the `.env` values:

```text
postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
```

Important details:

- `db` is Docker Compose service discovery, not a public DNS name.
- The `server` and `db` services share `netvault_netvault-internal`.
- PostgreSQL has no `ports:` mapping, so remote clients cannot connect directly to the VPS database port.
- SQLAlchemy enables connection health checks, a 30-minute recycle, a configurable pool, and a per-connection statement timeout.
- Development and tests may use SQLite when `NETVAULT_DATABASE_URL` is absent or overridden. That is not the production database.
- Database tables are initialized and upgraded by application code in `migrations.py`; there is currently no Alembic revision history.

The production `.env` file is the source of credentials and other secrets. It must stay outside Git, use mode `0600`, and be transferred separately during disaster recovery. Never paste its values into tickets, commits, chat logs, or migration manifests.

## Safe Operator Access

Connect to the server:

```bash
ssh root@72.62.255.34
cd /root/iiaide/netvault
```

Check services without revealing environment values:

```bash
docker compose -f docker-compose.iiaide.yml --env-file .env ps
docker compose -f docker-compose.iiaide.yml --env-file .env logs --tail=100 server db
```

Open `psql` inside the database container:

```bash
docker compose -f docker-compose.iiaide.yml --env-file .env exec db \
  sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Run a read-only inventory query:

```bash
docker compose -f docker-compose.iiaide.yml --env-file .env exec -T db \
  sh -c 'psql -X -At -F "|" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "select (select count(*) from users), (select count(*) from pdfs),
          (select count(*) from upload_records), (select count(*) from download_records);"'
```

If an external PostgreSQL client is ever required, use a temporary SSH tunnel bound only to localhost. This requires PostgreSQL to be deliberately published to VPS loopback first; the current Compose configuration does not publish it. Do not add a public `0.0.0.0:5432:5432` mapping.

## Backup System Observed

`/etc/cron.d/netvault-maintenance` runs:

- daily backup at `03:17` server local time;
- storage verification every Sunday at `04:45` server local time.

Backups are stored in `/root/netvault-data-backups`. At audit time there were recent successful backups through `20260712T191701Z`, occupying approximately 95 GB. The latest verification logged:

```text
database_without_object=0
objects_without_database=0
stale_temp_files=0
```

`scripts/backup-server.sh` creates a PostgreSQL custom-format dump and an incremental hard-linked storage snapshot, then records database SHA-256, object count, object bytes, and Git commit in `manifest.txt`. Retention defaults to 14 days.

### Backup limitations

1. Backups reside on the same VPS as the live service. A VPS or disk loss can destroy both production and backups. Replicate completed backup directories to a separate host or object store.
2. `.env` is intentionally not included. Maintain an encrypted, access-controlled copy separately.
3. The daily script dumps PostgreSQL before it synchronizes files while uploads may continue. It is suitable for routine recovery, but a zero-drift migration requires a write freeze and a final backup after the freeze.
4. A backup is not proven until a restore drill succeeds. Schedule a quarterly restore into an isolated Compose project and record the result.

## Routine Health Checklist

Run at least monthly and before every release affecting storage or models:

```bash
cd /root/iiaide/netvault
docker compose -f docker-compose.iiaide.yml --env-file .env ps
curl -fsS https://iiaide.com/nv/health
curl -fsS https://iiaide.com/nv/ready
sudo tail -50 /var/log/netvault-maintenance.log
sudo du -sh /root/netvault-data-backups storage
sudo ./scripts/verify-storage.sh
```

Also verify free disk space with `df -h` and confirm the newest backup contains `manifest.txt`, `netvault.pg.dump`, and `storage/objects`.

## Risks and Recommended Actions

| Priority | Risk | Action |
| --- | --- | --- |
| P0 | Backups and production are on the same VPS | Add encrypted off-host replication and alert on failure |
| P0 | A live backup can span concurrent uploads | Use the write-freeze procedure in `docs/migration.md` for a full migration |
| P1 | Restore success is not automatically tested | Perform and record an isolated restore drill quarterly |
| P1 | Production administration uses the root SSH account | Create a named sudo operator, restrict root login, and maintain an emergency key |
| P1 | SSH endpoint is recorded as a raw IP | Add a stable SSH host alias or operations DNS record and document key rotation |
| P2 | Application-managed migrations have no revision ledger | Consider adopting Alembic before schema evolution becomes complex |
| P2 | Capacity alerts are manual | Alert on disk usage, backup age, container health, and storage verification failures |

## Change Record Template

Append a short record after every database, storage, backup, or migration change:

```text
Date/time and timezone:
Operator:
Reason / ticket:
Source Git commit:
Target host and Compose project:
Pre-change backup ID and checksum verification:
Database and storage counts before:
Commands or script version used:
Database and storage counts after:
Health/readiness/smoke-test result:
Rollback required (yes/no) and result:
Notes:
```

The end-to-end migration and rollback runbook is in `docs/migration.md`.
