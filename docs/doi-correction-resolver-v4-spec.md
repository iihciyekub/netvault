# NetVault DOI Correction & Resolver v4 Specification

**Status:** Approved-for-implementation draft  
**Target release:** 0.7.17  
**Scope:** CLI + server API + web admin UI + DOI resolver + audit + release/deployment  
**Production target:** `root@72.62.255.34:/root/iiaide/netvault`

## 1. Problem

NetVault 0.7.16 can incorrectly treat PDF object syntax as part of a DOI when the raw-PDF fallback scans decoded bytes with the same broad suffix character set used for DOI validation.

Representative failure:

```text
10.1287/msom.2021.1032)>>/Border
```

The intended DOI is:

```text
10.1287/msom.2021.1032
```

The current resolver permits characters such as `)>`, `/`, and letters in the suffix, so raw PDF tokens such as `)>>/Border` can be consumed as if they were DOI content. A malformed identity can therefore reach PostgreSQL if later verification does not reject it.

NetVault already has a safe administrative correction primitive in 0.7.16:

```text
POST /admin/pdfs/{pdf_id}/correct-doi
```

and:

```text
netvault-admin correct-doi
```

The existing correction path already preserves the PDF row, SHA-256 object, upload/download history and aliases, stores an audit row in `pdf_doi_corrections`, and overwrites Crossref metadata. Release 0.7.17 will turn that primitive into a complete operator workflow and harden new DOI extraction.

---

## 2. Goals

0.7.17 SHALL:

1. Prevent PDF syntax fragments such as `)>>/Border`, `/URI`, `/Annot`, and related raw-object tokens from becoming part of a DOI.
2. Keep PostgreSQL identity corrections transaction-safe and auditable.
3. Never require direct SQL for normal DOI correction.
4. Re-fetch and fully synchronize Crossref metadata whenever an existing PDF DOI is corrected.
5. Verify that the target DOI belongs to the stored PDF by comparing Crossref title metadata with extractable PDF text.
6. Expose correction to administrators in both:
   - the lightweight `nv` CLI; and
   - the authenticated web UI.
7. Provide a non-destructive DOI audit workflow for finding already-polluted records.
8. Preserve the existing PDF object, SHA-256, row ID, aliases, upload history, and download history during a DOI correction.
9. Bump the resolver cache version so stale automatic identities are recomputed.
10. Release CLI and server together as version 0.7.17 and deploy the exact tested release to production.

---

## 3. Non-goals

0.7.17 SHALL NOT:

- silently merge two existing PDF rows that already claim the same corrected DOI;
- automatically rewrite suspicious historical DOI records without administrator review;
- add a Node/JavaScript DOI parsing dependency;
- move PDF objects based on DOI;
- change the content-addressed storage scheme;
- redesign the entire web application;
- introduce a new DOI registration-agency abstraction beyond the current Crossref-backed metadata workflow.

If a corrected DOI already belongs to another active PDF, the operation MUST stop with a conflict and leave both records unchanged. A future explicit merge workflow can handle that case.

---

## 4. Design principles

### 4.1 DOI extraction is candidate discovery, not identity proof

A regex match only creates a DOI candidate. It MUST NOT, by itself, authorize persistence as the PDF identity.

Final automatic identity is established by:

```text
PDF evidence
  -> candidate DOI
  -> normalization
  -> Crossref lookup
  -> canonical DOI
  -> Crossref-title <-> PDF-text verification
  -> persistence
```

### 4.2 Server is authoritative

The client may provide a DOI hint, but the server remains authoritative for automatic upload identity.

The browser SHALL NOT implement independent DOI parsing logic.

### 4.3 Corrections are identity changes, not file replacements

`--force` upload replaces bytes/metadata for the same DOI.

DOI correction changes the identity associated with the same stored PDF.

These workflows SHALL remain separate.

### 4.4 No direct SQL in normal operations

Normal corrections MUST go through the service/API so that locking, conflict checks, Crossref synchronization, evidence updates, cache invalidation, and audit history are all applied consistently.

---

## 5. Resolver v4

Set:

```python
DOI_RESOLVER_VERSION = 4
```

Automatic identity-cache entries from prior resolver versions SHALL be invalidated by the existing versioned-cache behavior.

### 5.1 Separate extraction patterns by context

Do not use one broad regex as both extractor and validator.

Introduce separate concepts:

```python
DOI_TEXT_RE
DOI_URL_RE
DOI_METADATA_RE
```

For ordinary visible text, use a conservative modern DOI candidate pattern equivalent in spirit to:

```regex
10\.\d{4,9}/[-._;()/:A-Z0-9]+
```

Case-insensitive.

This is a candidate extractor, not a complete definition of all valid DOI suffixes.

### 5.2 Normalize boundaries after extraction

Normalization SHALL:

1. remove `https://doi.org/`, `http://doi.org/`, `http(s)://dx.doi.org/`, and leading `doi:`;
2. Unicode-normalize where appropriate;
3. trim whitespace and ordinary sentence punctuation;
4. remove unmatched closing parentheses;
5. stop at PDF object delimiters / structural artifacts;
6. lowercase for NetVault's canonical storage representation;
7. reject empty/structurally impossible values.

Regression requirement:

```text
10.1287/msom.2021.1032)>>/Border
-> 10.1287/msom.2021.1032
```

### 5.3 Stop generic raw-head DOI scanning

The current generic scan over decoded raw PDF bytes SHALL be removed or restricted.

Raw bytes SHALL NOT be searched for arbitrary bare `10.xxxx/...` strings.

The raw fallback may only consider DOI candidates in explicit high-signal contexts, such as:

- `https://doi.org/...`
- `http://dx.doi.org/...`
- explicit `doi:` labels
- recognized DOI metadata keys

This prevents PDF object dictionaries from being interpreted as human text.

### 5.4 Parse PDF annotations structurally

Use `pypdf` to inspect page annotations and URI actions instead of recovering annotation URLs from raw PDF object syntax.

For each of the first pages where annotations are available:

```text
/Annots
  -> annotation object
  -> /A
  -> /URI
  -> explicit doi.org URL candidate
```

A DOI found in an explicit `doi.org` annotation is a high-confidence candidate, but still goes through Crossref/title verification before automatic persistence.

### 5.5 Candidate sources and suggested confidence

Suggested source order / confidence:

| Source | Suggested score / trust |
| --- | ---: |
| explicit `--doi` / verified download-index SHA binding | 100 |
| XMP / Document Info DOI | 96 |
| structured `doi.org` PDF annotation URI | 94 |
| filename DOI | 90 |
| visible first-page DOI with DOI label | 88 |
| visible page 2-3 DOI | lower |
| visible reference-list DOI | heavily down-ranked |
| restricted raw explicit DOI URL/label fallback | low |

The server still verifies candidates rather than trusting score alone.

### 5.6 Offline behavior

When `--no-crossref` is used:

- explicit user DOI and SHA-bound download-index DOI may proceed;
- consistent high-confidence metadata/filename/page evidence may proceed according to existing conflict rules;
- a raw-fallback-only candidate MUST NOT be automatically accepted;
- ambiguous evidence MUST require explicit `--doi`.

### 5.7 Shared client/server test vectors

The CLI and server currently maintain separate DOI modules.

0.7.17 will avoid a packaging redesign, but MUST introduce a single shared regression-test fixture containing DOI input/expected-output cases and run that fixture against both resolver implementations.

A full shared-package refactor can be considered for 0.8.0.

Required fixture cases include:

```text
10.1287/msom.2021.1032)>>/Border
https://doi.org/10.1287/msom.2021.1032)>>/Border
/URI (https://doi.org/10.1287/msom.2021.1032)>>/Border
DOI: 10.1287/msom.2021.1032.
DOI: 10.1000/example(abc)
multiple DOI candidates with a references section
filename DOI vs metadata DOI conflict
Crossref-valid wrong DOI whose title does not match the PDF
```

---

## 6. DOI correction service

Move correction business logic out of the FastAPI route into a reusable server service, e.g.:

```text
netvault_server/server/services/doi_correction.py
```

Both JSON API and Web routes SHALL call this service directly. The web layer MUST NOT make an HTTP request back into its own API.

Suggested service operations:

```python
preview_doi_correction(...)
apply_doi_correction(...)
```

### 6.1 Preview algorithm

Given PDF ID, requested DOI, reason, and optional concurrency expectations:

1. load the active PDF;
2. normalize requested DOI;
3. verify the current PDF SHA if an expected SHA is supplied;
4. verify the current DOI if an expected current DOI is supplied;
5. fetch Crossref metadata;
6. use Crossref's returned DOI as the canonical candidate when present;
7. normalize the canonical DOI;
8. check for a conflicting active PDF using the canonical DOI;
9. extract text from the stored PDF object;
10. compute `title_match_score(crossref_title, pdf_text)`;
11. return a preview plan without changing data.

### 6.2 Title match safety

If usable PDF text exists and:

```text
title_match_score < 0.88
```

the correction SHALL be blocked by default.

An administrator may explicitly override only with:

- CLI: `--allow-title-mismatch`
- Web: a deliberate confirmation control shown only after a mismatch

The override MUST be recorded in correction evidence/audit data.

If PDF text is unavailable, the preview SHALL display that title verification could not be performed; Crossref verification and administrator confirmation remain required.

### 6.3 Apply algorithm

Application MUST:

1. begin/continue a database transaction;
2. lock the PDF row with `SELECT ... FOR UPDATE`;
3. re-check expected SHA/current DOI;
4. re-run or revalidate the correction plan so preview cannot become stale;
5. check canonical target DOI conflict again;
6. snapshot previous identity and metadata;
7. insert `PdfDoiCorrection`;
8. update `pdf.doi` to canonical normalized DOI;
9. set `pdf.doi_source = "admin-corrected"`;
10. replace `pdf.doi_evidence` with structured correction evidence;
11. apply fresh Crossref metadata with `overwrite=True`;
12. commit;
13. invalidate statistics cache.

### 6.4 Crossref metadata synchronization

A successful DOI correction MUST refresh and overwrite the stored Crossref-derived fields:

- `title`
- `authors`
- `container_title`
- `publisher`
- `published_year`
- `crossref_status`
- `crossref_url`
- `crossref_fetched_at`

Changing `container_title` will cause the existing SQLAlchemy `before_update` listener to recompute `journal_key`, so journal-filter statistics remain aligned with the corrected metadata.

If Crossref returns `not_found` or is unavailable, the correction MUST NOT be applied.

### 6.5 Evidence written after correction

Suggested `doi_evidence` payload:

```json
{
  "source": "admin-corrected",
  "correction_id": 123,
  "corrected_from": "10.1287/msom.2021.1032)>>/border",
  "requested_doi": "10.1287/msom.2021.1032",
  "canonical_doi": "10.1287/msom.2021.1032",
  "reason": "Raw PDF object syntax was included in DOI",
  "crossref_status": "ok",
  "crossref_title": "...",
  "title_match_score": 0.97,
  "title_match_overridden": false
}
```

The existing `pdf_doi_corrections.previous_state` remains the authoritative before-state snapshot.

### 6.6 Data that MUST NOT change

A DOI correction MUST preserve:

- `pdf.id`
- `sha256`
- `storage_path`
- PDF bytes
- `PdfFileAlias` rows
- `UploadRecord` rows
- `DownloadRecord` rows
- original upload timestamp/history

---

## 7. API contract

Keep the existing endpoint for compatibility:

```text
POST /admin/pdfs/{pdf_id}/correct-doi
```

Extend the request model:

```json
{
  "doi": "10.1287/msom.2021.1032",
  "reason": "Raw PDF object syntax was included in DOI",
  "expected_sha256": "...",
  "expected_current_doi": "10.1287/msom.2021.1032)>>/border",
  "allow_title_mismatch": false,
  "dry_run": true
}
```

Extend the response model to include preview details:

```json
{
  "pdf_id": 56240,
  "previous_doi": "10.1287/msom.2021.1032)>>/border",
  "requested_doi": "10.1287/msom.2021.1032",
  "new_doi": "10.1287/msom.2021.1032",
  "sha256": "...",
  "title": "...",
  "authors": "...",
  "container_title": "...",
  "published_year": 2022,
  "crossref_status": "ok",
  "title_match_score": 0.97,
  "title_match_status": "match",
  "requires_title_override": false,
  "correction_id": null,
  "dry_run": true
}
```

The existing:

```text
GET /admin/pdfs/{pdf_id}/doi-corrections
```

remains the audit-history API.

---

## 8. Lightweight CLI workflow

Add correction support directly to the normal `nv` CLI so an administrator does not need the server package installed locally.

New command:

```text
nv correct-doi IDENTIFIER NEW_DOI --reason "..."
```

`IDENTIFIER` supports:

- numeric PDF ID;
- 64-character SHA-256;
- current DOI.

The CLI resolves the identifier to a single active PDF before preview.

### 8.1 Default interactive flow

Example:

```bash
nv correct-doi   10.1287/msom.2021.1032%29%3E%3E/border   10.1287/msom.2021.1032   --reason "Raw PDF object syntax was included in DOI"
```

CLI flow:

```text
Current DOI : 10.1287/msom.2021.1032)>>/border
PDF         : <title>
SHA-256     : <sha>
Requested   : 10.1287/msom.2021.1032
Canonical   : 10.1287/msom.2021.1032

Crossref
  Title     : ...
  Journal   : ...
  Year      : ...
  Match     : 97%

This keeps the same PDF file, row ID, aliases and history.
Crossref metadata will be replaced with the corrected record.

Apply correction? [y/N]
```

The CLI first sends `dry_run=true`. If confirmed, it automatically sends the returned SHA and current DOI as optimistic-concurrency guards.

The administrator should not normally need to manually copy `--expected-sha256`.

### 8.2 CLI options

Required/desired options:

```text
--reason TEXT               required
--dry-run                   preview only
--yes                       apply without interactive prompt after successful preview
--allow-title-mismatch      explicit safety override
--json                      machine-readable output
```

A title mismatch without `--allow-title-mismatch` MUST fail.

### 8.3 Backward compatibility

Keep:

```text
netvault-admin correct-doi
```

It SHALL use the same API semantics and safety checks.

Documentation should recommend `nv correct-doi` for ordinary remote administration.

---

## 9. DOI audit CLI

Add an administrator-only, non-destructive audit command:

```text
nv doi-audit
```

Default mode is local/server-data inspection only; it MUST NOT issue Crossref requests for the entire vault.

Flag records for review when, for example:

- DOI contains suspicious PDF syntax fragments such as `>>`, `<<`, `/Border`, `/URI`, `/Annot`;
- `crossref_status != "ok"`;
- stored `doi_evidence` records failed verification;
- DOI normalization would now change under resolver v4.

Suggested options:

```text
--limit N
--json
--verify-crossref
```

`--verify-crossref` must be explicit, bounded, cached, rate-limited, and respectful of Crossref backoff/rate-limit responses.

The audit command NEVER mutates a DOI automatically.

---

## 10. Web admin UI

### 10.1 Entry point

On `/web/pdfs` search results, administrators see one additional action beside Preview and Download:

```text
Correct DOI
```

Normal users do not see this control.

The action should use the existing visual system and remain compact; no new frontend framework is introduced.

### 10.2 Correction dialog

Open an accessible modal/sheet containing:

**Current record**
- current DOI
- title
- original filename
- PDF ID
- SHA-256 with copy action
- current Crossref status

**Correction form**
- new DOI
- reason (required)
- Verify button

### 10.3 Verify stage

Verify performs the same dry-run service call as the CLI and shows:

- normalized/canonical DOI;
- Crossref title;
- authors;
- journal/container title;
- year;
- title match score/status;
- conflict warning, if any.

States:

- green: verified + title match;
- amber: PDF text unavailable;
- red: Crossref failure, duplicate target, or title mismatch.

### 10.4 Apply stage

The Apply button remains disabled until preview succeeds.

If title mismatch is detected, the UI must require an explicit administrator confirmation before enabling Apply.

Apply includes the previewed current DOI and SHA as concurrency guards.

On success:

- close or transition the dialog to success state;
- refresh the affected result row/page;
- show corrected DOI and fresh Crossref metadata;
- invalidate frontend/PJAX page cache.

### 10.5 Correction history

The same dialog exposes a compact "History" section using:

```text
GET /admin/pdfs/{pdf_id}/doi-corrections
```

Display:

- old DOI -> new DOI;
- reason;
- administrator;
- timestamp.

### 10.6 Web routes

Because web authentication uses the signed session cookie and CSRF protection, create web-specific admin routes that call the shared correction service directly:

```text
POST /web/admin/pdfs/{pdf_id}/correct-doi/preview
POST /web/admin/pdfs/{pdf_id}/correct-doi
GET  /web/admin/pdfs/{pdf_id}/doi-corrections
```

All mutations require:

- authenticated admin;
- CSRF token;
- normal request-size/rate protections.

Do not make the web server HTTP-call its own JSON API.

---

## 11. Web DOI-health view

Add an admin-only filter/action to help find existing bad identities.

Suggested entry on PDF search page:

```text
DOI issues
```

This view uses local heuristics and stored status/evidence only; it does not call Crossref for every row during page render.

Each suspicious row may show a small warning badge such as:

```text
DOI review
```

Clicking it opens the same correction dialog.

---

## 12. Crossref integration requirements

Crossref remains the metadata authority for the current NetVault journal-paper workflow.

Implementation SHALL:

- use the versioned Crossref REST API endpoint;
- send the configured `NETVAULT_CROSSREF_MAILTO` / identifying User-Agent;
- reuse/cache metadata where safe;
- handle 404/not-found separately from temporary unavailability;
- honor rate limiting/backoff;
- never apply a correction when metadata verification fails.

The correction workflow uses a single-record lookup and is therefore appropriate for synchronous preview/apply.

Bulk audit Crossref verification is opt-in only.

---

## 13. Security and consistency

### 13.1 Authorization

Only `UserRole.admin` may:

- preview a stored-record DOI correction;
- apply a stored-record DOI correction;
- view correction history;
- run server-side DOI audit endpoints.

### 13.2 CSRF

All Web POST operations use the existing CSRF mechanism.

### 13.3 Concurrency

Apply must detect stale previews using:

- `expected_sha256`;
- `expected_current_doi`;
- row locking.

Return HTTP 409 if the PDF identity/file changed after preview.

### 13.4 Duplicate target DOI

If another active PDF already has the canonical target DOI:

- return HTTP 409;
- display the conflicting PDF ID/title where appropriate;
- make no changes.

No implicit row merge.

---

## 14. Database and migration impact

No schema migration is required for the core 0.7.17 plan.

Reuse:

- `pdfs`
- `pdf_file_aliases`
- `pdf_doi_corrections`
- `upload_records`
- `download_records`

Additional verification detail is stored in existing JSON/text evidence fields.

This keeps rollback simpler.

---

## 15. Testing

### 15.1 Resolver unit tests

Must cover:

- `)>>/Border` regression;
- annotation URI extraction;
- unmatched closing parenthesis cleanup;
- DOI labels and DOI URLs;
- valid DOI containing balanced parentheses;
- reference-list down-ranking;
- filename/metadata conflicts;
- raw fallback restrictions;
- resolver v3 cache invalidation to v4.

### 15.2 Server correction tests

Must cover:

- successful dry-run;
- successful correction;
- Crossref metadata fully overwritten;
- `journal_key` updated after container-title change;
- title mismatch rejected;
- explicit title-mismatch override recorded;
- Crossref not-found rejected;
- Crossref unavailable rejected;
- canonical DOI used;
- canonical target conflict rejected;
- stale SHA rejected;
- stale current DOI rejected;
- upload/download history unchanged;
- aliases unchanged;
- PDF bytes/storage path unchanged;
- audit row created exactly once;
- stats cache invalidated.

### 15.3 CLI tests

Must cover:

- resolve identifier by ID/SHA/DOI;
- dry-run output;
- interactive confirmation;
- `--yes`;
- `--json`;
- title mismatch behavior;
- server 403 for non-admin;
- conflict and stale-preview messages.

### 15.4 Web tests

Must cover:

- correction button visible only to admin;
- preview route CSRF/auth;
- apply route CSRF/auth;
- verified metadata rendered;
- mismatch confirmation required;
- correction history rendered;
- result page reflects corrected metadata;
- normal users cannot invoke hidden routes directly.

---

## 16. Documentation updates

Update:

- `README.md`
- `docs/user-guide.md`
- `docs/admin-guide.md`
- `CHANGELOG.md`

Document the preferred correction command:

```bash
nv correct-doi <old-doi-or-id-or-sha> <new-doi> --reason "..."
```

and state explicitly:

> Do not repair DOI identity by directly updating PostgreSQL except during controlled disaster recovery.

---

## 17. Version and release plan

Target version:

```text
0.7.17
```

Update every tracked version source required by `scripts/check-version-consistency.py`:

1. root `pyproject.toml`;
2. `src/netvault/__init__.py`;
3. root `uv.lock`;
4. server `pyproject.toml`;
5. server `src/netvault_server/__init__.py`;
6. server `uv.lock`;
7. top `CHANGELOG.md` release section.

Run:

```bash
uv lock
uv lock --project packages/netvault-server
python scripts/check-version-consistency.py --tag v0.7.17
pytest
```

Release commit subject MUST be:

```text
release: NetVault 0.7.17
```

Push the release commit to `main` and let the existing Release workflow create/tag `v0.7.17`.

---

## 18. Production deployment plan

Production target:

```text
root@72.62.255.34
/root/iiaide/netvault
```

### 18.1 Pre-deploy

1. confirm local/release tests are green;
2. confirm GitHub release/tag points to the intended commit;
3. create a production backup with the existing backup script;
4. record current deployed version/commit;
5. verify current `/health` and `/ready`.

### 18.2 Deploy

Deploy the exact tested 0.7.17 source while preserving:

- `.env`;
- `storage/`;
- production-only secrets/data.

Use the project's existing rsync/deploy convention and rebuild the server container:

```bash
docker compose -f docker-compose.iiaide.yml --env-file .env up --build -d server
```

### 18.3 Post-deploy validation

Verify:

```text
/health
/ready
/web
nv --version
```

Then perform:

1. login smoke test;
2. PDF search;
3. DOI-correction dry-run on a known record;
4. correction-history read;
5. Resolver v4 local regression test against the known `10.1287/msom.2021.1032)>>/Border` case;
6. no unexpected DB/storage drift.

### 18.4 Rollback

If deployment fails:

- restore the previous application release/image;
- if data was corrupted, use the verified pre-deploy backup;
- do not delete the new backup or prior release until smoke tests pass.

Because the planned feature requires no schema migration, application rollback should remain low risk.

---

## 19. Acceptance criteria

0.7.17 is complete only when all are true:

- `10.1287/msom.2021.1032)>>/Border` resolves to `10.1287/msom.2021.1032`;
- the same malformed suffix cannot silently enter PostgreSQL through automatic upload;
- an admin can find a PDF in the Web UI, preview a correction, see Crossref metadata/title match, and apply it;
- an admin can perform the equivalent workflow using `nv correct-doi`;
- a successful correction refreshes all Crossref metadata and journal-key/statistics behavior;
- PDF ID/SHA/file/history/aliases are preserved;
- a correction audit row is written;
- target DOI conflicts are blocked;
- stale preview SHA/current DOI is blocked;
- title mismatch is blocked unless explicitly overridden and audited;
- resolver cache version is 4;
- local DOI audit can locate suspicious existing records without modifying them;
- all tests pass;
- all version sources equal 0.7.17;
- production health/readiness checks pass after deployment.

---

## 20. Implementation order

Implement in this order:

1. add shared DOI regression fixture;
2. implement Resolver v4 in CLI and server;
3. add correction service and enhanced preview/apply contract;
4. add server/API tests;
5. add `nv correct-doi`;
6. add `nv doi-audit`;
7. add Web correction routes;
8. add Web correction dialog/history/DOI-health UI;
9. complete CLI/Web tests;
10. update docs/changelog;
11. bump all versions to 0.7.17 and regenerate locks;
12. run full test/version checks;
13. commit/push release;
14. deploy to `root@72.62.255.34`;
15. run production smoke tests and the known DOI regression check.
