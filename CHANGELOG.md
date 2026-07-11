# Changelog

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
