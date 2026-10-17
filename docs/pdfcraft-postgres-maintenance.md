# NetVault PDFCraft Compression and PostgreSQL Maintenance Notes

Verified: 2026-10-16. This document is persistent project context for future maintenance.

## Scope and terminology

These scripts perform **second-pass optimization of PDF files**, not PostgreSQL database-page, SQL-dump, or WAL compression. PDF content lives on disk; PostgreSQL stores metadata, canonical SHA-256, object sizes and paths, and PDFCraft completion markers.

## Verified deployment (read-only SSH inspection)

- Server: `ssh root@72.62.255.34` (hostname `srv1569006`).
- Remote project: `/root/iiaide/netvault`.
- Local project: `/Users/dev.ii/Documents/ii-project/netvault`.
- PostgreSQL: Docker container `netvault-db-1`, image `postgres:16`; application container `netvault-server-1`.
- Production PDF storage: `/root/iiaide/netvault/storage/objects/<sha-prefix>/<sha256>.pdf`.
- Confirmed present on the remote server:
  - `scripts/compress_pdfs_parallel.py`
  - `scripts/compress_pdfs_unprocessed.py`
  - `scripts/retry_pdfcraft_failures.py`
- No matching compression entry appeared in root's crontab during the 2026-10-16 inspection. This **does not** rule out systemd, other users' cron, or externally scheduled jobs; verify before operating.

## What the scripts do

- `scripts/compress_pdfs_parallel.py`: parallel PDFCraft optimization of eligible PDFs, with bounded worker count and resumable state.
- `scripts/compress_pdfs_unprocessed.py`: original single-worker scan of not-yet-processed PDF objects.
- `scripts/retry_pdfcraft_failures.py`: controlled retry/repair path for failed PDFCraft items, including special DOI/signature checks.
- `scripts/prune_failed_pdfs.py` and `scripts/prune_unresolved_doi_pdfs.py`: potentially destructive cleanup; review carefully and take verified backups before running.
- `scripts/verify-postgres-contract.py`: PostgreSQL contract verification (not a compression script).

Compression recipe in the checked scripts: PDFCraft `--steps optimize --ppi 150 --quality 75`. Keep the original when the final candidate is not strictly smaller; verify content structure, DOI and SHA-256. Script updates the `pdfs` metadata and PDFCraft marker columns transactionally. Running a script can replace physical PDF objects and update production database rows: **never run as a routine diagnostic**.

Shared lock: `/root/iiaide/netvault/storage/tmp/pdfcraft-batch.lock`.
Parallel/initial scan state: `storage/tmp/pdfcraft-unprocessed-state.json`, errors `storage/tmp/pdfcraft-unprocessed-errors.jsonl`.
Retry state: `storage/tmp/pdfcraft-repair-state.json`, errors `storage/tmp/pdfcraft-repair-errors.jsonl`.
Markers: `pdfcraft_processed_at`, `pdfcraft_processed_sha256`, `pdfcraft_ppi`, `pdfcraft_quality`.

## Future maintenance workflow

1. Read this note, `docs/admin-guide.md`, `docs/operations-audit-2026-07-13.md`, and `AGENTS.md`. Inspect the current Git status and differences; do not overwrite uncommitted work.
2. Check live service health, running processes, locks, disk space, snapshot availability, and current database schema before planning compression/repair.
3. Use SSH for administration; PostgreSQL is internal to Docker, not published directly. For read-only SQL inspection, use `docker exec -it netvault-db-1 psql -U netvault -d netvault`.
4. Backup and verify database **and PDF object storage together** before any data mutation; distinguish a SQL backup from PDF files.
5. Investigate whether a compression job is running or scheduled before any manual attempt. Avoid concurrent workers and protect the shared lock and resume markers.
6. Review current script arguments and operational behavior in source before execution. Start with a limited, explicitly approved pilot and verify file/DB consistency; do not assume the scripts offer `--dry-run`.
7. Do not disclose or commit `.env`, database credentials, SSH private keys, or other secrets.

## Evidence and limitations

On 2026-10-16, SSH successfully read remote directory/script metadata and observed Docker containers. Script behavior and state paths were inspected from the local project source. No compression execution, live SQL mutation, nor database marker count/status validation was performed. The scripts are present locally and remotely, but their current content hashes were **not** compared and deployment equivalence is not established. Root cron was checked only for matching lines.

## Prepared follow-up automation and CLI duplicate checks

See `docs/pdf-maintenance-runbook.md` for the read-only SQL / PDF Document Info
audit, optional cron example, manually approved Crossref bibliography repair,
and the `nv upload` DOI duplicate precheck changes. The new scripts and example
schedule are locally prepared only; no production repair job was enabled.
