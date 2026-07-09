# NetVault Admin Guide

This guide covers the deployed NetVault service at:

```text
https://iiaide.com/nv
```

## Architecture

```text
User CLI or Web UI
  -> https://iiaide.com/nv
    -> Traefik on VPS
      -> netvault-server FastAPI container
        -> PostgreSQL container for metadata
        -> storage/ directory for PDF objects
```

The CLI and browser never connect to PostgreSQL directly. All user and admin
actions go through the FastAPI server.

## VPS Location

Project directory:

```text
/root/iiaide/netvault
```

Important files:

```text
/root/iiaide/netvault/.env
/root/iiaide/netvault/docker-compose.iiaide.yml
/root/iiaide/netvault/storage/
```

Do not commit `.env`; it contains production secrets.

## Docker Services

Check status:

```bash
ssh root@72.62.255.34
cd /root/iiaide/netvault
docker compose -f docker-compose.iiaide.yml --env-file .env ps
```

Expected services:

```text
netvault-db-1       postgres:16
netvault-server-1   netvault-server
```

Restart server:

```bash
docker compose -f docker-compose.iiaide.yml --env-file .env up --build -d server
```

Restart all:

```bash
docker compose -f docker-compose.iiaide.yml --env-file .env up --build -d
```

Logs:

```bash
docker compose -f docker-compose.iiaide.yml --env-file .env logs -f server
docker compose -f docker-compose.iiaide.yml --env-file .env logs -f db
```

Health check:

```bash
curl -fsS https://iiaide.com/nv/health
```

Web UI:

```text
https://iiaide.com/nv/web
```

## Environment

Required `.env` keys:

```text
APP_HOST=iiaide.com
NETVAULT_BASE_PATH=/nv
POSTGRES_DB=netvault
POSTGRES_USER=netvault
POSTGRES_PASSWORD=...
NETVAULT_SECRET_KEY=...
NETVAULT_BOOTSTRAP_ADMIN=admin
NETVAULT_BOOTSTRAP_ADMIN_PASSWORD=...
NETVAULT_CROSSREF_MAILTO=admin@iiaide.com
```

`NETVAULT_CROSSREF_MAILTO` is used for Crossref polite API access.

## PostgreSQL

PostgreSQL runs inside Docker:

```text
container: netvault-db-1
database:  netvault
user:      netvault
```

Data volume:

```text
Docker volume: netvault_postgres-data
Mountpoint: /var/lib/docker/volumes/netvault_postgres-data/_data
```

Enter psql:

```bash
docker exec -it netvault-db-1 psql -U netvault -d netvault
```

Useful psql commands:

```sql
\dt
\d users
\d pdfs
\d upload_records
\d download_records
```

Current schema:

```text
users             accounts, roles, password hashes
pdfs              DOI, Crossref metadata, sha256, storage path, soft-delete state
upload_records    append-only upload events
download_records  append-only download events
```

Useful queries:

```sql
select id, username, role, is_active, created_at from users order by id;

select id, doi, title, published_year, crossref_status, is_deleted
from pdfs
order by uploaded_at desc;

select p.doi, count(*) as downloads
from download_records d
join pdfs p on p.id = d.pdf_id
group by p.doi
order by downloads desc;
```

## PDF Storage

PDF files are not stored in PostgreSQL. They are stored on disk:

```text
/root/iiaide/netvault/storage/objects/<sha-prefix>/<sha256>.pdf
```

The database stores the relative `storage_path`.

Check size:

```bash
du -sh /root/iiaide/netvault/storage
```

## User Management

Login as an admin locally:

```bash
nv login https://iiaide.com/nv
```

Install the server/admin package in an admin environment:

```bash
python -m pip install -e packages/netvault-server
```

Create user:

```bash
netvault-admin create-user alice
```

Create admin:

```bash
netvault-admin create-user alice --admin
```

Reset password:

```bash
netvault-admin reset-password alice
```

Deactivate user:

```bash
netvault-admin deactivate-user alice
```

Delete PDF by DOI:

```bash
netvault-admin delete-pdf 10.1016/j.ijpe.2018.04.006
```

Deletion is a soft delete. The row remains in PostgreSQL and the object file
currently remains in storage.

## Updates

Code repository:

```text
https://github.com/iihciyekub/netvault
```

Update server from local working tree:

```bash
rsync -az --delete \
  --exclude '.env' \
  --exclude '.venv/' \
  --exclude '.pytest_cache/' \
  --exclude '__pycache__/' \
  --exclude 'storage/' \
  --exclude '.DS_Store' \
  --exclude '*.pdf' \
  ./ root@72.62.255.34:/root/iiaide/netvault/

ssh root@72.62.255.34 'cd /root/iiaide/netvault && docker compose -f docker-compose.iiaide.yml --env-file .env up --build -d server'
```

Users update their CLI with:

```bash
nv update
```

## Known Admin Gaps

- No Alembic migration system yet; startup adds missing columns directly.
- No one-command backup/restore script yet.
- Soft-deleted PDF objects are not garbage-collected yet.
