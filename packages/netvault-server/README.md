# NetVault Server

FastAPI server, administrator CLI, and web UI for NetVault deployments.

The existing `POST /pdfs/exists` response includes `server_version`. The CLI uses
this value with SHA-256 and DOI evidence to invalidate remembered identity
rejections when server verification rules change, without an additional request.

Explicit CLI `nv upload ./papers --csv ./cli-downloads.csv` requests use the
`csv-trusted` DOI source. The server accepts these user-asserted DOI/PDF pairs
without parsing PDF text or matching article titles, so scanned PDFs are supported.
Metadata lookup remains independent and does not block ordinary uploads when
metadata is missing or unavailable. File integrity, size limits, deduplication,
existing hash/DOI conflicts, and the metadata requirement for force replacement
remain enforced. Automatic CSV discovery uses `download-record` and retains
PDF title verification. Stored DOI evidence distinguishes CSV trust from PDF
content verification; CSV-trusted aliases are recorded separately as well.


API and web routes share PDF and account services. New file aliases are created
by the verified upload path (or explicit CSV trust); `/pdfs/aliases` only acknowledges
already accepted hashes. Inventory exports include aliases by default;
`include_aliases=false` selects retained hashes only.

Identity primitives are generated from `contracts/identity.py` into each package.
No third runtime package is needed. Schema migration 10 aligns constraints and UTC
metadata timestamps, installs HTML5/ISSN normalization, and replaces the broad GIN
index with a text-only index. Run the isolated PostgreSQL contract check before
production upgrades.

Object/DOI locks use separate 4,096-slot directories under `storage/locks-v2`.
Stop all old writers before switching lock protocols. Archive `storage/locks` only
while the old service is stopped; never unlink active lock files. Backups exclude
both lock directory versions. Alias audits distinguish unretained variants from
missing canonical objects.
