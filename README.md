# NetVault

NetVault is a small multi-user PDF vault for a trusted team, lab, or study group.
Users upload and download PDFs with a lightweight LiteVault-style CLI, while a
remote FastAPI server stores files in a shared deduplicated repository and serves
a small authenticated web UI.

## Upload results and recovery

`nv upload ./papers` shows one progress bar with a fixed total of files needing processing. It advances when each file has a final result, including server deduplication, errors and remembered rejections. Existing hashes and local duplicates are summarized once before that stage. Only newly stored PDFs count as uploaded.

Individual errors and index warnings go to the atomic `nv-upload-report.json` instead of printing a line for every failed PDF. Use `--report PATH` for another location or `--verbose` to display individual errors, cache statistics, transfer speed and request timings.

Recognized server DOI/title verification failures become `needs-review`. An unchanged PDF is subsequently `review-skipped`, even after moving or renaming it without a new DOI hint. Rejections are keyed by SHA-256 and server, and committed to `.netvault-upload.sqlite3` beside each PDF; ordinary uploads still check server hashes first. File contents, DOI/index/CSV evidence, filename DOI hints, resolver rule changes permit another attempt. Ordinary CLI/server release upgrades preserve rejection state. Temporary network/provider errors and unrelated request-validation errors never block later uploads. A local missing DOI still gets a server attempt.

Use `nv upload ./papers --refresh-doi` to retry rejected identities, or `nv upload ./papers --retry-failed` to select failed, pending and review files from directory state (with legacy report fallback). A successful upload clears the remembered rejection. Directory state is independent of per-run reports and global cache eviction; copying the complete directory preserves it. PaperDock automatically reads the same state through a private integration interface; no extra nv command is required. New rejections/errors exit with status 1; skipping remembered review files alone exits with status 0. Use `--refresh-doi` explicitly after server verification rules change.

Run the same upload command to continue. Keep the hidden state file with the PDFs
when copying a directory. The [web CLI guide](https://iiaide.com/nv/web/cli), this
README and the [user guide](docs/user-guide.md) describe the same commands.

A sibling EBSCO `cli-downloads.csv` supplies DOI hints when the downloaded filename and byte size match. For recursive uploads, a CSV at the selected folder root also applies to PDFs in nested subfolders. These legacy records continue through server-side primary-title verification; every new hash, including a SHA-256-bound JSON index hint, passes through server identity checks. `--no-index` disables both index formats. PDFs without a usable DOI may use first-page title lookup; an unresolved identity remains a failure and does not increase dashboard counts. MIS Quarterly and MIS Quarterly Executive remain separate journals.

With automatic CSV discovery, renamed PDFs can be matched by SHA-256 or a unique article-title column matched against the PDF first page; server title verification still applies. Without a usable match, automatic uploads fall back to PDF DOI extraction.

**Explicit `--csv PATH` trusts the CSV's DOI/PDF pairs**, including scanned PDFs with no readable text. It matches a unique record by SHA-256 or filename and byte size, without PDF title matching, PDF content verification, or PDF DOI fallback. Unmatched, ambiguous, or conflicting records are reported as failures. CSV rows need `file` (or `filename`), `doi`, `bytes` (or `size`), and `status=downloaded`; `sha256` is optional and supports renamed files. Trust applies only to the explicit invocation and is not saved in the DOI cache. PDF signature/completeness checks, size limits, hash deduplication, and existing DOI assignment conflict checks remain active. Metadata lookup continues unless `--no-crossref` is used; missing or unavailable metadata does not block ordinary CSV uploads. `--force` still requires available DOI metadata. Use `--dry-run` to preview matches without login, uploads, or local writes. `--csv` cannot be combined with `--doi`, `--index-file`, or `--no-index`.

## What It Provides

- `netvault`: lightweight user CLI package with `nv` and `netvault` commands.
- `netvault-server`: separate FastAPI service package for Docker deployments, web UI, API, and admin CLI.
- PostgreSQL metadata storage via Docker Compose.
- Local filesystem PDF object storage under `storage/objects`.
- Authenticated web pages for dashboard stats, PDF upload, PDF list, and DOI download.
- Per-user editable UTD24, FT50, ABS, and Custom journal dashboard filters.
- Local hash-based DOI identification, safe file renaming, duplicate collection, and CSV reports.

The default Docker Compose setup binds the API to `127.0.0.1:8000`, so it is meant
to be reached through SSH tunneling, VPN, or a trusted internal network.

## Documentation

- [User Guide](docs/user-guide.md)
- [Admin Guide](docs/admin-guide.md)
- [Migration Guide](docs/migration.md)

## Deploy Server

The server package lives under `packages/netvault-server` and is installed only
inside the Docker image. On the remote machine:

```bash
git clone <your-netvault-repo>
cd netVault
cp docker-compose.yml docker-compose.local.yml
docker compose up --build -d
```

For the existing `iiaide.com` Traefik host, use:

```bash
docker compose -f docker-compose.iiaide.yml up --build -d
```

Production operators should also read the [SSH/PostgreSQL operations audit](docs/operations-audit-2026-07-13.md) and the [full migration runbook](docs/migration.md). They document the private Docker database connection, backup limitations, validation, cutover, and rollback procedures.

Required `.env` values:

```text
APP_HOST=iiaide.com
POSTGRES_PASSWORD=change-this
NETVAULT_SECRET_KEY=change-this
NETVAULT_BOOTSTRAP_ADMIN=admin
NETVAULT_BOOTSTRAP_ADMIN_PASSWORD=change-this
NETVAULT_BASE_PATH=/nv
NETVAULT_SECURE_COOKIES=true
```

Before real use, change these values in `docker-compose.yml` or an environment-specific
override:

- `POSTGRES_PASSWORD`
- `NETVAULT_SECRET_KEY`
- `NETVAULT_BOOTSTRAP_ADMIN_PASSWORD`

The first startup creates a bootstrap admin if `NETVAULT_BOOTSTRAP_ADMIN` and
`NETVAULT_BOOTSTRAP_ADMIN_PASSWORD` are set.

The web UI is available at:

```text
https://iiaide.com/nv/web
```

It uses the same users and passwords as the CLI. Browser sessions are stored in
an HttpOnly JWT cookie.

Login supports interactive prompts and optional `--username` / `--password`
arguments. The commands below use example credentials; interactive password
entry is available for everyday use.

## SSH Tunnel

From a user's local machine:

```bash
ssh -L 8000:127.0.0.1:8000 your-remote-host
```

Then the NetVault server is available locally at:

```text
http://127.0.0.1:8000
```

## Install CLI

Users install only the lightweight CLI package. The installers below install `uv`
when necessary, resolve the latest published NetVault release, install its wheel,
configure the command directory, and verify the installation.

### macOS and Linux

```bash
(
  set -e
  installer="$(mktemp "${TMPDIR:-/tmp}/netvault-install.XXXXXX")"
  trap 'rm -f "$installer"' EXIT
  curl -fsSLo "$installer" https://raw.githubusercontent.com/iihciyekub/netvault/main/scripts/install.sh
  bash "$installer"
)
```

### Windows PowerShell

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Join-Path $env:TEMP ('netvault-install-'+[guid]::NewGuid()+'.ps1'); try { irm https://raw.githubusercontent.com/iihciyekub/netvault/main/scripts/install.ps1 -OutFile $p; & $p } finally { Remove-Item $p -Force -ErrorAction SilentlyContinue }"
```

Open a new terminal after installation, then verify:

```text
nv --version
```

The installers pin the installation to GitHub's latest published release. They
download the release wheel directly and do not require Git. Only the lightweight
CLI is installed; the server, web interface, and deployment files are excluded.

For development from `main` instead:

```bash
uv tool install --force git+https://github.com/iihciyekub/netvault.git@main
```

The CLI provides both names:

```bash
netvault --help
nv --help
```

`nv` is the recommended short command. CLI subcommands are case-insensitive, so
`nv upload`, `nv Upload`, and `nv UPLOAD` are treated the same.

Update from GitHub:

```bash
nv update
```

`nv update` also resolves and installs the latest published release tag. See the
[release guide](docs/releasing.md) for the automated version and publication checks.

If the GitHub repo URL changes, update with:

```bash
nv update --repo-url https://github.com/YOUR_NAME/YOUR_REPO.git
```

For local development:

```bash
python -m pip install -e .
```

Login:

```bash
nv login http://127.0.0.1:8000 --username example-user --password example-password
```

Replace the example username and password with your own credentials, or run
`nv login http://127.0.0.1:8000` to enter them when prompted.

Login credentials are saved in `~/.config/netvault/credentials.toml`. The server
token is valid for 7 days by default and can be changed with
`NETVAULT_TOKEN_MINUTES`. If `nv upload` finds no valid login, it prompts for
server, username, and password before uploading.

Upload PDFs. NetVault extracts a DOI from the PDF, asks Crossref for metadata,
and stores the PDF under that DOI.

Uploads use three concurrent requests by default, with DOI identities prepared
in batches of 12 while earlier files upload. Hash checks, DOI verification and
idempotency remain enabled. Force replacements run serially in input order.
Use `nv upload ./papers --jobs 1` to diagnose a connection with serial requests;
`--jobs` accepts 1 through 3 and requires no change to the usual command.

Default output contains concise result counts, elapsed time and the report path.
Add `--verbose` for aggregate multipart bytes, average MiB/s, DOI/cache statistics
and detailed send, response-wait and server timings. Timings overlap under
concurrency and do not sum to total elapsed time.

```bash
nv upload ~/Documents/papers
nv upload ./paper.pdf
nv upload ./paper-a.pdf ./paper-b.pdf ~/Documents/papers
```

Before uploading bytes, the CLI checks the local PDF's DOI and sha256 against
the server. If the PDF is already in the vault, it is reported as `skipped`
without re-uploading the file.

The CLI caches file hashes by path, size, modification/change time and file identity in
`~/.config/netvault/hash-cache.json`. DOI identity results are cached separately
by SHA-256 in `~/.config/netvault/identity-cache.json`, so unchanged files do
not need to be parsed again even after they are renamed. Automatic `no-doi` and
conflict results are cached too; use `--refresh-doi` to rerun automatic DOI
resolution. User-confirmed identities are never invalidated automatically.
When a local file has the DOI of an existing vault item but a different SHA-256,
the server validates the uploaded file before recording its digest as an alias.
A manual DOI or local confirmation is a hint, not permission to register a new hash
without server verification. Explicit CSV pairs remain user-trusted. Later uploads
from either CLI or web can skip accepted aliases during the initial hash check.

Automatically discovered indexes skip invalid entries with one warning and use
independent PDF extraction for those files. Valid SHA-bound entries remain usable.
An explicitly supplied `--index-file` remains strict; stale hashes still fail.

When a PDF directory contains `pdf-download-index.json`, `nv upload` first looks
for a version 1 record whose `sha256` matches the locally computed PDF digest. A
valid matching record supplies the DOI without parsing DOI text from the PDF. A
PDF with no index record uses the normal cached or PDF-derived DOI resolver. If
the filename is indexed but the digest differs, the upload fails instead of
silently trusting a stale index.

The native index format is:

```json
{
  "version": 1,
  "updatedAt": "2026-07-13T04:58:31.935Z",
  "algorithm": "SHA-256",
  "records": [
    {
      "doi": "10.1002/mar.20228",
      "filename": "10.1002_mar.20228.pdf",
      "size": 147264,
      "lastModified": 1783918710583,
      "sha256": "81de15d258937e983e38bbca3bf63d2c2431ecf368d2457ef7194a3bb934bb5d",
      "downloadedAt": "2026-07-13T04:58:30.601Z",
      "sourceUrl": "/doi/pdfdirect/10.1002/mar.20228?download=true",
      "validation": {
        "status": "valid",
        "checkedAt": "2026-07-13T04:58:30.579Z",
        "method": "pdf-signature-eof",
        "reason": null
      }
    }
  ]
}
```

Use one index for every selected PDF, or bypass index discovery:

```bash
nv upload ./papers --index-file ./custom-download-index.json
nv upload ./papers --csv ./cli-downloads.csv
nv upload ./papers --csv ./cli-downloads.csv --dry-run
nv upload ./papers --no-index
```

Passing `--csv` explicitly accepts the matched DOI/PDF pairs without reading PDF text;
placing the CSV next to the PDFs without that option keeps automatic title verification.
Existing DOI records with a different file hash are deduplicated as CSV-trusted aliases;
an existing hash assigned to a different DOI is rejected, and files are not replaced
unless `--force` is passed.

Automatic DOI hints follow this priority: a securely matched
`cli-downloads.csv` / SHA-256 download index, explicit DOI fields in the PDF's
embedded metadata, DOI evidence on its first two pages, and only then its
filename. The server checks each candidate against article metadata and the
paper's first-page heading. DOI strings found only in references receive lower
confidence; a matching filename never overrides verified PDF evidence.
Renamed files with a known `--<hex fingerprint>` suffix are recognized.
Use `nv upload . --retry-failed` after a transient identity error to retry
previously failed, pending or review files from `nv-upload-report.json`.

Change the sibling filenames in `~/.config/netvault/config.toml`:

```toml
[upload.index]
enabled = true
names = ["pdf-download-index.json"]
```

Each configured name must be a JSON basename. During recursive upload, NetVault
uses only an index in the PDF's immediate directory; it does not search parent
directories. An explicit `--index-file` applies to every selected PDF.

Recursive uploads skip common technical directories such as `.git`, `.venv`,
`node_modules`, `Library`, `dist`, `build`, and `output`. An explicitly supplied
directory is still scanned even when its own name is normally excluded. Add
project-specific exclusions by repeating `--exclude-dir`:

```bash
nv upload . --exclude-dir archive --exclude-dir generated
```

Directory-name exclusions ignore case at every depth. `identify --rename` uses
`unidentifys/` for files absent from the vault, which can be valid new articles;
it does not establish upload eligibility. To intentionally omit collection folders:

```bash
nv upload ./papers --exclude-dir unidentifys --exclude-dir duplicates
```

If a scanned or unusually encoded PDF cannot be parsed, provide the DOI explicitly:

```bash
nv upload ./paper.pdf --doi 10.1234/example.doi
```

To replace the existing PDF for one DOI and overwrite its metadata with the
current Crossref record, add `--force`. Any authenticated user may do this:

```bash
nv upload ./replacement.pdf --force
```

Pass `--doi DOI` as usual when the PDF does not contain a reliable DOI. The
replacement is committed only after Crossref returns a valid record. On
success, the old PDF and its file aliases are removed.

Save a user-confirmed identity when the PDF itself does not contain a DOI:

```bash
nv doi ./paper.pdf --set 10.1234/example.doi
nv doi ./paper.pdf --show-cache
nv upload ./paper.pdf
```

Remove an incorrect cached identity or force a fresh automatic resolution:

```bash
nv doi ./paper.pdf --remove
nv upload ./paper.pdf --refresh-doi
```

If a PDF is already stored under the wrong DOI, administrators can correct only its identity and
metadata without replacing the PDF file, SHA-256, aliases, upload history, or download history:

```bash
nv correct-doi 56240 10.1287/msom.2021.1032 \
  --reason "Incorrect DOI extracted from PDF syntax"
```

The command previews the canonical DOI and fresh metadata first, then asks for confirmation.
The same correction workflow is available to administrators from the PDF search page in the Web
UI through **Correct DOI**.


If you only want DOI indexing and PDF storage, skip Crossref:

```bash
nv upload ./paper.pdf --no-crossref
```

DOI extraction uses NetVault's smart resolver:

- explicit `--doi`
- PDF metadata markers such as `prism:doi`, `crossmark:DOI`, `pdfx:doi`, and `dc:identifier`
- filename DOI values, including `10.1016_j.chb.2015.03.041.pdf`
- publisher filename patterns such as Springer `s12144-024-...`, PLOS `journal.pone...`, and Frontiers `fpsyg-...`
- visible text from the first three PDF pages, with reference-list DOI values heavily down-ranked
- structured DOI links from PDF annotations
- bounded metadata fallback for unusual PDF encodings, without scanning arbitrary raw PDF objects
- confidence scoring when multiple DOI candidates are present

For online uploads, the server treats client DOI values as hints, including explicit `--doi`
and SHA-bound download-index identities. It verifies candidates against Crossref, falls back to
DOI.org citation metadata when Crossref explicitly has no record, and checks the metadata title
against extractable PDF text before accepting it. A failed filename candidate therefore falls
back to metadata or page content instead of causing a client/server DOI conflict.

If both registry providers report no record, a DOI-indexed Semantic Scholar record
can recover the identity. Its DOI must match exactly, and its title, an author and
publication year must be corroborated before the abstract on the PDF's first page.
The evidence records this verification provider. Provider outages do not trigger
this fallback. Short titles such as "OTC Discount" require an exact standalone
heading on the first page rather than a fuzzy or body-text match.

When automatic candidates are missing or cannot be verified, a document-metadata
title can trigger a bounded Crossref search. NetVault accepts only an unambiguous
article whose title, journal, year and author are corroborated in the PDF. Journal
and issue DOIs are excluded from automatic article resolution.

Inspect DOI resolution before upload:

```bash
nv doi ./paper.pdf
nv doi ./paper.pdf --verbose
nv doi ./paper.pdf --set 10.1234/example.doi
nv doi ./paper.pdf --show-cache
nv doi ./paper.pdf --remove
```

Check a directory recursively for PDF files that cannot be opened, and move only
those damaged files into `./error` under the directory where the command is run:

```bash
nv check-pdfs ~/Downloads/papers
nv check-pdfs ~/Downloads/papers --dry-run
```

The check is local, does not require login, ignores non-PDF files, and keeps
encrypted PDFs because encryption by itself does not indicate file damage.

If a PDF is scanned, mislabeled, or still ambiguous, provide the DOI explicitly:

```bash
nv upload ./paper.pdf --doi 10.1234/example.doi
```

Export the complete active DOI/SHA-256 inventory for a print or electronic ISSN:

```bash
nv export --issn 0025-1909
nv export --issn 1526-5501
nv export --issn 0025-1909 --out ./management-science.csv
nv export --issn 1526-5501 --out ./management-science.csv --force
```

The CSV has `doi,sha256` columns and no pagination limit. Registered print and
online ISSNs share the journal's inventory, including older entries without
per-article serial identifiers. Without `--out`, output defaults to
`netvault-<ISSN>.csv` in the current directory. Repeated exports automatically use
`netvault-<ISSN>-2.csv`, `netvault-<ISSN>-3.csv`, and so on. Saving is atomic.
Use `--force` to replace the default file or an existing explicit `--out` path.

Identify and organize local PDFs with one command. Preview first; add `--rename`
to apply changes:

```bash
nv identify ./papers
nv identify ./papers --rename
nv identify ./papers --issn 0025-1909 --rename
nv identify ./papers --index ./netvault-0025-1909.csv --rename
```

With no inventory option, `identify` queries the complete vault, including hash
aliases. `--issn` downloads one journal inventory for fast local matching;
`--index` uses an exported CSV offline, without login or network requests.
Both retain unmatched PDFs with status `not_in_index`, since absence from one
inventory does not mean absence from the vault. Do not combine these options.

All modes use parallel hashing, a bounded processing pipeline and a verified
per-run hash cache by default, without parsing PDF pages. Identical copies and
verified same-DOI filename conflicts are collected into `duplicates/` beside
each source PDF. Full-vault misses move to the source's `unidentifys/`; index
misses stay in place. Archive folders are skipped recursively and existing
files are never overwritten. Use `--check-content` for slower page-content
duplicate checks, `--workers 1` for serial reads, or `--csv` for a custom report.
The default report is `netvault-index.csv` in the scanned directory.

List and download by DOI:

```bash
nv list
nv download 10.1234/example.doi --to ./downloads
nv download 10.1234/example.doi 10.5678/another.doi --to ./downloads
nv download --file ./dois.txt --to ./downloads
nv status
nv logout
```

`nv download --file` extracts DOI values from any text file using the same DOI
regex as upload metadata parsing. Downloads look up DOI file information in batches
of 200, prefetch the next batch, and start downloading before the full list is
looked up. Downloads use 8 parallel workers by default with a bounded queue and
automatically resume incomplete `.part` files. SHA-256 is computed while writing
and checked against the server digest before the file is finalized; existing
matching files are skipped. The single-line progress bar shows processed PDFs
and the number of DOIs looked up. If interrupted or disconnected, run the same
command with the same `--to` directory: completed matching files are skipped
and partial files resume from their saved byte offset. Ctrl+C cancels queued
downloads and preserves partial files.

Web uploads send each PDF in its own request with at most two concurrent files,
so one failure can be retried without repeating the whole batch. Files larger
than 32 MB skip browser-side hashing to keep page memory bounded; the server
still computes and verifies SHA-256 for every upload.

## Admin CLI

The admin CLI is part of the server package, not the lightweight user CLI. Use it
from a development checkout with the server package installed, or from an admin
environment:

```bash
python -m pip install -e packages/netvault-server
```

Login as an administrator first with the user CLI:

```bash
netvault login http://127.0.0.1:8000
```

Then:

```bash
netvault-admin create-user alice
netvault-admin reset-password alice
netvault-admin deactivate-user alice
netvault-admin delete-pdf 1
```

## API Summary

- `POST /auth/login`
- `POST /auth/logout`
- `GET /me`
- `POST /pdfs/upload`
- `POST /pdfs/exists`
- `POST /pdfs/aliases`
- `GET /pdfs`
- `GET /pdfs/search?q=...`
- `GET /pdfs/export?issn=0025-1909` (authenticated CSV, all matching active PDFs)
- `GET /pdfs/by-doi?doi=...`
- `GET /pdfs/by-doi/download?doi=...`
- `GET /pdfs/{id-or-sha256}`
- `GET /pdfs/{id-or-sha256}/download`
- `GET /stats/summary`
- `GET /stats/by-year`
- `GET /stats/by-journal`
- `GET /stats/by-journal-year`
- `POST /admin/users`
- `POST /admin/users/{username}/reset-password`
- `POST /admin/users/{username}/deactivate`
- `POST /admin/pdfs/{id}/correct-doi`
- `GET /admin/pdfs/{id}/doi-corrections`
- `DELETE /admin/pdfs/by-doi?doi=...`
- `DELETE /admin/pdfs/{id-or-sha256}`

## Development

```bash
python -m pip install -e ".[dev]" -e packages/netvault-server
pytest
uvicorn netvault_server.server.main:app --reload
```
