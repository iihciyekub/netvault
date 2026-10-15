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
nv login https://iiaide.com/nv --username example-user --password example-password
```

Replace the example username and password with your own credentials. Run
`nv login` without parameters to enter them when prompted.

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
the account, or resetting its password invalidates browser and CLI sessions.
`nv logout` and browser Logout both revoke all existing sessions for the account.

Ordinary upload resolution starts with parsed PDF Document Info. A unique DOI
supplies the identity before cached values, download indexes, `--doi`, filenames
or page text. Multiple different Document Info DOIs stop resolution as a conflict.
XMP is never used for DOI identification or modified; automatic writeback only
compares its raw bytes for preservation. Only with no Document Info DOI does the existing
resolver use DOI hints, annotation links, filename patterns and the first three
pages through Poppler and pypdf. That fallback retains server metadata/title
verification. See the [PDF DOI metadata policy](pdf-doi-policy.md) for the shared
rules and local read/write commands.

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
nv identify ~/Downloads/papers --rename --report ~/Downloads/papers-report.csv
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
in the scanned directory. Use `--report PATH.csv` for a different new report
(`--csv` remains a compatible alias).
Without `--rename`, only the preview CSV is written. File errors or unresolved
filename conflicts produce exit status 1.

## Upload PDFs

Uploads use three concurrent requests by default, with DOI identities prepared
in batches of 12 while earlier files upload. Hash checks, DOI verification and
idempotency remain enabled. Force replacements run serially in input order.
Use `nv upload ./papers --jobs 1` to diagnose a connection with serial requests;
`--jobs` accepts 1 through 3 and requires no change to the usual command.

Default output contains concise result counts, elapsed time and the report path.
Add `--verbose` for aggregate multipart bytes, average MiB/s, DOI/cache statistics
and detailed send, response-wait and server timings. Timings overlap under
concurrency and do not sum to total elapsed time.


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

NetVault only accepts PDF files. Ordinary uploads leave local PDF bytes
unchanged. On an ordinary upload it:

1. Checks the file is a PDF.
2. Computes sha256 locally.
3. Checks server hashes before extracting DOI text or sending PDF bytes.
4. Reuses directory state and skips unchanged confirmed identity rejections.
5. Reads Document Info for remaining files: uses its unique DOI, stops on a
   metadata conflict, or runs the existing hint/content resolver when no DOI is
   embedded. The server independently applies the same identity rule.
6. Stores a new PDF, or registers an accepted alternate hash for an existing DOI.
7. Stores metadata in PostgreSQL and commits each local upload outcome.

Run the same `nv upload` command to continue an interrupted or repeated upload.
Keep `.netvault-upload.sqlite3` with each PDF directory when copying it; no extra
command or option is needed. New files, changed evidence and temporary errors can
be processed again. Per-run reports can be removed without losing directory state.

NetVault caches SHA-256 values in `~/.config/netvault/hash-cache.json` and DOI
identity results in `~/.config/netvault/identity-cache.json`. The identity cache
is keyed by SHA-256 rather than path, so moving or renaming an unchanged PDF
does not trigger another DOI scan. Automatic missing/conflict results are also
cached; pass `--refresh-doi` when you want to retry them.
Cached hints never override a unique Document Info DOI or resolve conflicting
Document Info values.

Upload uses one progress bar with a fixed total of files requiring processing.
It advances on final results; existing hashes and local duplicates are summarized
before this stage. Individual errors and index warnings are saved in the atomic
`nv-upload-report.json`; `--report PATH` changes its location. Default output
contains result counts and elapsed time; `--verbose` adds individual errors and
technical diagnostics.

Recognized server DOI/title rejections are recorded as `needs-review` and remembered
with SHA-256 and server address in a `.netvault-upload.sqlite3` ledger beside each PDF. The ledger commits each outcome and remains independent of the global identity cache and per-run report. Normal repeated uploads
skip them as `review-skipped` without parsing or sending the same PDF. Moving or
renaming without a new DOI hint preserves this behavior. Changed PDF bytes,
DOI/index/CSV evidence, filename DOI hints or resolver rule changes allow another attempt. Ordinary CLI/server release upgrades preserve rejections. Server
hash matches take precedence. Local missing DOI results still get a server attempt;
network errors, provider outages and unrelated request validation are not remembered
as identity rejections.

```bash
nv upload ./papers --refresh-doi
nv upload ./papers --retry-failed
nv upload ./papers --verbose
```

`--retry-failed` selects failed, pending, `needs-review` and `review-skipped` files
from directory state, with the chosen report as a fallback for legacy records. Both retry options bypass remembered rejections; explicit
force uploads also bypass them. Success clears a remembered rejection. Fresh errors
or rejections exit with status 1; skipping remembered review files alone exits with
status 0. Use `--refresh-doi` after server verification rules change. PaperDock uses a private versioned interface to read the same directory state and query server DOI/hash presence; the public CLI command set is unchanged.

### Automatically write an independently verified DOI

Opt in to supplementing missing Document Info DOI metadata before upload:

```bash
nv upload ./papers --write-doi --dry-run
nv upload ./papers --write-doi
```

The dry run is a local preview only. It does not contact the server or Crossref,
change PDFs, or establish that a candidate DOI is verified. The live command
cannot be combined with `--csv`, `--no-crossref` or `--doi`.

For each eligible PDF without an Info DOI, the server independently examines the
original bytes using authenticated `POST /pdfs/verify-doi`. This temporary
verification does not create a vault record or hash alias. The returned evidence
is bound to the original SHA-256. Automatic writing requires all of these:

- An article DOI printed explicitly on the first page, excluding references,
  download URLs and journal/issue/volume DOIs.
- An exact Crossref lookup whose canonical DOI matches the printed DOI.
- A first-page article title matching the Crossref title with similarity of at
  least `0.98`, plus independent first-page author and publication-year evidence.
- Exactly one candidate satisfying all checks.

Only Crossref article records qualify. Title search alone, DOI.org/Semantic
Scholar results, filenames, cache/index hints, explicit CSV pairs and a successful
upload cannot authorize a write. A Crossref record existing by itself does not
prove it belongs to the PDF. The rule is conservative and high confidence;
preprint/version ambiguity and incomplete evidence still need review.

An existing unique Info DOI is not rewritten and follows ordinary upload rules,
including signed PDFs with an existing DOI. For missing-DOI files, encryption,
signatures, a nonempty invalid DOI field, conflicts or insufficient evidence keep
the source unchanged and prevent upload for that file in this invocation.
Verification/provider errors are reported as failures or review outcomes;
they never trigger an unverified write or upload of the original instead.

Before a verified write, NetVault creates a byte-for-byte backup beside the
source: `filename.pdf.nv-doi-<original-sha256>.bak`. It verifies the source hash,
creates and validates an incremental temporary PDF preserving the original-byte
prefix and page structure, rechecks the source hash and atomically replaces the
source. A safety-check or write failure leaves the source unchanged and stops
that file's upload. The order is verification, backup, validated write, new
SHA-256, then upload. Per-file report entries include verification, original and
resulting hashes, and the backup path.

Ordinary deduplication still retains an existing canonical server PDF for the
same DOI, even if this local PDF now has metadata and different bytes. It may
register the new hash as an alias; downloads can return the retained server
copy. Add `--force` to request canonical replacement, which continues to require
successful registry metadata. See the [complete policy](pdf-doi-policy.md).

### Download index files

Indexes are fallback evidence only when Document Info has no DOI.
Automatically discovered indexes skip invalid entries with one warning and use
independent PDF extraction for those files. Valid SHA-bound entries remain usable.
In this fallback path, an explicitly supplied `--index-file` remains strict;
stale hashes still fail.

If Document Info has no DOI and the PDF's immediate directory contains
`pdf-download-index.json`, NetVault can use the downloader's DOI record as a hint
before page-text extraction. The JSON
file must use version 1, declare `SHA-256`, and contain a `records` array. Each
record contains `doi`, `filename`, `size`, `lastModified`, `sha256`,
`downloadedAt`, `sourceUrl`, and a `validation` object with `status`, `checkedAt`,
`method`, and `reason`.

NetVault still computes the PDF digest itself. It uses an index DOI only when the
actual digest and size match and `validation.status` is `valid`. Files absent
from the index use the normal DOI resolver. Automatically discovered stale
records are skipped with a warning; stale records in an explicit `--index-file`
fail. `--no-index` disables download indexes without disabling Document Info.

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

Explicit `--csv` selects a separate trusted import mode and accepts matched
DOI/PDF pairs without resolving Document Info DOI values, reading PDF text,
extracting DOI candidates, or verifying PDF article titles. It requires a unique
SHA-256 match or filename and byte-size match. CSV rows must contain `file` (or
`filename`), `doi`, `bytes` (or `size`), and `status=downloaded`; optional `sha256`
supports renamed files. Unmatched or ambiguous files fail without PDF fallback,
even when a cached DOI is available. This trust applies only to this invocation;
it is not stored in the local DOI cache. `--csv` cannot be combined with `--doi`,
`--index-file`, `--no-index`, or `--write-doi`.

PDF signature/completeness checks, upload size limits, deduplication, and existing
hash/DOI conflict checks still apply. Metadata lookup continues unless disabled,
but missing or unavailable metadata does not prevent an ordinary CSV upload.
CSV assertions are recorded as `csv-trusted`, including aliases for another file
with the same DOI. `--force` still requires available DOI metadata before replacing
the stored file. A CSV discovered automatically beside the PDFs (or at the selected
folder root) remains a `download-record` hint used only when Document Info has no
DOI and still requires PDF title verification. Use explicit `--csv` even when the CSV is inside the upload folder
if you want to trust its pairs.

When the server verifies an existing DOI for a PDF with different bytes, or accepts
an explicit CSV pair, it records the new SHA-256 as an alias of the canonical item. This handles
publisher copies, repository copies, and regenerated PDFs across devices without
repeating DOI extraction on subsequent uploads.

Recursive uploads prune common technical directories (`.git`, `.venv`,
`node_modules`, `Library`, `dist`, `build`, and `output`). Use repeated
`--exclude-dir NAME` options for additional project-specific directories. If
you explicitly pass an excluded directory as an upload path, NetVault scans it.
Directory-name exclusions ignore case at every depth. `unidentifys/` means absent
from the vault, not rejected by upload, and remains uploadable by default. To
intentionally omit identify collection folders:

```bash
nv upload ./papers --exclude-dir unidentifys --exclude-dir duplicates
```

If Document Info has no DOI and automatic resolution fails, provide a DOI
candidate for server verification:

```bash
nv upload ~/Downloads/paper.pdf --doi 10.1016/j.ijpe.2018.04.006
```

Replace the PDF and all stored metadata for an existing DOI with `--force`:

```bash
nv upload ~/Downloads/replacement.pdf --force
```

Add `--doi DOI` when Document Info has no DOI and automatic resolution is not
possible. It cannot override a unique embedded DOI or clear an embedded
conflict. Replacement is available to every authenticated user. NetVault requires
successful current DOI metadata retrieval before it replaces the canonical PDF, clears old file
aliases, and overwrites the title, authors, journal, publisher, year, and URL.
`--force` cannot change DOI identity. If a file was registered under an incorrect
DOI, an administrator must correct that record before replacing its PDF.

The server independently reads Document Info. Without an embedded DOI, automatic
DOI claims and `--doi` hints retain server metadata/title verification. If a
publisher download URL contains a DOI-like path that conflicts with printed
article evidence, inspect the resolver output before supplying a candidate:

```bash
nv update
nv doi ~/Downloads/paper.pdf --verbose
nv upload ~/Downloads/paper.pdf --doi 10.1016/j.ijpe.2018.04.006
```

For a PDF with no Document Info DOI whose identity you have verified, save a
user-confirmed local hint before upload:

```bash
nv doi ~/Downloads/paper.pdf --set 10.1016/j.ijpe.2018.04.006
nv doi ~/Downloads/paper.pdf --show-cache
nv upload ~/Downloads/paper.pdf
```

The cached confirmation is bound to the PDF SHA-256 and survives renames. It does
not change PDF bytes or receive the embedded metadata shortcut; a unique Document
Info DOI or embedded conflict takes priority. Use `nv pdf-meta write` to modify
the PDF itself. If a cached assertion is wrong, remove it with:

```bash
nv doi ~/Downloads/paper.pdf --remove
```

To skip online DOI/title verification and metadata lookup:

```bash
nv upload ~/Downloads/paper.pdf --no-crossref
```

This leaves Document Info precedence and conflict handling active. Ordinary
uploads with a unique embedded DOI can also proceed when registry metadata is
missing or temporarily unavailable. `--force` retains its successful-metadata
requirement and cannot be combined with `--no-crossref`.

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

## Read and Write PDF DOI Metadata

Inspect embedded Document Info offline without page-text extraction:

```bash
nv pdf-meta read ~/Downloads/paper.pdf
nv pdf-meta read ~/Downloads/paper.pdf --json
```

Write the DOI into `/DOI`, or save a separate PDF while preserving the original:

```bash
nv pdf-meta write ~/Downloads/paper.pdf --doi 10.1234/example.doi
nv pdf-meta write ~/Downloads/paper.pdf --doi 10.1234/example.doi \
  --output ~/Downloads/tagged-paper.pdf
```

The writer uses a validated pypdf incremental append and atomic replacement.
Original bytes remain a prefix; pages and unrelated Document Info fields are
preserved. XMP is untouched. Optional `--title`, `--author`, `--subject` and
`--keywords` update those Document Info fields. A different existing embedded DOI
requires explicit correction:

```bash
nv pdf-meta write ~/Downloads/paper.pdf --doi 10.1234/corrected.doi --replace
```

`--replace` updates conflicting DOI strings in eligible Document Info fields and
sets `/DOI`. An existing `--output` destination is refused. Validation failures
leave the original unchanged. A changed PDF has a new SHA-256. Ordinary uploads
can retain an existing same-DOI server copy through deduplication; use
`nv upload ~/Downloads/paper.pdf --force` to request replacing that stored copy.
Repeating an already-current DOI write in place without optional field changes
is a no-op. This offline command records the supplied DOI without independent
article-identity verification; `nv upload ./papers --write-doi` performs the
stricter automatic verification and missing-DOI write workflow described above.

## DOI Resolution

Document Info is authoritative for ordinary uploads: normalize and deduplicate
DOIs from string fields such as `/DOI`, `/Subject` and `/Keywords`. A unique value
is the identity, even when other hints disagree; multiple different values stop
resolution as a conflict. The client and server skip page DOI extraction and
title/content verification for that unique identity.

Crossref is queried for that exact DOI. The existing DOI.org/Semantic Scholar
provider fallback may supply metadata for the same identity. Missing or
unavailable metadata does not select another DOI or block an ordinary upload
with a unique embedded DOI. `--force` still requires successful metadata
retrieval before replacing a stored file.

Only when Document Info has no DOI does the existing smart resolver consider
explicit `--doi`, cached identities, matched download records, filename DOI
patterns, DOI links and the first three pages. Reference-list DOI candidates are
down-ranked. Fallback candidates retain server registry/title/content checks;
failed candidates can fall back to other evidence or bounded title lookup. XMP
and arbitrary raw PDF object strings are excluded.

PDFs without a DOI are rejected. This keeps the vault DOI-centered.

Inspect how NetVault will resolve a DOI before upload:

```bash
nv doi ~/Downloads/paper.pdf
nv doi ~/Downloads/paper.pdf --verbose
nv doi ~/Downloads/paper.pdf --show-cache
```

If Document Info has no DOI and the fallback resolver is wrong or ambiguous,
provide an explicit candidate:

```bash
nv upload ~/Downloads/paper.pdf --doi 10.1016/j.ijpe.2018.04.006
```

Online explicit DOI values are still verified against registry metadata and available PDF text;
they do not override embedded metadata. To correct a unique embedded DOI or
conflicting embedded values, use `nv pdf-meta read` and `nv pdf-meta write
--replace`. See the [complete policy](pdf-doi-policy.md) for the explicit trusted
CSV import exception and the responsibilities of each command.

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

The CSV columns are `doi,sha256`, with the current retained hash and accepted
alias hashes for each active PDF, ordered by DOI and hash. Use `--primary-only`
for one retained hash per PDF. There is no pagination limit. Print and
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

Click a populated journal-year heatmap cell to open its PDFs in the existing
Search page. Journal identity and publication year are matched exactly using the
same journal key as the dashboard. Active filters stay visible and are retained
when paging or searching within the results. **Clear** returns to ordinary search.

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

The PDF has no usable Document Info DOI or fallback identity. Inspect it with
`nv pdf-meta read FILE` and `nv doi FILE --verbose`; supply a verified `--doi`
candidate or embed the correct DOI with `nv pdf-meta write FILE --doi DOI`.

`DOI conflict`

Run `nv pdf-meta read FILE` first. Multiple different Document Info DOI values
must be corrected with `nv pdf-meta write FILE --doi DOI --replace`; `--doi`
and cached hints cannot override that conflict. If Document Info has no DOI,
inspect fallback candidates with `nv doi FILE --verbose` and supply a verified
`--doi` candidate when needed.

`PDF not found for DOI`

No active PDF is currently registered under that DOI, or an administrator soft-deleted it.

`command not found: nv`

Install the CLI, or ensure your tool install directory is in `PATH`.
