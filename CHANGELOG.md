# Changelog

## 0.7.25 - 2026-10-11

- Upload with three bounded concurrent requests by default, preparing DOI identities
  in small batches while earlier uploads are in flight; keep force replacements serial.
- Show aggregate sent bytes, average throughput, send/response-wait timings, and
  server storage, PDF parsing, verification and persistence timings.
- Reuse extracted PDF text for DOI title verification and run blocking PDF parsing
  and title matching outside the server event loop.
- Serialize same-DOI lookup/create decisions across distinct hashes, preserving
  verification, hash idempotency and duplicate/alias behavior under concurrent uploads.
- Share metadata concurrency and request pacing across server request handlers, preserving
  provider retries and Retry-After handling. Use `--jobs 1` for serial diagnostics.

## 0.7.24 - 2026-10-10

- Identify equivalent complete PDF page content even when byte hashes and registered
  DOIs differ, retaining one copy and moving extras to `duplicates/` with `--rename`.
- Compare decoded page objects/resources while ignoring document metadata and
  verified standalone Wiley download notices; preserve differences in body text,
  fonts, images, annotations, page geometry/order, and optional-content settings.
- Record `moved_content_duplicate` (or its preview status), page-content fingerprints,
  and retained file paths/hashes in CSV while preserving each file's original DOI.
- Keep unsupported PDFs on the existing hash/DOI rules and verify retained files
  before collecting content duplicates, including across nested directories.

## 0.7.23 - 2026-10-10

- With `nv identify --rename`, also collect same-DOI filename conflicts with
  different hashes into `duplicates/`, preserving the existing DOI-named PDF.
- Distinguish these moves as `moved_doi_conflict` (or `would_move_doi_conflict`
  in previews), with the retained file's path and local hash in the CSV.
- Keep byte-identical copies marked `moved_duplicate`, and preserve files when
  different DOIs collide after filename conversion or a conflict reference changes.

## 0.7.22 - 2026-10-10

- Add `nv identify` to batch-match local PDF SHA-256 values against the server,
  rename matched files by DOI, and export an auditable CSV with original/final paths.
- With `--rename`, automatically collect identical extra copies into `duplicates/`
  and unmatched retained PDFs into `unidentifys/` under the command's current directory.
- Exclude both collection directories from repeated scans, preserve existing files
  and reports, and verify copied hashes before completing cross-filesystem moves.
- Handle alternate server hashes, DOI filename conflicts, and long Unicode filenames
  while keeping CLI and server release versions synchronized.

## 0.7.21 - 2026-10-05

- Publish a version-only maintenance release with synchronized CLI and server
  package versions, retaining the DOI upload and journal dashboard fixes from 0.7.20.

## 0.7.20 - 2026-10-05

- Ignore invalid entries in automatically discovered download indexes, warn once, and
  independently resolve affected PDFs instead of failing an entire upload batch.
- Send missing DOI hints without an invalid source flag, exclude journal/container
  DOIs, and invalidate older automatic DOI caches.
- Recover absent or obsolete printed DOIs through Crossref title search only when
  article title, journal, publication year and author match the PDF unambiguously.
- Filter dashboard journals by initials that omit function words, with 200 ms
  debouncing, and add an accessible right-click Open in Web of Science menu.

## 0.7.19 - 2026-10-05

- Persist browser logins for 400 days and renew authenticated visits, upgrading valid
  existing sessions while preserving logout and account revocation.
- Strengthen PDF DOI extraction with decoded compressed XMP, page-level error
  recovery, conservative wrapped-token repair, and the strongest available evidence.
- Retry unsuccessful local DOI scans and send PDFs without a local DOI to the server
  for independent extraction and metadata/title verification.
- Include Poppler in the server image so the alternate PDF text parser is available
  in production, with matching CLI/server regression coverage.

## 0.7.18 - 2026-09-28

- Treat verified uploads for an already stored DOI as successful skips instead of upload
  failures, while recording alternate SHA-256 values as auditable aliases without storing
  duplicate PDF bytes.
- Make automatic DOI alias registration idempotent and persist its source and asserting user,
  preventing PostgreSQL NOT NULL and duplicate-alias errors.
- Report server-confirmed duplicates in `nv upload` as `already stored: N skipped` and keep
  alias-persistence optimization failures out of the normal user-facing upload result.

## 0.7.17 - 2026-09-19

- Harden DOI Resolver v4 against PDF object-syntax contamination such as `)>>/Border`,
  preserve punctuation-significant DOI identity, structurally inspect DOI annotation links,
  and restrict raw-PDF fallback to explicit DOI labels and DOI.org URLs.
- Verify online DOI identities with Crossref plus a limited DOI.org metadata fallback,
  and apply the same PDF-title verification to explicit and download-index DOI claims.
- Add administrator `nv correct-doi` and `nv doi-audit` workflows that correct DOI identity
  and refresh metadata without replacing PDF bytes, SHA-256 aliases, or upload/download history.
- Add the Web `Correct DOI` preview/apply/history workflow with admin/CSRF protection,
  title-mismatch confirmation, duplicate-target checks, and stale DOI/SHA guards.

## 0.7.16 - 2026-07-22

- Resolve automatic DOI candidates in filename, PDF metadata, and first-three-page order,
  verify each candidate with Crossref and normalized PDF-title matching, and fall back instead
  of rejecting stale client hints.
- Preserve canonical DOI values returned by Crossref, read PDF Document Info DOI values, and
  invalidate resolver-version-2 automatic identity cache entries.

## 0.7.15 - 2026-07-20

- Harden the standalone CLI installers for macOS, Linux, and Windows with retrying
  downloads, temporary-file cleanup, release-wheel pinning, PATH fallbacks, and
  installed-command and version verification, without shipping server or web components.
- Clarify in the journal-list editor that lists are saved to the signed-in account and
  remain available across devices, with regression coverage for session persistence and
  account isolation.

## 0.7.14 - 2026-07-15

- Collapse local PDF copies with the same SHA-256 before DOI resolution and upload,
  and report found paths, unique PDFs, local duplicates, server skips, and uploads separately.

## 0.7.13 - 2026-07-15

- Pin CLI installs and updates to the latest published GitHub release, enforce one
  version across CLI/server metadata and lockfiles, and automate tested tag/release
  publication with attached source and wheel artifacts.
- Prevent publisher PDF download URLs from overriding a standalone labeled DOI,
  preserve valid DOI suffixes containing additional slashes, and invalidate stale
  automatic DOI cache entries with resolver version 2.
- Revalidate automatic client DOI claims on the server, require confirmation for
  unverified publisher-URL candidates, and show related malformed identifiers when
  an exact DOI search has no match.
- Add dry-run-capable, SHA-guarded administrator DOI correction with Crossref
  verification and a durable correction audit trail that preserves PDF IDs and history.
- Keep dashboard vault totals independent of journal filters, move filtered PDF counts
  beside the journal count, and replace visible Show/Sort labels with accessible icons.

## 0.7.12 - 2026-07-13

- Accept registered DOI suffixes containing ampersands during extraction and
  download-index validation.

## 0.7.11 - 2026-07-13

- Let `nv upload` use SHA-256-bound DOI records from a sibling version 1
  `pdf-download-index.json`, with strict stale-index detection and PDF parsing fallback.
- Add configurable download-index names plus explicit `--index-file` and `--no-index`
  controls, and preserve the `download-index` DOI source in server audit data.

## 0.7.10 - 2026-07-12

- Add authenticated `nv upload FILE --force` replacement for every user.
- Atomically replace the canonical PDF and refresh all stored metadata from the
  current Crossref record, while preserving the original when Crossref is unavailable.
- Move journal selection into the custom-list editor, autosave the heatmap limit,
  persist sorting locally, enlarge the legend, and refresh the UTD24 journal names.

## 0.7.9 - 2026-07-11

- Add the CEIBS FT50 journal list and a per-user Custom journal filter.
- Make UTD24, FT50, and every ABS journal list privately editable from the
  dashboard by right-click, keyboard context menu, or the touch edit control.
- Persist user-specific journal lists in the database, support restoring
  standard defaults, and isolate filtered statistics and caches by user.
- Refine PDF action groups, mobile download results, and heatmap alignment.

## 0.7.8 - 2026-07-11

- Cache DOI identity results by PDF SHA-256, including user-confirmed identities
  and automatic missing/conflict results, with explicit inspection and refresh controls.
- Skip common technical directories during recursive uploads and avoid expensive PDF
  text extraction when a filename provides unambiguous DOI evidence.
- Register DOI-confirmed alternate PDF digests as server-side aliases so equivalent
  publisher, repository, and regenerated copies are recognized across devices.

## 0.7.7 - 2026-07-11

- Add a local `nv check-pdfs` command that recursively finds PDFs that cannot
  be opened and moves them into a collision-safe `./error` directory.
- Keep encrypted PDFs, ignore non-PDF files, skip the error directory itself,
  and support a non-mutating `--dry-run` check.

## 0.7.6 - 2026-07-11

- Add authenticated, on-demand PDF previews to web search and DOI match results.
- Stream previews inline with byte-range support without consuming download audit counts.
- Keep preview traffic independently rate-limited and open documents safely in a new tab.

## 0.7.5 - 2026-07-10

- Make the journal-year heatmap denser and right-align journal names.
- Format dashboard PDF counts with thousands separators.
- Show distinct, measurable upload preflight stages for server checks, local
  DOI extraction, and DOI duplicate checks.

## 0.7.4 - 2026-07-10

- Correct the published shared CLI credentials and keep the password
  shell-safe in the complete copyable login command.

## 0.7.3 - 2026-07-10

- Show the shared username and password directly in the CLI login command and
  make the copy action include the complete non-interactive login command.

## 0.7.2 - 2026-07-10

- Make live incremental backups ignore transient upload, lock, and quarantine
  files, automatically remove incomplete snapshots, and only link against
  previously verified backup manifests.

## 0.7.1 - 2026-07-10

- Raise the default authenticated upload allowance to 5,000 files per hour so
  legitimate high-volume research imports are not interrupted.

## 0.7.0 - 2026-07-10

- Add PostgreSQL trigram search, active-list, journal-year, upload-log, and
  download-log indexes with normalized journal keys.
- Route exact DOI searches through the unique DOI index and collapse web count
  plus result retrieval into one query.
- Upload web files independently with bounded client hashing, two-file
  concurrency, cancellation, DOI batch preflight, PDF completeness validation,
  and idempotent audit records.
- Add resilient Crossref connection pooling, polite-pool identification,
  transient retries, and metadata retry for previously unavailable PDFs.
- Stream stored ZIP archives directly to clients and record download audits
  after response completion.
- Add full-journal pin restoration, keyboard-accessible heatmap cells, mobile
  utility navigation, and short-lived PJAX caching.
- Remove the shared password from web/CLI examples and add authenticated action
  limits, request IDs, timing headers, HSTS, private response caching, storage
  free-space readiness, Docker resource limits, and log rotation.
- Add incremental backup, guarded restore, and storage-consistency scripts.

## 0.6.2 - 2026-07-10

- Add a persistent local journal pin list for focused heatmap views.
- Simplify heatmap point tooltips to year and PDF count with an icon.
- Refine CLI timeline spacing, alignment, and light syntax highlighting.
- Correct Crossref verification seal alignment.

## 0.6.1 - 2026-07-10

- Add self-hosted Font Awesome icons throughout the web interface.
- Show Crossref metadata and verification status in responsive search results.
- Add fast, debounced, case-insensitive journal filtering to the heatmap.
- Improve the Info page declaration, acknowledgement, layout, and author display.
- Remove unnecessary page focus outlines and tighten dashboard spacing.

## 0.6.0 - 2026-07-10

- Stage uploads until DOI validation succeeds and clean failed uploads.
- Verify downloaded PDFs with SHA-256 and validate resumed HTTP ranges.
- Revoke tokens on logout and password reset with per-user token versions.
- Rate-limit repeated login failures and strengthen password/user validation.
- Add API pagination, batch limits, readiness checks, and security headers.
- Improve web headings, labels, keyboard navigation, and focus handling.
- Add concise CLI failures, partial-failure exit codes, JSON output, pagination,
  HTTP retries, and summary-based status reporting.
- Add versioned schema migrations and Python 3.11/3.12 CI checks.
