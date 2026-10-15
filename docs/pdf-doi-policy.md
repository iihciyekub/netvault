# PDF DOI Metadata Policy

This is the shared policy for the NetVault CLI, API and web upload flow. PDF
identity and registry enrichment are separate decisions: Document Info can
establish the DOI/PDF identity even when registry metadata is unavailable.

## One embedded metadata source

Parsed PDF **Document Info** is the only authoritative embedded metadata source.
The resolver normalizes DOI strings found in string fields such as `/DOI`,
`/Subject` and `/Keywords`, and deduplicates equivalent values. A DOI URL and its
equivalent plain DOI represent one identity. Fields explicitly describing a
journal, issue or volume DOI are excluded from article DOI evidence.

XMP is not parsed for DOI identity and is never modified. Automatic writeback
compares its raw bytes only to verify preservation. Raw PDF objects,
annotation links, page text and filenames are not Document Info. They cannot
receive its authoritative identity shortcut.

| Document Info result | Ordinary upload behavior |
| --- | --- |
| Exactly one normalized DOI | Use that DOI before caches, indexes, `--doi`, filenames or page text. Request metadata for that exact DOI and bind the PDF to the same identity. Skip page DOI extraction and title/content verification. |
| Multiple different DOIs | Stop with a DOI conflict. Do not choose `/DOI` over a conflicting `/Subject`, trust another hint, or guess from page text. Correct the PDF metadata explicitly before retrying. |
| No DOI | Continue the existing hint/content resolver, including local cached identities, explicit `--doi`, matched download records, page DOI evidence, annotation links, filename candidates and bounded title lookup. Their existing verification rules remain active. |

The server parses Document Info independently; a client's `pdf-metadata` label
alone does not establish this trust. Existing PDF integrity, size, authentication,
deduplication and DOI/hash assignment checks still apply. An already-known hash
can be skipped before another upload. Document Info establishes an asserted
identity; it does not prove authorship or authenticity.

## Crossref enriches the selected identity

For a unique Document Info DOI, query Crossref by that exact normalized DOI.
Successful metadata for that identity supplies title, authors, journal and other
article fields, and the PDF is associated with the same DOI. The existing
DOI.org citation-metadata and DOI-indexed Semantic Scholar provider fallbacks may
enrich the record for the same DOI when Crossref has no record.
Registry responses must not change an authoritative Document Info DOI to another
identity.

If the DOI is absent from the provider, a response is invalid, or the provider is
temporarily unavailable, ordinary uploads retain the embedded DOI without
claiming successful registry enrichment. Do not search by title, scan the paper
for another DOI or use an index/cache candidate to replace it. Metadata lookup
failure and DOI identity conflict are different outcomes.

`--no-crossref` skips online verification/enrichment; it does not change Document
Info precedence or ignore conflicting embedded DOI values. `--force` is a
separate replacement operation and continues to require successful metadata
retrieval before committing the replacement; it cannot be combined with
`--no-crossref`.

Only when Document Info has no DOI may the existing resolver try other DOI
candidates or bounded title search. That path retains registry/title/content
checks and its existing pending or review outcomes. Reference-list DOIs remain
lower-confidence evidence, and a filename does not override verified PDF
evidence.

## Hints, indexes and the explicit CSV exception

`nv upload paper.pdf --doi DOI` supplies a candidate for the fallback path; it is
not a trusted override of embedded metadata. `nv doi paper.pdf --set DOI` saves
only a local identity-cache entry. It does not modify the PDF, gain the Document
Info shortcut, override a unique embedded DOI, or clear an embedded conflict.

Automatic `cli-downloads.csv` and SHA-256 download indexes are fallback evidence.
They are consulted only after Document Info has no DOI and retain server
verification. `--no-index` disables index discovery, not metadata reading.
Unusable automatically discovered records are skipped with a warning. An
explicit `--index-file` remains strict when used in the fallback path. Cached
identities are likewise subordinate to a unique embedded DOI or embedded
conflict; `--refresh-doi` refreshes fallback results and is not a way to override
Document Info.

Explicit `nv upload ./papers --csv ./cli-downloads.csv` selects a separate,
user-trusted import mode. The matched DOI/PDF pairs establish the requested
identity without PDF DOI resolution or title/content verification, including
scanned PDFs. Unmatched, ambiguous or conflicting CSV records fail without
falling back to embedded metadata or guessed DOI values. CSV trust applies only
to that invocation and is not persisted as a trusted cache entry. File integrity,
deduplication and existing DOI/hash assignment conflicts still apply. Ordinary
CSV imports may proceed without registry enrichment; `--force` retains its
metadata-success requirement. `--csv` cannot be combined with `--doi`,
`--index-file`, `--no-index` or `--write-doi`.

## Independently verified automatic DOI writing

Automatic writing is opt-in:

```bash
# Offline preview only: no server, Crossref, PDF writes or validation claim.
nv upload ./papers --write-doi --dry-run

# Independently verify eligible files, append /DOI, then upload.
nv upload ./papers --write-doi
```

Ordinary `nv upload` does not change local PDF bytes. `--write-doi` cannot be
combined with `--csv`, `--no-crossref` or `--doi`. Its stricter write decision is
separate from ordinary upload identity selection: accepting an existing unique
Document Info DOI is not permission to manufacture that evidence in another PDF.
Crossref finding a DOI proves that a registry record exists, not that the DOI
belongs to this PDF.

Only a PDF with no Document Info DOI is eligible for automatic supplementation.
Before writing, the CLI submits the original bytes to authenticated
`POST /pdfs/verify-doi`. This endpoint temporarily inspects the PDF without
creating a vault item or registering a hash alias. The server derives its own
evidence and binds the result to the original SHA-256. All of these requirements
must be met:

- The first page explicitly prints an article DOI. Reference-list DOIs, download
  URL paths, and journal, issue or volume DOIs do not qualify.
- An exact lookup returns primary Crossref metadata with the same normalized
  canonical DOI and type `journal-article` or `proceedings-article`.
- The Crossref title matches the first-page article title with similarity at
  least `0.98`, and both an author and the publication year are independently
  corroborated on that page.
- Exactly one candidate passes these checks.

Title search alone cannot supply a DOI for automatic writing in this release.
DOI.org or Semantic Scholar metadata, filenames, cached identities, download
indexes, CSV pairs, and successful uploads are insufficient write evidence.
Provider failures and version ambiguity, including preprint/published-version
ambiguity, require review. This is a conservative high-confidence rule, not a
mathematical guarantee of article identity.
Correction, erratum, retraction, commentary, reply and manuscript-version notices
in the heading also require review; an original article's DOI must not be
automatically assigned to a related document. Author evidence must come from a
separate byline, not a surname appearing in the title or journal name.

| Input or result | `--write-doi` behavior |
| --- | --- |
| Existing unique Document Info DOI | Leave bytes unchanged and follow the ordinary upload identity policy; signed files with an existing DOI are not rewritten. |
| Conflicting DOI values or a nonempty invalid DOI field | Preserve the file and report the need for review; do not overwrite or upload it in this invocation. |
| Encrypted or signed PDF without an existing usable DOI | Preserve the file and report that automatic writing cannot proceed; do not upload it in this invocation. |
| Missing DOI with insufficient, ambiguous or unavailable verification | Preserve the file, do not upload it, and report review or verification failure; do not claim that a DOI was verified. |
| Missing DOI with all checks passed | Safely append the verified `/DOI`, compute the new hash, and use the resulting file for upload. |
| Write or safety check failure | Preserve the source, report the failure, and do not upload that file in this invocation. |

For a successful write, a byte-for-byte backup is created beside the source as
`filename.pdf.nv-doi-<original-sha256>.bak`. The CLI verifies that the source
still matches the verified original hash, writes a same-directory temporary PDF
using a real incremental append, and validates the resulting Document Info,
preserved original-byte prefix and page structure. It checks the source hash
again before atomically replacing it. A failed safety check does not replace the
source. The backup remains available after a successful change.

The order is **independent verification → backup and validated append → atomic
replacement → new SHA-256 → upload**. Per-file entries in
`nv-upload-report.json` record the verification outcome, original and resulting
hashes, and backup path. `--dry-run --write-doi` only previews local eligibility;
it performs no online verification and cannot report a candidate as verified.
The server independently checks the received upload hash against the CLI's final
hash before binding or replacing a file. If upload fails after a successful local
write, the tagged local PDF and original backup remain; retrying retains that
DOI without writing again. Reports are per run; use `--report PATH` to retain
separate reports when an audit trail across runs is needed.

An existing canonical server PDF for the same DOI is still retained by ordinary
deduplication. A locally tagged PDF may be accepted as an alias without replacing
that server file, so a later download need not be byte-for-byte identical to the
local file. Use `--force` to request replacement of the canonical copy; its
successful-metadata requirement still applies.

## Command responsibilities

| Command | Responsibility | Changes the PDF? |
| --- | --- | --- |
| `nv pdf-meta read` | Inspect embedded Document Info DOI values and their sources, offline. | No |
| `nv pdf-meta write --doi DOI` | Append an embedded `/DOI` and optional Document Info field changes, offline. | Yes, unless already current |
| `nv doi` | Inspect resolver evidence or manage a local SHA-bound identity hint with `--set`, `--show-cache` or `--remove`. | No |
| `nv upload` | Apply the identity policy, enrich metadata when enabled, deduplicate and bind/store the PDF on the server. | No local PDF change |
| `nv upload --write-doi` | Independently verify an eligible missing DOI, safely append it locally, then apply upload/deduplication policy using the resulting hash. | Yes, only after all write checks pass; `--dry-run` never writes |
| `nv identify` | Match local SHA-256 values to existing server hashes/aliases or an exported inventory, with optional renaming/organization. It does not infer a new DOI from metadata or page text. | No byte changes; `--rename` can move files |
| `nv check-pdfs` | Check whether PDFs can be opened and optionally move damaged files into `error/`. It does not validate DOI identity or metadata. | No byte changes; damaged files can move |
| `nv correct-doi` | Administratively correct an existing server identity and registry metadata after preview and confirmation. | No byte changes |

## Read Document Info offline

```bash
nv pdf-meta read ./paper.pdf
nv pdf-meta read ./paper.pdf --json
```

The command reads Document Info without page-text extraction or XMP access. JSON
includes `status`, `doi`, `dois`, `document_info` and `doi_sources`:

- `ok`: one normalized DOI; `doi` contains that value.
- `no-doi`: no DOI in Document Info; `doi` is null.
- `conflict`: multiple different DOI values; `doi` is null and `dois` lists them.

Encrypted PDFs must be decrypted before this local inspection or modification.

## Append DOI metadata offline

```bash
# Append a normalized /DOI value to the PDF itself.
nv pdf-meta write ./paper.pdf --doi 10.1234/example.doi

# Preserve the source and create a separate PDF.
nv pdf-meta write ./paper.pdf --doi 10.1234/example.doi --output ./tagged-paper.pdf

# Explicitly correct conflicting Document Info DOI values.
nv pdf-meta write ./paper.pdf --doi 10.1234/corrected.doi --replace

# Optionally update other Document Info fields in the same operation.
nv pdf-meta write ./paper.pdf --doi 10.1234/example.doi \
  --title "Article title" --author "A. Author" \
  --subject "Article subject" --keywords "keyword one; keyword two"
```

Writing uses pypdf's incremental PDF update: the original file bytes remain a
prefix, and changed objects plus the new cross-reference/trailer data are
appended. Existing pages and unrelated Document Info fields are preserved. XMP
is left untouched. This is an actual embedded metadata update, not a sidecar or
local cache record.

The command normalizes the DOI, writes to a temporary file in the target
directory, validates that the result contains exactly the requested Document
Info DOI, flushes it, and atomically replaces the target. Validation failure
leaves the original target unchanged. `--output` refuses an existing destination
and leaves the input unchanged. A conflicting existing DOI requires `--replace`;
that option corrects conflicting DOI strings in eligible Document Info fields
and sets `/DOI`. Repeating an already-current `/DOI` write in place without other
field changes is a no-op.

A successful change alters the PDF bytes and SHA-256. A later upload computes
the new hash. To replace an existing server copy for the same DOI, use
`nv upload ./paper.pdf --force`, which requires successful registry metadata.
An ordinary upload may deduplicate to the retained copy without replacing its bytes.
Writing local metadata alone does not change an existing server record or its
stored file. Metadata-only corrections to an existing server identity use the
separate administrator `nv correct-doi` workflow.
