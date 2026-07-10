# Changelog

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
