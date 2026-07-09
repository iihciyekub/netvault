# NetVault

NetVault is a small multi-user PDF vault for a trusted team, lab, or study group.
Users upload and download PDFs with a LiteVault-style CLI, while a remote FastAPI
server stores files in a shared deduplicated repository.

## What It Provides

- `netvault-server`: FastAPI service for authentication, metadata, uploads, and downloads.
- PostgreSQL metadata storage via Docker Compose.
- Local filesystem PDF object storage under `storage/objects`.
- `netvault` CLI for users.
- `netvault-admin` CLI for administrators.

The default Docker Compose setup binds the API to `127.0.0.1:8000`, so it is meant
to be reached through SSH tunneling, VPN, or a trusted internal network.

## Documentation

- [User Guide](docs/user-guide.md)
- [Admin Guide](docs/admin-guide.md)
- [Migration Guide](docs/migration.md)

## Remote Server Setup

On the remote machine:

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
```

Before real use, change these values in `docker-compose.yml` or an environment-specific
override:

- `POSTGRES_PASSWORD`
- `NETVAULT_SECRET_KEY`
- `NETVAULT_BOOTSTRAP_ADMIN_PASSWORD`

The first startup creates a bootstrap admin if `NETVAULT_BOOTSTRAP_ADMIN` and
`NETVAULT_BOOTSTRAP_ADMIN_PASSWORD` are set.

## SSH Tunnel

From a user's local machine:

```bash
ssh -L 8000:127.0.0.1:8000 your-remote-host
```

Then the NetVault server is available locally at:

```text
http://127.0.0.1:8000
```

## Local CLI Install

After publishing this repo to GitHub, users should install the CLI with:

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

DOI extraction follows the same practical model as LitVault:

- explicit `--doi`
- PDF metadata markers such as `prism:doi`, `crossmark:DOI`, and `dc:identifier`
- visible PDF text from the first pages when `pdftotext` is available
- raw PDF text and filename fallback
- conflict detection when multiple DOI values disagree

List and download by DOI:

```bash
nv list
nv download 10.1234/example.doi --to ./downloads
nv status
nv logout
```

## Admin CLI

Login as an administrator first:

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
- `POST /admin/users`
- `POST /admin/users/{username}/reset-password`
- `POST /admin/users/{username}/deactivate`
- `DELETE /admin/pdfs/by-doi?doi=...`
- `DELETE /admin/pdfs/{id-or-sha256}`

## Development

```bash
python -m pip install -e ".[dev]"
pytest
uvicorn netvault.server.main:app --reload
```
