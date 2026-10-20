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
180 ms search debounce, and cancellation of superseded requests. Filter state is
stored in the URL. English, Simplified Chinese, Traditional Chinese, and themes
follow NetVault settings; journal metadata is not translated.

Journal names open WOS `SO=("title")`; ISSN/eISSN open `IS=(identifier)` using the
existing WOS `queryJson=[{"rowText":"..."}]` URL contract. Authentication at WOS
still uses the reader's own WOS/browser session.

Verification: `pytest tests/test_jcr.py tests/test_i18n_web.py tests/test_web_and_stats.py`.
The source-language guard includes the JCR feature JS and CSS.
