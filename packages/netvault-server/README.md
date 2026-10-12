# NetVault Server

FastAPI server, administrator CLI, and web UI for NetVault deployments.

Explicit CLI `nv upload ./papers --csv ./cli-downloads.csv` requests use the
`csv-trusted` DOI source. The server accepts these user-asserted DOI/PDF pairs
without parsing PDF text or matching article titles, so scanned PDFs are supported.
Metadata lookup remains independent and does not block ordinary uploads when
metadata is missing or unavailable. File integrity, size limits, deduplication,
existing hash/DOI conflicts, and the metadata requirement for force replacement
remain enforced. Automatic CSV discovery uses `download-record` and retains
PDF title verification. Stored DOI evidence distinguishes CSV trust from PDF
content verification; CSV-trusted aliases are recorded separately as well.
