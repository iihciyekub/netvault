# NetVault Admin Console — Operations Runbook

The existing admin-only page `/nv/web/admin` now includes PostgreSQL/PDF
metrics, document audits, metadata review, bounded compression approval and a
separate explicitly authorized full-library PDFCraft + embedded DOI sweep.

## Admin Console navigation

The administration UI uses a clean, neutral, two-column management layout.
Only users with the `admin` role can access any of the pages:

- `/nv/web/admin` — **Overview**: live PostgreSQL/PDF statistics, active
  maintenance job with progress, and recent tasks.
- `/nv/web/admin/maintenance` — **Maintenance**: full library optimization,
  read-only audits and metadata preview forms. This view deliberately does not
  auto-refresh while the administrator types a confirmation.
- `/nv/web/admin/jobs` — **Task history**: current task progress, reports,
  cancellation and reviewed-change approvals. Records are paged 20 per page;
  direct links use `?page=N` and `#job-<id>`.
- `/nv/web/admin/users` — **People**: create accounts, reset passwords,
  change user active status and view current role assignments.

All links perform a full page navigation to preserve the existing admin-only
JavaScript initialization; the dashboard and task history update when status
or progress changes. Neither navigation nor page loading enqueues jobs.
Styling uses NetVault's own palette and typography (including dark mode).
English, Simplified Chinese and Traditional Chinese navigation are maintained
in `static/i18n-catalog.tsv` and generated into `static/i18n.js`.
All browser copy is English, as required by `AGENTS.md`.

## Security and architecture

```
admin cookie + CSRF
  -> /web/admin (FastAPI; role must be admin)
  -> maintenance_jobs table (durable, single-active-job unique index)
  -> host-side root-owned cron runner (Docker private network)
  -> whitelisted scripts, fixed argument lists, no shell=True
  -> owner-only JSON reports in storage/tmp/admin-maintenance
```

The web service never receives Docker's socket, a VPS shell, database passwords
from the worker, or PostgreSQL's public port. Normal users cannot read reports,
queue jobs, or approve operations even if they guess API URLs. The app uses a
database-backed lock to prevent multiple active admin maintenance jobs. The
host runner additionally takes a nonblocking OS file lock.

## Read-only overview and previews

The web interface shows active PDF rows, the sum of their SQL file sizes,
PostgreSQL database size, compression markers for PPI=150/quality=75,
metadata backlog and free disk space. The values are point-in-time data and
may include files uploaded in the last two hours. The audit itself deliberately
excludes recent uploads.

From the administrator page:

1. **Run Read-Only Audit** queues `audit_pdf_maintenance.py` (up to 100
   candidates in each preview list).
2. **Preview Metadata Changes** queues `repair_pdf_bibliography.py` without
   `--apply` (up to 10 records; only trusted DOI and verified PDF evidence).
3. Open the job's **Report** link to inspect JSON with exact candidate IDs,
   SHA-256 hashes, and accepted/rejected metadata proposals.

No admin page view, statistics refresh, or read-only preview runs write scripts.

## Approval and write gates

Mutating tasks require all of the following, enforced by both server and runner:

- A completed matching preview not older than one hour, with verified SHA-256
  report integrity; a preview can only be approved once.
- An admin role plus CSRF token plus exact typed phrase, `APPLY METADATA` or
  `COMPRESS PDFS`.
- The runner verifies the source preview again before execution and checks the
  latest backup in `/root/netvault-data-backups/YYYYMMDDTHHMMSSZ/manifest.txt`.
- The backup must be less than 24 hours old, contain a PostgreSQL dump and
  a storage/objects snapshot, and match the dump's manifest SHA-256. At least
  5 GiB of storage must be free.
- Metadata application re-fetches Crossref and re-verifies PDF identity.
  Only exactly previewed DOI+SHA+fill-only fields can be written. Existing
  nonempty fields are never replaced.
- Compression is pilot mode, one process, a batch of at most 10 explicitly
  approved document IDs and SHA hashes; it rechecks every candidate before
  the first modification and again at each document. It reuses PDFCraft's
  global coordination lock, signed/encrypted-PDF safeguards and content checks.
- The admin UI does not expose arbitrary SQL, arbitrary paths, shell commands,
  bulk deletion, or cleanup/retry of failed DOI identities.

**Approval queues a write, it does not automatically create a backup.**
Operators must first ensure the existing verified backup is suitably recent.
The snapshot timestamp shown in the UI is not itself a verification success.
Task failures are recorded in the audit table and detailed output remains in
host-only `storage/tmp/admin-maintenance/<job-id>.log`.

## Full Library Optimization + DOI Writeback (no automatic backup)

The visible **Full Library Optimization + DOI Writeback** card invokes the
same existing host command used for the last full scan:

```bash
cd /root/iiaide/netvault
.venv/bin/python scripts/compress_pdfs_parallel.py --all --jobs 3
```

The command above is shown for transparency; clicking the UI queues a
`full_compression` task instead of executing inside FastAPI. The host worker
then launches the exact allowlisted command (jobs 1–4) as a separate process.
PDFCraft uses PPI 150 / quality 75 and the existing
`storage/tmp/pdfcraft-batch.lock` plus resumable
`storage/tmp/pdfcraft-unprocessed-state.json`. It scans the entire eligible
backlog, not just 10 documents; already-optimized SHA + recipe markers are
skipped. For a verified DOI identity, missing embedded `/DOI` is written to
the candidate output. The output replaces the original only if it is smaller
and passes SHA, structural and PDF metadata checks. When a replacement is
published, PostgreSQL is updated in the same business operation; the old PDF
object is unlinked after committing. DOI text in PostgreSQL is not guessed or
automatically altered.

**This all-library mode deliberately does not run backups or require a
backup manifest.** The admin must type `RUN FULL LIBRARY` and independently
check the acknowledgement that a rollback may be impossible without an
existing external backup. The existing small-batch/metadata apply flows retain
their more restrictive reviewed-preview and backup requirement. Full-library
mode does *not* execute `repair_pdf_bibliography.py --apply`; Crossref
title/author/journal/year enrichment is a distinct workflow.

The admin page tracks only this job's counts by subtracting the legacy script's
persisted cumulative counters at start. Progress includes attempted PDFs,
replaced/unchanged/failed counts, `/DOI` writes, space saved and a sanitized
recent log. Yesterday's cumulative counters are not shown as today's progress.
Background work continues after browser closure. The task can be cancelled
by typing `STOP FULL RUN`. This sends SIGTERM to the coordinator, letting it
drain in-flight PDFs and checkpoint the resume cursor; after the grace period
the worker forcibly stops the process group. A subsequent admin-triggered
full run picks up the retained cursor. No automatic rerun is scheduled.

The host cron installed for this console only polls for explicitly queued
jobs once per minute. It never starts a full run on its own. Deploy this
feature by applying the additive `cancel_requested` column migration before
or together with the updated host worker, and verify an empty job queue
before restarting. Do not run `--all` or enqueue a production full job as
a deployment smoke test.

## Host worker installation (separate from optional read-only audit cron)

On the VPS, after deploying matching server, scripts, tests and migrations:

```bash
cd /root/iiaide/netvault
docker compose -f docker-compose.iiaide.yml --env-file .env ps
.venv/bin/python -m pytest -q tests/test_admin_maintenance_console.py
.venv/bin/python scripts/run_admin_maintenance_worker.py
sudo install -o root -g root -m 0644 deploy/netvault-admin-maintenance.cron /etc/cron.d/netvault-admin-maintenance
sudo systemctl reload cron || sudo systemctl restart cron
```

The first manual worker invocation should have **no queued jobs**. It only
refreshes a harmless timestamp and validates connectivity. Do not run a
production metadata apply or PDF compression as a deployment smoke test.

Verify the `/web/admin` page using an actual admin account, the online
worker indicator, and a read-only audit. Verify ordinary users receive 403
and that the user-management forms still work. Do not write to or clean up
the production storage tree during deployment.

To suspend new execution without losing already queued jobs:

```bash
sudo mv /etc/cron.d/netvault-admin-maintenance /etc/cron.d/netvault-admin-maintenance.disabled
```

(Place the disabled file outside `/etc/cron.d` if the cron implementation
loads all files irrespective of suffix.) Jobs left queued remain in
PostgreSQL; the worker does not run until scheduling is restored. Do not
replay interrupted write jobs automatically. The worker marks orphaned
`running` jobs as failed after its exclusive lock confirms no worker is alive.

## Recovery and known boundaries

Logs and reports are retained locally in `storage/tmp/admin-maintenance`.
Do not publish raw logs, DB credentials or backup paths through the web.
Restore PostgreSQL and PDF objects together using the documented
`docs/admin-guide.md` backup procedure during a maintenance window.
PDFCraft already guards against overwrites but cannot provide atomic
cross-resource rollback; an interrupted operation requires manual review.
A failed batch can contain some successfully completed PDFs, so verify the
result and backup before attempting a new approved preview.

The queue polls once a minute. Browser polling merely refreshes the display;
closing the browser cannot stop a job. Background jobs are host-owned and
persist independently of FastAPI process restarts. The host cron is **not**
a schedule for automatic repairs or compression: it only runs explicitly
approved, whitelisted jobs from the admin queue.
