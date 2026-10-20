# JCR journal catalogue

Open `/nv/web/jcr` (sidebar table icon). All authenticated readers can browse;
administrators can import a ZIP using **Import ZIP**. Upload the complete JCR
export ZIP, including its hidden `.wosaide-jcr` folder. macOS resource forks are
ignored. Both journal arrays and `{ "data": [...] }` API responses are accepted.

The importer reads all JSON batches, including nested recovery batches, checks
`task.json.totalCount` where present, deduplicates identical journal records and
rejects conflicting duplicates. A validated upload replaces only the years in
that upload in one SQLite transaction. Other years and all PDF data are retained.
Invalid archives leave existing catalogue data intact. ZIPs are limited to 64 MB
compressed, 256 MB expanded, 2,000 entries, and 12 MB per JSON file. Archives are
never extracted to disk. Do not upload a partial export without its manifest:
without the manifest, completeness cannot be verified.

Persistent data: `storage/jcr/catalog.sqlite3` with SQLite WAL. Journal objects
are stored losslessly alongside indexed projections and per-category index /
quartile rows. Import SHA-256, input filenames, counts, and timestamp are retained
in the `imports` table. Future source fields remain visible in the details drawer.
The source field `jif2019` is displayed as JIF for the record's `jcrYear`; the
historical field name is preserved in original JSON.

Filters: year, search words, exact publisher, index, JIF quartile, category family,
exact category, and JIF range. Combined index/quartile/category constraints must
match **the same category record**. Multi-index strings are split correctly.
Unranked matches empty / N/A category quartiles. SCI is exposed as SCI / SCIE.

Category families are derived from the category name before its first comma.
They are a local navigation aid, not official JCR broad subjects or CAS partitions.
The original 254 categories in the 2025 export are retained without relabelling.

The table uses bounded server pagination (50/100/200 rows), 23 px rows, sticky
headings, horizontal scrolling, optional persistent columns, numeric sorting,
120 ms search debounce, and cancellation of superseded requests. Filter state is
stored in the URL. English, Simplified Chinese, Traditional Chinese, and themes
follow NetVault settings; journal metadata is not translated.

Journal names open WOS `SO=("title")`; ISSN/eISSN open `IS=(identifier)` using the
existing WOS `queryJson=[{"rowText":"..."}]` URL contract. Authentication at WOS
still uses the reader's own WOS/browser session.

Verification: `pytest tests/test_jcr.py tests/test_i18n_web.py tests/test_web_and_stats.py`.
The source-language guard includes the JCR feature JS and CSS.

## Search and table controls

The column picker exposes all 33 top-level source fields plus three derived
navigation columns (36 data columns); 12 common columns are shown by default. Category-ranking arrays open a drawer
showing every original per-category metric. Right-click any table heading to open the searchable checkbox picker. The toolbar
button supports keyboard/touch access. Selections persist locally. Users can hide/show fields and use
**Show all columns** to restore complete coverage.

Acronyms are derived from journal-title initials after dropping common English
and Romance/German function words, ignoring punctuation and possessive endings.
`IJIR` and `I.J.I.R.` match International Journal of Intercultural Relations.
Acronyms can collide; all matching journals are retained. Acronym tokens can be
combined with ordinary search words and the usual filters.

Every data heading sorts the entire filtered catalogue on the server before
pagination. Numeric metrics and rank numerators use numeric values; missing
values remain at the end in both directions. Stable journal IDs break ties.
A separate typed SQLite projection is backfilled transactionally once for
existing catalogues and updated atomically on future ZIP imports. Raw JSON and
journal identities remain intact. Sorted projections use SQLite indexes; the
browser never downloads all journals to sort a page.

Drag a heading's right edge to resize, double-click to restore its default width,
or focus the divider and use Left/Right (Shift for 50 px steps, Home to reset).
Widths persist locally. Resizing uses requestAnimationFrame and sends no API
requests; it remains active if an in-flight search finishes during a drag. The
table keeps explicit widths and scrolls horizontally within its own region.
