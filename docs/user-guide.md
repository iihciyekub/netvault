# NetVault User Guide

NetVault is a team PDF vault. Users install a lightweight CLI to upload and
download PDFs, while the remote server handles storage, metadata, and the web UI.

## Install

The platform installers install `uv` when necessary, resolve the latest published
NetVault release, install its wheel, configure the command directory, and verify
the installation. Git is not required.

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

Open a new terminal after installation, then check the CLI:

```text
nv --version
```

`nv` and `netvault` are equivalent. Subcommands are case-insensitive.

The CLI package does not install the server, PostgreSQL, Docker deployment files,
or `netvault-admin`.

Update the CLI:

```bash
nv update
```

The installer and `nv update` pin the CLI to GitHub's latest published NetVault
release. Development commits on `main` are not installed as user releases.

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

Browser logins persist for 400 days by default and renew when an authenticated
page is visited. Valid existing logins are upgraded automatically. Administrators
can shorten this with `NETVAULT_WEB_SESSION_DAYS` (1–400). Logging out, disabling
the account, or resetting its password still invalidates the session.

PDF DOI resolution reads document information, decoded XMP metadata, DOI links,
and text from the first three pages using both Poppler and pypdf. A damaged page
does not discard readable pages, and wrapped DOI tokens are repaired before
candidate scoring. The server verifies automatic candidates against DOI metadata
and the PDF title. If local extraction finds no DOI, the CLI still sends the PDF
for server extraction; unsuccessful local results are retried on the next upload.

Logout:

```bash
nv logout
```

## Identify and organize local PDFs

Look up local PDF hashes in the server database and write a CSV preview:

```bash
nv identify ~/Downloads/papers
```

The command scans `.pdf` and `.PDF` files recursively, computes their SHA-256
values from their bytes, and queries the existing authenticated API in batches
of up to 500 unique hashes. It also finds hashes registered as alternate versions
of a server PDF. No PDFs are uploaded, no DOI is inferred from PDF text, and no
CSL metadata is requested. Without `--rename`, all PDFs remain unchanged.

Rename matched PDFs by DOI and automatically collect duplicates and unmatched PDFs:

```bash
nv identify ~/Downloads/papers --rename
```

No destination arguments are required. Under each PDF's original parent directory,
`duplicates/` receives identical extra copies, equivalent page content, and
same-DOI filename conflicts with different hashes. `unidentifys/` receives
retained PDFs whose hashes have no DOI match on the server. For example,
`papers/A/copy.pdf` moves to `papers/A/duplicates/copy.pdf`, and an unmatched
`papers/A/deeper/unknown.pdf` moves to `papers/A/deeper/unidentifys/unknown.pdf`.
This applies at every depth and does not depend on the command's current working
directory. Collection directories are created when needed, only with `--rename`.

For example, DOI `10.1177/00222437241234567` produces
`10.1177_00222437241234567.pdf`. Path separators, Windows-reserved punctuation,
and control characters become underscores. The CSV retains the complete DOI.
When a DOI destination belongs to a server-matched PDF with the same DOI and a
different local hash, the extra PDF moves to `duplicates/` with status
`moved_doi_conflict`. The existing DOI-named PDF is retained. Other occupied
destinations, including two different DOIs mapping to the same filename, produce
a `conflict`; the source is kept and no file is overwritten. Very long DOI names
produce an error rather than silently truncating the DOI.

To preview or apply all operations for PDFs in the current directory:

```bash
# Preview renames and both kinds of collection moves.
nv identify

# Rename and collect using the automatic directories.
nv identify --rename
```

An optional `--duplicates-to PATH` collects duplicates into one shared destination
instead. Unidentified PDFs still move under their own original parent directories.

One copy per hash is retained. The default fast engine retains the first
successfully processed copy; an existing verified DOI destination in the same
directory is preferred. With `--check-content`, an already DOI-named copy is
preferred across the complete scan, then the first sorted absolute path. Other
copies move to the duplicate directory using their original basenames. If a basename is
already taken, a short hash and, when needed, a number are appended. Different
PDF bytes sharing a DOI are recorded separately as DOI conflicts when they
compete for the same filename. Identical copies without
a server match are also collected: one copy goes to `unidentifys/`, the others
to `duplicates/`, and the CSV records the retained copy's final path.

With `--check-content`, PDFs with different byte hashes can also be recognized
as containing the same complete page content. The command compares page objects
and decoded resources, ignoring document metadata,
object numbers, stream compression, and verified standalone Wiley download notices.
Fonts, images, annotations, page geometry/order, and optional-content settings remain
part of the comparison. This is a conservative comparison, not a text-only similarity
check. Unreadable, encrypted, empty, attached-file, or unsupported cyclic PDFs receive
no content fingerprint and still use the existing hash/DOI rules.

For equivalent pages, one server-matched, correctly named copy is preferred, then
the first sorted absolute path. Extra versions move to `duplicates/` with status
`moved_content_duplicate`, even when their registered DOIs differ. Each CSV row retains
its own server DOI and actual byte hash, and records the retained PDF's path/hash.
No DOI suffix is stripped: `_2`, `_3`, etc. can be valid parts of registered DOIs.
Content duplicates are collected before applying the unmatched-file rule, across
the full recursively scanned directory. Both archive directories remain excluded.

Folders named `duplicates` or `unidentifys` are excluded at every depth, ignoring
case, including subsequent runs. An explicit shared duplicate destination is also
excluded. Explicit destinations cannot equal or contain the scan directory, or
overlap each other. Automatic collection folders must be directories rather than
symbolic links. Original basenames are kept when collecting
unmatched PDFs; a hash and number prevent overwriting prior runs or same-name files.
Symbolic links are skipped. Cross-filesystem moves verify the copied hash before
removing the original file. Files changed after scanning are left in place.

By default the CSV is saved as `netvault-index.csv` in the scanned directory.
Repeated runs produce `netvault-index-2.csv`, `netvault-index-3.csv`, etc., keeping
earlier audit records. Choose a different new report filename with:

```bash
nv identify ~/Downloads/papers --rename --csv ~/Downloads/papers-report.csv
```

An explicitly specified existing CSV is never overwritten. The parent directory
must already exist. Reports use UTF-8 with a BOM for spreadsheet compatibility
and contain one row per scanned PDF, including unmatched files and failures:

| Column | Meaning |
| --- | --- |
| `sha256` | Actual local PDF hash, including when matched through a server alias |
| `doi` | Complete DOI returned by the server, or empty when unmatched |
| `directory` | Actual containing directory after this run |
| `original_filename`, `original_path` | Name and absolute path before processing |
| `filename`, `path` | Actual name and absolute path after processing |
| `proposed_path` | Planned destination; preview runs leave `path` unchanged |
| `status` | `would_rename`, `would_move_duplicate`, `would_move_content_duplicate`, `would_move_doi_conflict`, `would_move_unidentified`, `renamed`, `moved_duplicate`, `moved_content_duplicate`, `moved_doi_conflict`, `moved_unidentified`, `already_named`, `not_found`, `not_in_index`, `conflict`, or `error` |
| `match_status` | `matched`, `not_found`, `not_in_index`, or `error` |
| `duplicate_of` | Actual path of the retained identical copy |
| `conflict_with`, `conflict_with_sha256` | Path and local hash of the retained same-DOI file for a DOI conflict; the hashes differ |
| `content_sha256` | Conservative page-content fingerprint, or empty when unavailable |
| `content_duplicate_of`, `content_duplicate_of_sha256` | Actual path and byte hash of the retained PDF with equivalent page content |
| `error` | Reason a file could not be processed |

Without `--rename`, the command creates a report but does not move or rename PDFs.
The fast engine applies each successful batch; a later server failure stops
the run and preserves completed CSV records. A failure is never treated as an
unmatched PDF. With `--check-content`, all queries finish before any file changes.
A run containing file errors or conflicts writes its report and exits with
status 1; otherwise it exits with 0.

## One command for literature management

Use `identify` for all local matching and renaming. Preview first; add `--rename`
to apply the changes. The separate `rename` command has been removed.

```bash
# Mixed journals or unknown ISSN: query the complete vault.
nv identify ./papers
nv identify ./papers --rename

# Known journal: download its inventory once and match locally.
nv identify ./papers --issn 0025-1909
nv identify ./papers --issn 0025-1909 --rename

# Reuse an exported inventory entirely offline.
nv export --issn 0025-1909
nv identify ./papers --index ./netvault-0025-1909.csv
nv identify ./papers --index ./netvault-0025-1909.csv --rename
```

Without `--issn` or `--index`, matching uses the server's bulk hash lookup,
including aliases. `--issn` accepts a print or electronic ISSN and downloads one
temporary inventory. `--index` needs no login or network requests. The complete
`doi,sha256` CSV is validated before any PDF processing; invalid rows or a hash
assigned to conflicting DOIs stop the command. Do not combine these two options.

An inventory miss means only that the file was not in that selected index.
Its report status is `not_in_index`, and the retained PDF stays in place, even
with `--rename`. An empty inventory likewise leaves retained PDFs in place.
Run `nv identify ./papers` without either option to check the remaining files
against the complete vault. Full-vault misses move to `unidentifys/` only when
`--rename` is supplied. Identical extra copies and verified same-DOI filename
conflicts move to `duplicates/` under their own original parent directories in
all modes. Files with different DOIs that encode to the same filename are kept
and reported as conflicts. No existing PDF is overwritten.

The default engine keeps the fast hash-only method: four parallel SHA-256 readers,
at most twice the reader count queued, matching and moves as batches finish, and
a verified per-run hash cache. Online lookups use batches of up to 500; an offline
index can match and process each file as soon as its hash is ready. PDF pages are
not parsed by default. Use `--workers 1` for serial reads. Hashes are reused only
while device, inode, size, mtime and ctime agree, and checked again before moves.

Fast processing retains the first successfully processed copy for each exact
hash, preferring an existing verified DOI destination in the same directory when
needed. Later identical copies reference that retained file in the CSV.
All archive folder names are skipped recursively. If a later server batch fails,
completed moves remain recorded in the durable CSV; remaining sources stay put.
A server error is never treated as an unidentified file.

For different PDF bytes containing equivalent complete page content, opt in:

```bash
nv identify ./papers --check-content
nv identify ./papers --check-content --rename
```

This uses the full content-comparison workflow described above and is slower.
It can also be combined with `--issn` or `--index`; retained index misses still
stay in place. All lookups and content checks complete before its first move.

Each run writes `netvault-index.csv`, then `netvault-index-2.csv`, and so on,
in the scanned directory. Use `--csv PATH.csv` for a different new report.
Without `--rename`, only the preview CSV is written. File errors or unresolved
filename conflicts produce exit status 1.

## Upload PDFs

Uploads use three concurrent requests by default, with DOI identities prepared
in batches of 12 while earlier files upload. Hash checks, DOI verification and
idempotency remain enabled. Force replacements run serially in input order.
Use `nv upload ./papers --jobs 1` to diagnose a connection with serial requests;
`--jobs` accepts 1 through 3 and requires no change to the usual command.

Progress shows aggregate multipart bytes sent and average MiB/s since the first
request. The summary separates body-send time from response wait, which includes
network and server processing. Updated servers also report average storage, PDF
parsing, verification, persistence and total handler time. Per-request averages
can overlap under concurrency; they do not sum to the command's elapsed time.


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

### Download index files

Automatically discovered indexes skip invalid entries with one warning and use
independent PDF extraction for those files. Valid SHA-bound entries remain usable.
An explicitly supplied `--index-file` remains strict; stale hashes still fail.

If a PDF's immediate directory contains `pdf-download-index.json`, NetVault can
use the downloader's DOI record before reading DOI text from the PDF. The JSON
file must use version 1, declare `SHA-256`, and contain a `records` array. Each
record contains `doi`, `filename`, `size`, `lastModified`, `sha256`,
`downloadedAt`, `sourceUrl`, and a `validation` object with `status`, `checkedAt`,
`method`, and `reason`.

NetVault still computes the PDF digest itself. It uses an index DOI only when the
actual digest and size match and `validation.status` is `valid`. Files absent
from the index use the normal DOI resolver. A matching filename with different
bytes is treated as a stale index error and is not silently parsed or uploaded.

Specify one index for all selected PDFs or disable index lookup:

```bash
nv upload ~/Downloads/papers --index-file ~/Downloads/pdf-download-index.json
nv upload ~/Downloads/papers --no-index
```

The default sibling name can be changed in `~/.config/netvault/config.toml`:

```toml
[upload.index]
enabled = true
names = ["pdf-download-index.json"]
```

Names must be JSON basenames. More than one configured index present in the same
directory is an error; select the intended file with `--index-file`.

For scanned PDFs, explicitly specify a CSV whose DOI/PDF pairs you trust:

```bash
nv upload ~/Downloads/papers --csv ~/Downloads/cli-downloads.csv --dry-run
nv upload ~/Downloads/papers --csv ~/Downloads/cli-downloads.csv
```

Explicit `--csv` accepts matched DOI/PDF pairs without reading PDF text,
extracting DOI candidates, or verifying PDF article titles. It requires a unique
SHA-256 match or filename and byte-size match. CSV rows must contain `file` (or
`filename`), `doi`, `bytes` (or `size`), and `status=downloaded`; optional `sha256`
supports renamed files. Unmatched or ambiguous files fail without PDF fallback,
even when a cached DOI is available. This trust applies only to this invocation;
it is not stored in the local DOI cache. `--csv` cannot be combined with `--doi`,
`--index-file`, or `--no-index`.

PDF signature/completeness checks, upload size limits, deduplication, and existing
hash/DOI conflict checks still apply. Metadata lookup continues unless disabled,
but missing or unavailable metadata does not prevent an ordinary CSV upload.
CSV assertions are recorded as `csv-trusted`, including aliases for another file
with the same DOI. `--force` still requires available DOI metadata before replacing
the stored file. A CSV discovered automatically beside the PDFs (or at the selected
folder root) remains a `download-record` hint and still requires PDF title
verification. Use explicit `--csv` even when the CSV is inside the upload folder
if you want to trust its pairs.

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

Replace the PDF and all stored metadata for an existing DOI with `--force`:

```bash
nv upload ~/Downloads/replacement.pdf --force
```

Add `--doi DOI` when automatic DOI extraction is not possible. This is available
to every authenticated user. NetVault requires a successful
current Crossref response before it replaces the canonical PDF, clears old file
aliases, and overwrites the title, authors, journal, publisher, year, and URL.
`--force` cannot change DOI identity. If a file was registered under an incorrect
DOI, an administrator must correct that record before replacing its PDF.

Automatic DOI claims are independently checked by the server. If a publisher
download URL contains a DOI-like path that conflicts with the PDF's labeled DOI,
update the CLI and inspect the evidence before confirming it explicitly:

```bash
nv update
nv doi ~/Downloads/paper.pdf --verbose
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
- visible text from the first three pages if `pdftotext` is available
- reference-list DOI candidates are heavily down-ranked
- raw PDF text fallback for unusual encodings
- confidence scoring when multiple DOI candidates are present

During an automatic upload, the server tries filename, PDF metadata, and first-three-page DOI
candidates in order. Each candidate is looked up in Crossref and, when PDF text is available,
the normalized Crossref title must match the PDF. Invalid filename candidates automatically fall
back to metadata or page content. Use `--no-crossref` only when this online verification is not
required; ambiguous offline evidence still requires an explicit `--doi`.

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

Online explicit DOI values are still verified against registry metadata and available PDF text;
they are not a bypass around server-side identity checks.

If an administrator later discovers that an already stored PDF has the wrong DOI, correct the
identity without re-uploading the PDF:

```bash
nv correct-doi 56240 10.1287/msom.2021.1032 \
  --reason "Verified DOI from the publisher record"
```

Use a PDF id, SHA-256, or current DOI as the first argument. The command previews the change before
applying it. The PDF bytes, SHA-256, aliases, and upload/download history remain unchanged while
the DOI and registry metadata are refreshed.


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

## Export a journal inventory

Export every active stored PDF for a print ISSN or electronic ISSN:

```bash
nv export --issn 0025-1909
nv export --issn 1526-5501
nv export --issn 0025-1909 --out ./management-science.csv
nv export --issn 1526-5501 --out ./management-science.csv --force
```

The CSV columns are `doi,sha256`, with one row per active stored PDF and its
current SHA-256 digest, ordered by DOI. There is no pagination limit. Print and
online identifiers registered to the same journal export the same inventory,
including historical articles without per-article ISSN metadata. Different
historical journal identities remain separate. The command also matches stored
article ISSNs when the journal has not yet been registered.

Without `--out`, the filename is `netvault-<ISSN>.csv` in the current directory.
If it already exists, the command automatically chooses `netvault-<ISSN>-2.csv`,
then `netvault-<ISSN>-3.csv`, and so on. No output filename is required.
Use `--force` without `--out` to replace `netvault-<ISSN>.csv`, or with `--out`
to replace that explicit path. The command validates the complete CSV
before saving; a failed or interrupted export preserves an existing destination.
Run `nv login` first. Invalid ISSN checksums and unknown identifiers are reported
as errors; a registered journal with no active PDFs produces a header-only CSV.

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

### Editable journal filters

Type a journal name or initials in the dashboard filter. Initials omit function
words: `IJIR` matches International Journal of Intercultural Relations. Results
update locally 200 ms after typing stops; Enter applies immediately. Right-click
a journal name, or press Shift+F10 while it is focused, and select **Open in Web
of Science** to search its full publication name in a new tab.

The dashboard includes All, UTD24, FT50, ABS 4*, ABS 4, ABS 3, ABS 2, ABS 1,
and Custom filters. Custom is always shown last.

- Click a filter to show PDF counts and the journal-year heatmap for that list.
- Right-click any list filter to edit its journal names. On touch screens, tap
  the ellipsis inside the filter.
- Enter one journal per line and save. Changes are private to the signed-in user.
- Standard lists can be reset to their bundled defaults at any time.
- Custom starts empty and is intended for a user's own watched journals.

Journal matching is case-insensitive and treats punctuation, a leading “The”,
and “&” versus “and” as equivalent. The default FT50 list is sourced from the
[CEIBS Library FT50 guide](https://ceibs.libguides.com/c.php?g=963339&p=7006421).

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
