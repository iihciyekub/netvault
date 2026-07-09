# NetVault User Guide

NetVault is a team PDF vault. Users upload PDFs, NetVault extracts a DOI, fetches
Crossref metadata, and stores the PDF so anyone with access can download it by DOI.

## Install

Recommended install from GitHub:

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

NetVault only accepts PDF files. On upload it:

1. Checks the file is a PDF.
2. Computes sha256.
3. Extracts DOI from the PDF or filename.
4. Fetches Crossref metadata.
5. Stores the PDF by sha256.
6. Stores DOI and metadata in PostgreSQL.

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

The downloaded filename uses the original uploaded filename.

## Status

Show current account and vault size:

```bash
nv status
```

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
