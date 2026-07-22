# NetVault

NetVault is a small multi-user PDF vault for a trusted team, lab, or study group.
Users upload and download PDFs with a lightweight LiteVault-style CLI, while a
remote FastAPI server stores files in a shared deduplicated repository and serves
a small authenticated web UI.

## What It Provides

- `netvault`: lightweight user CLI package with `nv` and `netvault` commands.
- `netvault-server`: separate FastAPI service package for Docker deployments, web UI, API, and admin CLI.
- PostgreSQL metadata storage via Docker Compose.
- Local filesystem PDF object storage under `storage/objects`.
- Authenticated web pages for dashboard stats, PDF upload, PDF list, and DOI download.
- Per-user editable UTD24, FT50, ABS, and Custom journal dashboard filters.

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

Passwords are always entered interactively. Do not place passwords in command
arguments, documentation, shell history, or copied examples.

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
nv login http://127.0.0.1:8000
```

Login credentials are saved in `~/.config/netvault/credentials.toml`. The server
token is valid for 7 days by default and can be changed with
`NETVAULT_TOKEN_MINUTES`. If `nv upload` finds no valid login, it prompts for
server, username, and password before uploading.

Upload PDFs. NetVault extracts a DOI from the PDF, asks Crossref for metadata,
and stores the PDF under that DOI:

```bash
nv upload ~/Documents/papers
nv upload ./paper.pdf
nv upload ./paper-a.pdf ./paper-b.pdf ~/Documents/papers
```

Before uploading bytes, the CLI checks the local PDF's DOI and sha256 against
the server. If the PDF is already in the vault, it is reported as `skipped`
without re-uploading the file.

The CLI caches file hashes by path, size, and modification time in
`~/.config/netvault/hash-cache.json`. DOI identity results are cached separately
by SHA-256 in `~/.config/netvault/identity-cache.json`, so unchanged files do
not need to be parsed again even after they are renamed. Automatic `no-doi` and
conflict results are cached too; use `--refresh-doi` to rerun automatic DOI
resolution. User-confirmed identities are never invalidated automatically.
When a local file has the DOI of an existing vault item but a different SHA-256,
the CLI also registers that digest as a server-side alias. Later uploads from
this or another machine can then skip the file during the initial SHA-256 check.

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
nv upload ./papers --no-index
```

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
- raw PDF text fallback for unusual encodings
- confidence scoring when multiple DOI candidates are present

For automatic uploads, the server treats the client result as a hint. It tries filename,
PDF-metadata, and first-three-page candidates in order, verifies each DOI with Crossref, and
checks the normalized Crossref title against extractable PDF text before accepting it. A failed
filename candidate therefore falls back to PDF metadata or page content instead of causing a
client/server DOI conflict.

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
regex as upload metadata parsing. Downloads use 8 parallel workers by default
and automatically resume incomplete `.part` files. Completed downloads are verified
against the server SHA-256 digest. Upload and download both use
a single-line progress bar.

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
