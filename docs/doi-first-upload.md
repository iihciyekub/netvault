# DOI-first nv upload (source implementation)

Default: scan PDF Document Info for a unique DOI, otherwise run pdftotext on
pages 1-2. Query existing server DOIs in batches of 500 **before** hashing or
uploading. Already-stored DOI skips need no SHA-256 comparison. If the DOI came
from PDF text or filename, corroborate an existing record using its first-page
DOI and title; ambiguous identities use the existing resolver.

DOI preflight uses a bounded thread pool for independent metadata + pdftotext
scans. The worker count adapts to CPU affinity or logical cores (roughly half,
maximum six, never more than the number of files). Ordinary nv upload --dry-run
shares the same scan and does not hash PDFs already identified by this fast
pass. Explicit index and writeback previews retain their legacy specialized
logic. The --jobs option continues to control upload concurrency only.

For new text-derived identities, require an exact CSL title substring after
case folding and retaining only ASCII a-z characters. Failure becomes
needs-review, not a forced upload. A unique DOI in PDF Document Info is trusted
without online title confirmation. The server remains the authority for
untrusted PDF identity verification.

Examples:

    nv upload .
    nv upload paper.pdf --doi 10.1234/example
    nv upload . --csv dois.csv
    nv upload paper.pdf --doi 10.1234/example --force

Simple CSV:

    file,doi
    paper.pdf,10.1234/example
    journal/other.pdf,10.1234/other

Explicit CLI/CSV DOIs are trusted. CSV matches exact relative paths or unique
basenames, no SHA-256 or original byte-size requirement; richer legacy download
CSV files retain a fallback resolver for renamed files. Already-present DOI
skips by default, --force replaces it.

For a valid new PDF, the server writes canonical /DOI to the private staging
file, recomputes the final content-addressed SHA-256, and only then commits the
stored PDF and database row. SHA remains a file integrity key, never the paper
identity key. Signed/encrypted PDFs cannot be modified safely.

PDFCraft optimization is separate from upload and need not block the response.
The existing scripts/compress_pdfs_parallel.py attempts compression, restamps
/DOI if PDFCraft drops Info, verifies structure, and only accepts a final
object strictly smaller than the **already DOI-stamped** source. It records a
no-gain attempt against the original hash to avoid repeated work.

The optional bounded cron example is in
deploy/netvault-upload-compression.example.cron. Do not install it until the
matching server/CLI code has passed tests and deployment review: it performs
unattended canonical-object/DB modifications, including old backlog.
