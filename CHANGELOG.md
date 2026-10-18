# Changelog

## Unreleased

## 0.7.83 — 2026-10-18

- Restore the original plain journal-list navigation: remove the unintended UTD24/FT50/ABS colored backgrounds, text overrides and vertical stripes.
- Highlight only authentic Journal rankings **result values**: UTD24, FT50, ABS 4* and ABS 4. Use bold, theme-aware colored text without changing ranking cell backgrounds, unrelated grades or layout.

## 0.7.82 — 2026-10-18

- Place dashboard-wide literature and PDF-size totals on the same line as the heatmap legend and pagination, eliminating the separate footer row.
- Adapt to the actual available pager width: progressively abbreviate explanatory labels, keep counts and page controls visible, and never wrap the row. Retain vault totals when no journal-year data exists.

## 0.7.81 — 2026-10-18

- Add a compact, localized dashboard footer with the full NetVault literature count and the stored PDF file size; values are server statistics for all active PDFs, unchanged by journal-list filters.
- Keep the footer visible beneath the heatmap and ranking panel in responsive dark/light layouts without additional database queries.

## 0.7.80 — 2026-10-18

- Add exact-journal NetVault PDF search to the dashboard journal context menu, with proper URL encoding and site-prefix support.
- Highlight UTD24, FT50, ABS 4* and ABS 4 journal lists with strong accessible theme-aware text; remove the duplicate native hover tooltip over journal names.
- Reduce first-hover rankings render latency by reusing one locale collator and preserve a complete journal-row slot in narrow browser heights.
- Remove the SHA-to-server `nv identify` CLI because server-side PDF recompression changes the file hash; retain `nv tidy` for offline per-directory cleanup.
- Verify that explicit `nv upload --doi` and `nv upload --csv` accept image-only scanned PDFs without comparing the PDF title to CSL, and stamp `/DOI` into the stored server PDF on upload. Local PDF bytes stay untouched.
- Consolidate CLI operation events into `.netvault/history.jsonl` at the selected folder root, with a consistent small schema, append-only writes and interrupted-tail recovery.
- Stop creating default per-command upload/download JSON and tidy CSV reports. Read-only previews create no audit file. Explicit `--report` JSON export remains available for upload.
- Replace active per-folder upload SQLite state with in-memory run caches and replayable journal outcomes. The old SQLite is only read in compatibility mode when recovering old rejected identities or upload states; it is never created, updated or deleted.
- Preserve `nv upload --retry-failed` using JSONL events, keep optional legacy report retry, and log applied tidy mutations and downloads to the shared audit.
- Preserve user DOI mapping CSV and explicit hash inventory CSV as data inputs; do not merge data exchange inputs into the audit.
- Compact upload state at the end of each completed run: successful/known DOI entries disappear, only unresolved pending/error/review items and latest summary remain. Legacy SQLite rejections are cleared safely without writing the old DB; manually confirmed DOI and file-operation audits are retained.
- Persist rejection DOI and resolver context in the JSONL queue, so unchanged failures can be retried or skipped after renames without re-extracting text. Lock append and compaction using the same lightweight file lock.

## 0.7.79 — 2026-10-18

- Add `nv tidy` for offline recursive PDF housekeeping: preview by default, explicit `--apply` to remove confirmed same-parent duplicates and collect DOI-less PDFs in each parent's `unidentifys/`.
- Protect different PDF content sharing a DOI, prevent cross-folder deletion, preserve symlinks and corrupt files, and write an exclusive CSV audit for applied changes.
- Reuse adaptive metadata/pdftotext pages 1-2 DOI scanning, conservative page fingerprints, and safe moves; filenames alone cannot establish a DOI for tidy.
- Exclude `unidentifys/` and `duplicates/` from recursive `nv upload` scans, while retaining direct file uploads and existing `nv identify` behavior.
- Carry forward DOI-first upload and PDFCraft metadata-stamping source improvements; production server deployment remains separate.


## 0.7.78 — 2026-10-17

- Simplify the public README to focus on the `nv` CLI and web management.
- Release the consolidated NetVault CLI, server and web UI as one matching version.

## 0.7.77 — 2026-10-17

- Merge the VPS NetVault functionality with the GitHub 0.7.76 release without replacing remote history.
- Consolidate the journal navigator, custom-list categories/types, PostgreSQL rank-cache prefetch and persistent ranking sidebar.
- Add English, Simplified Chinese and Traditional Chinese interface translations with a shared catalog and generated browser bundle.
- Integrate the administrator maintenance queue, approved PDF processing, PDFCraft state tracking and safer host-worker scripts.
- Reconcile database migrations and journal-list cache refresh behavior; retain existing ranking, heatmap, and CLI functionality.
- Use 0.7.77 consistently for CLI, server, web assets, package lockfiles and release metadata.

## 0.7.76 — 2026-10-16

- Move the list-wide ranking status into the existing Dashboard search toolbar as a compact `Ranks x/y` indicator, with full progress available on hover and an icon-only re-fetch control. Remove the separate full-width banner so it no longer pushes the heatmap downward.
- Add an opt-in **Hover ranks** checkbox beside the compact status. Mouse hover over a journal name opens the existing draggable rank window and hovering another name updates its contents without moving the window or stealing keyboard focus.
- Remember each user's hover setting in browser storage. Debounce hover requests, ignore touch input, and keep the original context-menu lookup and manual refresh unchanged.
- Run focused ranking UI and layout checks rather than rerunning unrelated PDF extraction tests.

## 0.7.75 — 2026-10-16

- Restore the existing Chinese easyScholar dataset labels in the journal ranking panel, including previously browser-cached responses. Group headings now match the Chinese ranking taxonomy instead of overriding the server mapping with English labels.
- Persist successful per-journal ranking results globally in PostgreSQL so any authenticated user can reuse them after browser or server restarts. Normal opens read the shared cache; the per-journal Re-fetch action explicitly refreshes and replaces it.
- Add **Fetch current list rankings** to the journal context menu. It queues a server-side background job for the selected journal list, with resumable progress, safe concurrent-job deduplication, shared per-journal caching, and status polling across page reloads.
- Add **Re-fetch list rankings** after the batch finishes to deliberately refresh every journal, preserve previously cached data when the provider fails, and report failed counts without blocking the UI.
- Cover nonblocking execution, cache persistence across users, duplicate requests, restart recovery, manual refresh, permission checks and the Chinese label/frontend actions in new tests.

## 0.7.74 — 2026-10-16

- Fix the journal-list editor's Save list button: wire the delegated form submit event to the authenticated JSON POST, preventing the browser's default GET and ensuring additions/removals reach the database.
- Show a confirmation with the saved journal count after the dashboard refresh; keep the editor open and report errors or optimistic-concurrency conflicts without discarding edits.
- Normalize saved-list revision timestamps to UTC across PostgreSQL and SQLite, so consecutive saves and reopening the editor do not generate false conflicts.
- Add client-side submit-event regressions for adding/removing journals, double-click protection, refresh and error feedback, plus a database-backed save/reopen/delete round-trip test.

## 0.7.73 — 2026-10-16

- Harden dashboard preference migration for long-lived NetVault installations whose schema-version marker is ahead of the actual user columns. Idempotently reconcile the new preference field before loading account records, even if the migration version already matches.
- Add regression coverage for PostgreSQL-compatible schema drift and preserving existing saved journal lists.

## 0.7.72 — 2026-10-16

- Preserve each user's last selected Dashboard journal list across refreshes, navigation and devices; retain UTD24 as a first-visit default rather than resetting on every visit.
- Display **every** saved journal in filtered heatmaps, including journals without year-tagged PDFs, with zero counts rather than silently omitting them.
- Remember Dashboard search text, pagination, sort order and year-scroll position per user/list; retain the existing database-persisted journal display limit and theme settings.
- Add one-click text export for journal lists, always fetch a fresh list revision for editing, and reject outdated saves from another browser/tab rather than silently overwriting changes.
- Save the preferred filter only on actual user navigation via an authenticated CSRF-protected POST, not on Dashboard prefetch or read-only GET requests.
- Extend migration and browser/backend regressions for journal list completeness, selection persistence, editing concurrency and URL-prefix-aware preference requests.

## 0.7.71 — 2026-10-16

- Speed up normal `nv upload` on large folders: batch up to 500 DOI candidates per indexed `/pdfs/exists` request, instead of 12 per preflight.
- Include SHA-256-bound download-index DOI hints in batch preflight, but only skip distinct PDF bytes when their first-page DOI and complete article title corroborate the server record; keep legacy automatic CSV and ambiguous DOI hints on the verified upload path.
- Retry transient failures of read-only duplicate-lookup requests without treating network outages as missing server records.
- Distinguish already-stored files from new uploads in progress, and provide hashing, DOI lookup, precheck, and duplicate-resolution timings in verbose output.
- Keep force replacement, metadata writeback, explicit CSV trust, and resumable failed uploads on their existing safety workflows.

## 0.7.70 — 2026-10-16

- Remove the visible Correct DOI action from Search PDFs without changing the protected administrative correction API.
- Add an accessible checkbox to each search result, a Select all / Deselect all toggle scoped to the current page, a live selection count, and Download selected ZIP using the existing authenticated, CSRF-protected streaming ZIP endpoint.
- Disable ZIP download when nothing is selected and keep PDF selection behavior working after in-app navigation.
- Add selection interaction and selective ZIP regression tests.

## 0.7.69 — 2026-10-15

- Move dashboard heatmap scrollbars into separate horizontal and vertical rails outside the year squares, including macOS overlay scrollbar configurations.
- Keep the fixed TOTAL and JOURNAL columns vertically aligned with the independently scrolling year matrix, and synchronize scroll positions in both directions.

## 0.7.68 — 2026-10-15

- Consolidate DOI ZIP downloads into the dashboard Batch DOI editor, showing the exact count of stored PDFs available before offering a ZIP; retain the paginated Search PDFs action and remove the retired download page/sidebar link.
- Align fixed journal totals and names with the year grid rows.
- Refresh first-party static scripts and styles after deployment rather than letting the browser cache outdated click handlers for a year; keep vendor assets immutable.

## 0.7.67 — 2026-10-15

- Add a read-only Meta action to PDF search results, with a responsive dialog showing the vault's catalog metadata separately from the original PDF Document Info.
- Inspect embedded PDF Title, Author, DOI, page count and custom Info fields without modifying the stored PDF or trusting XMP for DOI identity; show conflicting embedded DOIs explicitly.
- Require a valid web session to read file metadata and return a clear unavailable or encrypted state when a PDF cannot be inspected; safely render user-supplied Info strings as text.

## 0.7.66 — 2026-10-15

- Add opt-in `nv upload --write-doi`: independently verify a missing DOI against Crossref and first-page article evidence, back up the source, append the DOI, and upload using the new SHA-256.
- Require an exact canonical DOI, one supported printed article identity, a matching heading, author and publication year; embedded metadata, CSV trust, filenames and ordinary upload acceptance do not authorize automatic writes.
- Bind verification to original bytes, reject unsafe/conflicting PDFs and concurrent source changes, preserve pages and unrelated metadata, and record original/new hashes, verification and backup paths in the upload report.
- Add an authenticated temporary-only DOI verification endpoint; preserve normal upload behavior, explicit CSV trust and existing DOI deduplication unless `--write-doi` is selected.

## 0.7.65 — 2026-10-15

- Add offline `nv pdf-meta read` and validated incremental `write --doi`, with explicit DOI correction, output-file support and preservation of unrelated PDF metadata.
- Use a unique parsed PDF Document Info DOI before upload hints, indexes, caches, filenames or page text in the CLI, API and web flow; stop on conflicting Info DOIs and retain the selected identity when registry enrichment is unavailable.
- Preserve explicit CSV trust and the existing resolver when Info has no DOI; keep hash assignment conflicts and successful metadata requirements for force uploads.
- Align the README, user guide, web CLI guide and shared PDF DOI policy, including the distinction between embedded metadata writes and local DOI cache hints.
- Restore corrupted wheel hashes in both dependency locks from verified PyPI file records so locked builds pass validation.

## 0.7.64 — 2026-10-15

- Place PDF Preview, Download, and Correct DOI controls on the first row at the upper right of search-result cards, beside DOI and Crossref metadata.
- Move the labeled Batch DOI entry button from the heatmap legend/footer to the left of the dashboard journal search field.
- Open DOI batch search results in a new browser tab while keeping the dashboard and DOI editor in the original tab; create the destination tab synchronously to avoid popup blocking.

## 0.7.63 — 2026-10-15

- Keep one batch DOI toolbar shortcut; search from the modal Search PDFs action.
- Fix the native regex checkbox to 16px and prevent its label from wrapping.

## 0.7.62 — 2026-10-15

- Add two compact DOI batch shortcuts before the heatmap legend: edit a multiline DOI list or immediately search the prepared batch.
- Optionally extract DOI patterns from pasted text with a disabled-by-default regex checkbox; deduplicate and normalize to one DOI per line with a live count.
- Provide authenticated, CSRF-validated exact DOI batch searches in the existing Search PDF results/cards, preserving standard pagination with a short, account-scoped batch URL.
- Limit each search to 500 unique DOIs and retain saved drafts in browser session storage.

## 0.7.61 — 2026-10-15

- Show a single theme-aware tooltip immediately beside the pointer when hovering any journal-year grid square, with the year and exact PDF count including zeros.
- Replace generated PDF links with accessible buttons that keep normal and modifier/middle-click navigation but avoid the browser's unstyleable white URL preview bar.
- Keep the tooltip outside the scrolling viewport and constrain its position to the visible screen.

## 0.7.60 — 2026-10-15

- Show the exact year and PDF count on hover for every year-grid cell, including years with zero PDFs; reuse native tooltips without additional floating UI.
- Open journal-list editors only after their list data is ready to prevent empty-modal loading flashes, Help auto-focus popovers, and content jumps.
- Warm list data only on edit intent and reuse a short-lived in-memory snapshot on quick reopens; invalidate after save/reset.
- Keep the Help tooltip intentional (hover or keyboard focus), remove the backdrop blur, and prevent saving an empty list if loading fails.

## 0.7.59 — 2026-10-15

- Make the centered dashboard width respond to actual year-grid and visible journal-title content instead of always filling its 1320px maximum; preserve narrower viewport scrolling.
- Simplify journal rank UI by moving selected-only and re-fetch actions to icon buttons beside Close in the title bar.
- Hide the redundant successful ranking-count/status line; keep loading, errors, and no-result feedback.
- Add icons to every journal context menu action and use concise English labels.

## 0.7.58 — 2026-10-15

- Use the Chinese source names from the 45-entry easyScholar abbreviation appendix when showing journal rankings.
- Display international/core journal lists before CAS divisions, JCR metrics, Xinrui Academic datasets, and other institutions, across both official and custom entries.
- Apply identical translations and grouping to browser-cached results without requiring refresh; preserve the original rank values.
- Translate the ranking panel headers and controls, retaining the filter, cache, refresh, and scroll behaviors.

## 0.7.57 — 2026-10-15

- Prevent sticky year labels from overlapping and bleeding through the scrolling heatmap rows by clipping the rotated text within the header and masking scrolled cells with the page-matched background.
- Keep the independent horizontal year scroller and frozen TOTAL/JOURNAL columns unchanged.

## 0.7.56 — 2026-10-15

- Cap the dashboard page width at 1320px and center it on ultrawide screens.
- Keep the toolbar and heatmap within that width while preserving the year-only horizontal scroll on narrower screens.

## 0.7.55 — 2026-10-15

- Scroll years independently from fixed TOTAL and JOURNAL columns, with latest years visible initially.
- Toggle descending and ascending sorting by clicking TOTAL or JOURNAL, without a separate sort icon.
- Restore input-triggered journal search, including initialisms formed after omitting function words.
- Reduce focus outlines and stretch the dashboard search bar and counts to fit the available width.

## 0.7.54 — 2026-10-15

- Display heatmap years from oldest on the left to newest on the right, keeping the statistics API unchanged.
- Start narrow heatmap views near the newest years while preserving horizontal scrolling after manual interaction.
- Keep journal names on a single line and move density controls to the left side of a simplified, borderless footer pager.

## 0.7.53 — 2026-10-15

- Scope the picker Add-button layout to its direct child so the new dropdown journal suggestions keep their individual rows instead of overlapping.
- Re-key deployed assets to guarantee fresh CSS at the public URL.

## 0.7.52 — 2026-10-15

- Remove heatmap header fills; bottom-align JOURNAL with TOTAL.
- Harmonize light and dark selected/focused states in journal filter tabs, list menus, and journal editors.
- Uppercase, deduplicate, and alphabetize journal titles on edit or save; move permanent editor guidance into a keyboard-accessible hover Help control.
- Replace unthemed native datalist suggestions with a scrollable, keyboard-accessible, width-constrained suggestion menu. Bound filter popovers to the viewport.
- Refresh the web CSS and JS cache keys with the package version.

## 0.7.51 — 2026-10-15

- Keep the right-hand journal names in normal grid flow, preventing sticky positioning from pulling them left across the counts in narrow containers.
- Preserve transparent labels in both color themes and invalidate the previous cached frontend release.

## 0.7.50 — 2026-10-15

- Arrange journal heatmap columns as year dots, right-aligned counts and left-aligned journal names; center the grid and allow horizontal scrolling on narrow screens.
- Remove journal-name row backgrounds in both themes. Bump web asset URLs with the matching package release so cached JavaScript and CSS cannot show the previous layout.

## 0.7.48 — 2026-10-15

- Default the dashboard to UTD24 and add a persistent light/dark theme toggle at the bottom of the sidebar.
- Remember the dragged ranking panel position across journal changes and browser reloads.
- Display browser-cached journal rankings immediately for 24 hours; revalidate older results in the background and preserve them if refresh fails.
- Reuse in-flight lookups, retain manual refresh, and adapt panel height to the viewport with a vertically scrolling results list.

## 0.7.47 — 2026-10-15

- Compact the ranking panel header, controls and table, removing repeated labels, timestamps and footer copy.
- Remove journal and year-cell hover/focus tooltips; journal actions open on click or explicit keyboard activation.

## 0.7.46 — 2026-10-15

- Allow the dashboard to use available width as needed, with complete wrapping journal names and horizontally scrolling years that keep recent years visible.
- Add copy, Web of Science, and easyScholar ranking actions to journal names, with a draggable fixed ranking panel.
- Bundle the easyScholar server-side key and normalize official and custom rankings using the PaperDock approach, with a 24-hour cache.

## 0.7.45 — 2026-10-14

- Match the complete input DOI list against server SQL in batches before downloads start. Exclude unavailable DOI values and verified local files from the download progress total; count missing DOI values quietly instead of treating them as failures.
- Keep bounded parallel downloads, SHA-256 verification and HTTP Range resume. Stop queued downloads when the server returns a rate limit, preserve partial files and save real failures or pending DOI values to an atomic report instead of printing per-DOI errors.
- Exempt admin API, web and ZIP downloads from the hourly download count limit. Keep the configured hourly limit for regular users (1200 by default) and existing concurrency, preview and ZIP-size controls.
- Update the CLI, server, web version badge, asset cache keys and download guide together to 0.7.45.

## 0.7.44 — 2026-10-14

- Open populated journal-year heatmap nodes in the existing PDF Search page with exact canonical journal and year filters. Use parameterized queries and the existing journal/year index, with no schema or CLI command changes.
- Show active filters and preserve them in pagination and subsequent keyword searches. Use standard links for keyboard access, browser back and opening a node in a new tab.
- Cover heatmap/result count parity, similar journal names, deleted PDFs, year filtering, pagination, keyword refinement and invalid query inputs in CI.

## 0.7.43 — 2026-10-14

- Keep dashboard journal controls on one line. Let the search field shrink from its 680px maximum as space narrows; preserve controls in a horizontally scrollable row on very small screens.
- Show journal sorting as one compact icon with the existing native options and keyboard support. Put the selected ordering in its hover tooltip instead of persistent text.

## 0.7.42 — 2026-10-14

- Widen the dashboard journal search to 680px on desktop while fitting narrower screens. Reduce bottom padding to 4px plus device safe-area needs so the heatmap uses the space above the compact pager.
- Use the installer's curl transport for public deployment version checks through the reverse proxy.

## 0.7.41 — 2026-10-14

- Require direct VPS deployment of the matching server/web package alongside each CLI release. Add a live API/web/asset version check and keep GitHub Actions responsible for package publication.
- Use one package version for the web badge and both static asset URLs. Prevent caching rendered HTML so the browser receives the current version after deployment.
- Make journal and search pagination compact. Reduce footer spacing, use 26px controls and override inherited form minimum heights that kept the old pager oversized.
- Align web CLI guidance, command help and user documentation for incremental uploads, directory state, explicit retries, download resume and PaperDock integration. Check displayed/copied guide commands against CLI declarations in CI without adding commands.
- Derive web and release consistency assertions from the package version instead of maintaining separate version literals in tests.

## 0.7.40 — 2026-10-14

- Persist hashes, identities and upload outcomes in a transactional `.netvault-upload.sqlite3` beside PDFs. Keep rejection state when global caches or per-run reports are removed, and commit each outcome before continuing.
- Preserve rejected identities across ordinary CLI/server release upgrades. Changes to PDF bytes, DOI evidence or resolver rules and existing explicit retry options still permit another attempt. Select retries from directory state, with legacy report fallback.
- Add a private, versioned PaperDock integration protocol for batch DOI/hash presence checks, including accepted aliases. Distinguish outages from confirmed absence and keep public nv commands unchanged.

## 0.7.39 — 2026-10-13

- Look up download DOI file information in batches of 200, prefetch the next batch, and start parallel downloads before the entire DOI list has been queried. Bound the queue to two slots per download worker and retry transient bulk lookup failures.
- Compute SHA-256 while writing downloads, without a second full-file read; hash the existing prefix before continuing a resumed download. Keep size and checksum verification before finalizing files.
- Cancel queued downloads on Ctrl+C, preserve partial files, and continue the same `nv download --file ... --to ...` command by skipping verified complete files and resuming `.part` files with HTTP Range.
- Deduplicate large DOI lists using a set while preserving input order. Add regression coverage for pipelining, filename collisions, missing files, transient lookup failures, interrupted downloads, resume ranges, and corrupt or incomplete responses.

## 0.7.38 — 2026-10-13

- Enforce explicit/cached/indexed DOI conflicts before CLI hash skips; send every new hash through server verification before establishing a file alias. Keep explicit CSV trust for scanned PDFs.
- Share API/web PDF lookup, alias prechecks, exact hash/prefix search, downloads, correction history and account validation; preserve password whitespace and explain global session logout.
- Generate the CLI/server DOI and ISSN primitives from one contract, with consistent candidate priorities and no additional runtime package. Align PostgreSQL journal entity decoding and ISSN validation with Python.
- Add schema migration 10 to align DOI/metadata nullability, database defaults and UTC metadata timestamps. Build a narrower text-search index concurrently while retaining precise main/alias hash queries.
- Bound object and DOI locks to separate 4,096-slot namespaces; retain safe move auditing, coalesce preview flushes, and exclude temporary locks from backups.
- Coalesce failed-upload report/cache writes, maintain incremental report counts, flush recoverable checkpoints on interruption, and atomically merge bounded caches across processes.
- Limit legacy serial lookup fallback to explicitly unsupported endpoints. Export accepted hash aliases by default; offer --primary-only for retained objects and --report for identify with --csv compatibility.
- Apply configured upload limits consistently to JavaScript and HTML forms, batch web prechecks, clarify verification opt-outs, and classify unretained aliases separately in identity audits.

## 0.7.37 — 2026-10-13

- Keep upload output concise with a single progress bar whose fixed total counts final file outcomes; save individual errors and index warnings to the atomic report, and show diagnostics only with `--verbose`.
- Remember recognized server DOI/title verification rejections by SHA-256 and server, so unchanged rejected PDFs are skipped even after renaming or moving. Reuse local missing/conflicting DOI results without treating a local miss as a server rejection.
- Retry after file contents, DOI/index/CSV evidence, client/resolver version or server version changes; `--refresh-doi`, `--retry-failed` and explicit force uploads also permit retries. Temporary errors and generic request validation are never remembered as identity rejections.
- Add `needs-review` and `review-skipped` report outcomes; include both in `--retry-failed`, and clear remembered rejection after a successful upload.
- Return server version with the existing bulk hash query, without another request; make recursive upload exclusions case-insensitive at every depth. Keep `unidentifys/` uploadable by default because vault misses can be new articles.

## 0.7.36 — 2026-10-13

- Add a complete login example with the server URL, `--username example-user` and `--password example-password` to the web CLI guide and copy button, CLI login help, README and user guide; retain interactive login.
- Simplify the web CLI guide to everyday install, login/status, upload and download commands; keep literature management and DOI tools in a collapsed Advanced Management section.
- Use clearly labelled example credentials in the rendered page and copy buttons.

- Unify local literature management under `nv identify`: automatic vault lookup, `--issn` inventory matching, or offline `--index`; preview by default and apply with `--rename`.
- Preserve the fast bounded parallel hash pipeline and per-run cache; make slower page-content comparison opt-in with `--check-content`, and retain inventory misses with status `not_in_index`.
- Remove the separate `nv rename` command and add a concise Literature Management category to the web CLI guide.

- Collect `nv identify --rename` duplicates and unidentified PDFs into `duplicates/` and `unidentifys/` under each PDF's original parent directory, at every recursive depth.
- Exclude both collection folder names throughout the scan and preserve shared duplicate collection only when `--duplicates-to` is explicitly supplied.
- Validate automatic collection folders before file operations and cover deep nesting, content duplicates, repeated runs and existing filenames in regression tests.

## 0.7.35 — 2026-10-12

- Add `nv rename` for exact-hash DOI renaming with an offline `doi,sha256` inventory (`--index`), a journal inventory downloaded once (`--issn`), or existing server bulk hash lookup with aliases.
- Overlap hashing, matching, renaming and audit writes through a bounded pipeline with configurable parallel readers; reuse unchanged hashes within the run with ctime checks before moves, without PDF page parsing.
- Preserve unmatched PDFs and occupied destinations, provide `--dry-run` and CSV audit reports, and validate the complete input index before renaming.
- Keep `nv identify --rename` as the full page-content comparison and duplicate collection workflow.

## 0.7.34 — 2026-10-12

- Make filename-free journal exports explicit in CLI help, README, the user guide and web examples: `nv export --issn ISSN`.
- Automatically add an available numeric suffix when a default export CSV already exists, so repeated exports need no filename and preserve earlier files.
- Keep custom output paths available through `--out`; use `--force` to replace the default or an explicit output path.

## 0.7.33 — 2026-10-12

- Add authenticated `nv export --issn ISSN --out FILE.csv` for the complete active DOI/PDF SHA-256 inventory, including older records linked through the reviewed journal registry.
- Match print and electronic ISSNs to the same journal, preserve distinct historical identities, validate ISSN checksums, and exclude deleted records.
- Download CSVs atomically, verify columns, hashes and row counts, and preserve existing files on failed exports.
- Document journal inventory export in CLI help, the user guide and web CLI examples.
- Retain raw venue names and enforce uppercase journal identities through reviewed ISSN/alias mappings in SQL and metadata refreshes; keep annual proceedings distinguishable.

## 0.7.32 — 2026-10-12

- Trust DOI/PDF pairs only when uploads explicitly specify `--csv`, including scanned PDFs with no readable text; skip PDF title/content verification while retaining independent metadata lookup.
- Match explicit CSV records by SHA-256 or filename and byte size; report unmatched, ambiguous or conflicting records without PDF extraction fallback.
- Keep automatically discovered CSV hints under server title verification and keep explicit CSV trust out of persistent DOI caches.
- Preserve PDF file checks, size limits, hash deduplication, existing DOI assignment conflicts and the metadata requirement for force replacement; record CSV-trusted identities and aliases explicitly.
- Explain explicit versus automatic CSV behavior, required columns and scanned PDF uploads in CLI help, README and the web CLI guide.

## 0.7.31 — 2026-10-11

- Recover interleaved heading text using bounded layout coordinates on the first two PDF pages; parse layout at most once for registry title matching per upload.
- Keep numeric identity and negation intact, require exact short titles, and restrict fuzzy matching to one small extraction error in a long word.
- Store articles with missing or unavailable external DOI metadata as pending only when a unique labelled DOI, a prominent heading, ISSN, publication history and author contact evidence agree within the PDF.
- Retain local pending titles during unsuccessful refreshes; known metadata conflicts, ambiguous printed DOIs and reference-only identities remain rejected.
- Require external metadata verification for force replacement and automatic aliases between different file hashes.

## 0.7.30 — 2026-10-11

- Verify DOI metadata titles directly against bounded heading regions on PDF pages one and two, including publisher covers and relocated subtitles.
- Normalize line wrapping, punctuation, soft hyphens and ligatures; keep short titles exact and long-title fuzzy matching conservative.
- Stop at article body, reference sections and citation instructions, and skip page two once page one has entered the article body.
- Avoid geometric title guessing and additional metadata requests when DOI-led title verification succeeds; invitation labels cannot override a matching article title.

## 0.7.29 — 2026-10-11

- Prioritize verified CSV/download-index DOI hints, embedded PDF metadata and first-page/second-page DOI evidence ahead of filename guesses.
- Correctly strip known double-hyphen hexadecimal download fingerprints from DOI-based filenames without altering stored PDFs.
- Extract article headings below publisher ISSN/journal mastheads, handling invalid Poppler XML control glyphs and fallback to page two.
- Keep reference-only DOI evidence behind filename fallbacks, enforce metadata title verification, and distinguish mismatched DOI candidates from provider outages.
- Refresh stale automatic DOI identity caches by bumping the resolver version; add regression tests for actual failure patterns.

## 0.7.28 — 2026-10-11

- Replace the top navigation with a compact icon-only sidebar, simplify Help, and expose administrators' last successful login timestamps.
- Consolidate Naval Research Logistics title variants without altering stored PDF metadata, and sort all journals before paginating the year heatmap.
- Add previous/next and direct journal page navigation with persistent initials search, Tab completion, and configurable journals per page.
- Reduce dashboard HTML by rendering only visible journal rows, combine PostgreSQL full-vault summary queries, and reuse cached global data for category filters.
- Check a lightweight dashboard revision token before refreshing full statistics, with a periodic fallback refresh.
- Enhance `nv upload` CSV DOI hint matching for nested and renamed PDFs with SHA-256 or verified article-title evidence; add `--csv`, `--dry-run`, and simplified CLI Help examples.

## 0.7.27 — 2026-10-11

- Verify article identity against the first-page heading instead of cited titles, prioritize primary DOI evidence, and reject dataset DOIs for articles.
- Read EBSCO cli-downloads.csv as filename/size-bound DOI hints, retaining server title verification.
- Display new/existing/failed counts during uploads, save an atomic upload report, and retry only failed or pending files with --retry-failed.
- Retry temporary upload failures with a rebuilt multipart stream and the same idempotency key.
- Refresh dashboard counts every 30 seconds while visible, preserving journal filters, sort order and scrolling.

## 0.7.26 - 2026-10-11

- Accept short article titles such as "OTC Discount" during DOI verification when
  they exactly match a standalone heading near the start of the PDF's first page.
- Require exact heading evidence for short titles, excluding body/reference-only
  occurrences, later pages and fuzzy or substring matches.
- Preserve short PDF title metadata for verified title-search recovery instead of
  requiring at least 12 characters and three words.
- Recover DOI-indexed Semantic Scholar metadata only when both Crossref and DOI.org
  explicitly report no record. Require an identical DOI and corroborate the title,
  author and year on the PDF's first page; record the actual verification provider.

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
