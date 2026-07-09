# NetVault Migration Guide

This guide explains how to move NetVault from one VPS to another.

## What Must Be Moved

NetVault has three stateful pieces:

```text
1. PostgreSQL database
2. PDF storage directory
3. .env production configuration
```

Current deployed paths:

```text
project:  /root/iiaide/netvault
env:      /root/iiaide/netvault/.env
storage:  /root/iiaide/netvault/storage
db volume: netvault_postgres-data
```

The PostgreSQL volume mountpoint is:

```text
/var/lib/docker/volumes/netvault_postgres-data/_data
```

Prefer `pg_dump` over copying the raw volume.

## Backup On Old Server

SSH into the old VPS:

```bash
ssh root@72.62.255.34
cd /root/iiaide/netvault
```

Create a backup directory:

```bash
mkdir -p /root/netvault-backups
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/root/netvault-backups/$STAMP"
mkdir -p "$BACKUP_DIR"
```

Dump PostgreSQL:

```bash
docker exec netvault-db-1 pg_dump -U netvault -d netvault -Fc > "$BACKUP_DIR/netvault.pg.dump"
```

Archive PDF storage:

```bash
tar -czf "$BACKUP_DIR/netvault-storage.tar.gz" storage
```

Copy production config:

```bash
cp .env "$BACKUP_DIR/netvault.env"
chmod 600 "$BACKUP_DIR/netvault.env"
```

Optional checksum:

```bash
cd "$BACKUP_DIR"
sha256sum netvault.pg.dump netvault-storage.tar.gz netvault.env > SHA256SUMS
```

## Transfer To New Server

From your local machine or old server:

```bash
scp -r root@OLD_SERVER:/root/netvault-backups/YYYYMMDD-HHMMSS root@NEW_SERVER:/root/
```

Or use `rsync`:

```bash
rsync -az root@OLD_SERVER:/root/netvault-backups/YYYYMMDD-HHMMSS/ root@NEW_SERVER:/root/netvault-restore/
```

## Prepare New Server

Install Docker and Docker Compose on the new VPS.

Clone the repo:

```bash
ssh root@NEW_SERVER
mkdir -p /root/iiaide
git clone https://github.com/iihciyekub/netvault.git /root/iiaide/netvault
cd /root/iiaide/netvault
```

Restore `.env`:

```bash
cp /root/netvault-restore/netvault.env .env
chmod 600 .env
```

Adjust `.env` if the hostname or base path changes:

```text
APP_HOST=iiaide.com
NETVAULT_BASE_PATH=/nv
```

Restore storage:

```bash
tar -xzf /root/netvault-restore/netvault-storage.tar.gz
```

Start PostgreSQL:

```bash
docker compose -f docker-compose.iiaide.yml --env-file .env up -d db
```

Restore database:

```bash
docker cp /root/netvault-restore/netvault.pg.dump netvault-db-1:/tmp/netvault.pg.dump
docker exec netvault-db-1 pg_restore -U netvault -d netvault --clean --if-exists /tmp/netvault.pg.dump
```

Start the full service:

```bash
docker compose -f docker-compose.iiaide.yml --env-file .env up --build -d
```

## DNS And Reverse Proxy

If keeping the same URL:

```text
https://iiaide.com/nv
```

point DNS or Cloudflare to the new VPS.

The current production compose expects an external Docker network:

```text
iiaide-data
```

If the new VPS already has Traefik using this network, keep it. Otherwise create it:

```bash
docker network create iiaide-data
```

You also need Traefik configured to watch Docker labels and serve HTTP/HTTPS.

## Verify New Server

Health:

```bash
curl -fsS https://iiaide.com/nv/health
```

Login:

```bash
nv login https://iiaide.com/nv
```

List:

```bash
nv list
```

Database counts:

```bash
docker exec netvault-db-1 psql -U netvault -d netvault -c \
  "select (select count(*) from users) as users, (select count(*) from pdfs) as pdfs, (select count(*) from upload_records) as uploads, (select count(*) from download_records) as downloads;"
```

Storage size:

```bash
du -sh /root/iiaide/netvault/storage
```

## Rollback

If the new server fails:

1. Point DNS back to the old VPS.
2. Keep the old containers running until the new server is verified.
3. Do not delete old `/root/iiaide/netvault` or Docker volumes until the new server is fully tested.

## Future Improvement

This should eventually be wrapped in scripts:

```text
scripts/backup-server.sh
scripts/restore-server.sh
```

For now, use the commands in this document.
