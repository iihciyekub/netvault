# NetVault User Guide

NetVault is a team PDF vault. Users install a lightweight CLI to upload and
download PDFs, while the remote server handles storage, metadata, and the web UI.

## Install

Recommended lightweight CLI install from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/iihciyekub/netvault/main/scripts/install.sh | bash
```

If `uv` is missing on macOS:

```bash
brew install uv
curl -fsSL https://raw.githubusercontent.com/iihciyekub/netvault/main/scripts/install.sh | bash
```

Check the CLI:

```bash
nv --help
```

`nv` and `netvault` are equivalent. Subcommands are case-insensitive.

The CLI package does not install the server, PostgreSQL, Docker deployment files,
or `netvault-admin`.

Update the CLI:

```bash
nv update
```

## Login

```bash
nv login https://iiaide.com/nv
```

Credentials are stored locally under:

```text
~/.config/netvault/credentials.toml
```

The login token is valid for 7 days by default. The server administrator can
change this with `NETVAULT_TOKEN_MINUTES`. If `nv upload` has no valid login, it
will ask for the server, username, and password before uploading.

Logout:

```bash
nv logout
```

## Upload PDFs

Upload one PDF:

```bash
nv upload ~/Downloads/paper.pdf
```

Upload a directory recursively:

```bash
nv upload ~/Downloads/papers
```

Upload multiple files and directories in one command:

```bash
nv upload ~/Downloads/a.pdf ~/Downloads/b.pdf ~/Downloads/papers
```

NetVault only accepts PDF files. On upload it:

1. Checks the file is a PDF.
2. Computes sha256 locally.
3. Extracts DOI locally when possible.
4. Skips the upload if the DOI or sha256 is already in the vault.
5. Uploads only new PDFs.
6. Fetches Crossref metadata.
7. Stores the PDF by sha256.
8. Stores DOI and metadata in PostgreSQL.

If DOI extraction fails, provide the DOI manually:

```bash
nv upload ~/Downloads/paper.pdf --doi 10.1016/j.ijpe.2018.04.006
```

If Crossref should be skipped:

```bash
nv upload ~/Downloads/paper.pdf --no-crossref
```

## DOI Extraction

NetVault follows the same practical DOI approach as LitVault:

- explicit `--doi`
- PDF metadata markers such as `prism:doi`, `crossmark:DOI`, `pdfx:doi`, and `dc:identifier`
- visible text from the first pages if `pdftotext` is available on the server
- raw PDF text fallback
- filename fallback, including names like `10.1234_abc-def.pdf`
- conflict detection if multiple DOI values disagree

PDFs without a DOI are rejected. This keeps the vault DOI-centered.

## List And Search

List available PDFs:

```bash
nv list
```

Search by DOI, title, author, venue, filename, sha256, or uploader:

```bash
nv search supply
nv search 10.1016
```

## Download

Download by DOI:

```bash
nv download 10.1016/j.ijpe.2018.04.006 --to ~/Downloads
```

Download multiple DOI values:

```bash
nv download 10.1016/j.ijpe.2018.04.006 10.1234/example.doi --to ~/Downloads
```

Download DOI values extracted from a text file:

```bash
nv download --file ./dois.txt --to ~/Downloads
```

The downloaded filename uses the original uploaded filename.

## Status

Show current account and vault size:

```bash
nv status
```

## Web UI

Open the authenticated web UI:

```text
https://iiaide.com/nv/web
```

The web UI uses the same username and password as the CLI. It provides dashboard
statistics, PDF listing/search, browser upload, and DOI-based download.

## Common Errors

`Not logged in`

Run:

```bash
nv login https://iiaide.com/nv
```

`No DOI found`

The PDF may be scanned or have unusual encoding. Upload with `--doi`.

`DOI conflict`

The PDF or filename contains multiple conflicting DOI candidates. Upload with the
correct `--doi`, or inspect the file first.

`PDF not found for DOI`

No active PDF is currently registered under that DOI, or an administrator soft-deleted it.

`command not found: nv`

Install the CLI, or ensure your tool install directory is in `PATH`.
