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

Users install only the lightweight CLI package:

```bash
curl -fsSL https://raw.githubusercontent.com/iihciyekub/netvault/main/scripts/install.sh | bash
```

Or manually:

```bash
uv tool install --force git+https://github.com/iihciyekub/netvault.git
```

If `uv` is not installed:

```bash
brew install uv
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

If a scanned or unusually encoded PDF cannot be parsed, provide the DOI explicitly:

```bash
nv upload ./paper.pdf --doi 10.1234/example.doi
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
- visible first-page PDF text, with reference-list DOI values heavily down-ranked
- raw PDF text fallback for unusual encodings
- confidence scoring when multiple DOI candidates are present

Inspect DOI resolution before upload:

```bash
nv doi ./paper.pdf
nv doi ./paper.pdf --verbose
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
- `DELETE /admin/pdfs/by-doi?doi=...`
- `DELETE /admin/pdfs/{id-or-sha256}`

## Development

```bash
python -m pip install -e ".[dev]" -e packages/netvault-server
pytest
uvicorn netvault_server.server.main:app --reload
```
