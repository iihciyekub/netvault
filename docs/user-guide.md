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

NetVault caches SHA-256 values in `~/.config/netvault/hash-cache.json` and DOI
identity results in `~/.config/netvault/identity-cache.json`. The identity cache
is keyed by SHA-256 rather than path, so moving or renaming an unchanged PDF
does not trigger another DOI scan. Automatic missing/conflict results are also
cached; pass `--refresh-doi` when you want to retry them.

If an existing DOI is confirmed for a PDF with different bytes, NetVault records
the new SHA-256 as a server-side alias of the canonical item. This handles
publisher copies, repository copies, and regenerated PDFs across devices without
repeating DOI extraction on subsequent uploads.

Recursive uploads prune common technical directories (`.git`, `.venv`,
`node_modules`, `Library`, `dist`, `build`, and `output`). Use repeated
`--exclude-dir NAME` options for additional project-specific directories. If
you explicitly pass an excluded directory as an upload path, NetVault scans it.

If DOI extraction fails, provide the DOI manually:

```bash
nv upload ~/Downloads/paper.pdf --doi 10.1016/j.ijpe.2018.04.006
```

For a PDF that has no embedded DOI but whose identity you have verified, save a
user-confirmed mapping before upload:

```bash
nv doi ~/Downloads/paper.pdf --set 10.1016/j.ijpe.2018.04.006
nv doi ~/Downloads/paper.pdf --show-cache
nv upload ~/Downloads/paper.pdf
```

The confirmation is bound to the PDF SHA-256, survives renames, and takes
precedence over automatic extraction. If an assertion is wrong, remove it with:

```bash
nv doi ~/Downloads/paper.pdf --remove
```

If Crossref should be skipped:

```bash
nv upload ~/Downloads/paper.pdf --no-crossref
```

## Check Damaged PDFs

Recursively check a directory and move PDFs that cannot be opened into an
`error` directory under the current working directory:

```bash
cd ~/Downloads
nv check-pdfs ./papers
```

Preview the result without moving anything:

```bash
nv check-pdfs ./papers --dry-run
```

Only `.pdf`/`.PDF` files are checked. The command does not inspect DOI values,
text, or page appearance, does not require login, and does not contact the
server. Encrypted PDFs are kept because encryption alone does not indicate that
a file is damaged. If files have the same name, numbered names are used in
`error/` rather than overwriting an existing file.

## DOI Extraction

NetVault uses a smart DOI resolver rather than a single PDF regex:

- explicit `--doi`
- PDF metadata markers such as `prism:doi`, `crossmark:DOI`, `pdfx:doi`, and `dc:identifier`
- filename DOI values, including names like `10.1016_j.chb.2015.03.041.pdf`
- publisher filename patterns such as Springer `s12144-024-...`, PLOS `journal.pone...`, and Frontiers `fpsyg-...`
- visible first-page text if `pdftotext` is available
- reference-list DOI candidates are heavily down-ranked
- raw PDF text fallback for unusual encodings
- confidence scoring when multiple DOI candidates are present

PDFs without a DOI are rejected. This keeps the vault DOI-centered.

Inspect how NetVault will resolve a DOI before upload:

```bash
nv doi ~/Downloads/paper.pdf
nv doi ~/Downloads/paper.pdf --verbose
nv doi ~/Downloads/paper.pdf --show-cache
```

If the resolver is still wrong or ambiguous, override it:

```bash
nv upload ~/Downloads/paper.pdf --doi 10.1016/j.ijpe.2018.04.006
```

## List And Search

List available PDFs:

```bash
nv list
nv list --limit 50 --offset 50
```

Search by DOI, title, author, venue, filename, sha256, or uploader:

```bash
nv search supply
nv search 10.1016
nv search supply --json
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
Downloads use 8 parallel workers by default, automatically resume incomplete
`.part` files, and verify completed files against the server SHA-256 digest. To
tune parallelism:

```bash
nv download --file ./dois.txt --to ~/Downloads --workers 4
```

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

The PDF or filename contains multiple high-confidence DOI candidates. Run
`nv doi FILE --verbose` to inspect them, then upload with the correct `--doi` if needed.

`PDF not found for DOI`

No active PDF is currently registered under that DOI, or an administrator soft-deleted it.

`command not found: nv`

Install the CLI, or ensure your tool install directory is in `PATH`.
