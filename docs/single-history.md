# One local NetVault journal (v1)

Commands `nv upload`, `nv tidy --apply` and `nv download`
append to the same `.netvault/history.jsonl` in the invoked root. Recursive commands do not create separate logs in each child
directory. Previews never write an audit.

The JSON Lines schema is flat: `v`, `time`, `run`,
`cmd`, `action`, `file`, `result`, with optional
`doi`, `target`, `reason`, `sha256`, `server` and
`signature`. Paths are relative to the invocation root whenever possible.
Each upload run writes one summary. Unchanged server DOIs do not create per-PDF events.
Only actual upload attempts enter the pending queue; unresolved errors/reviews remain
retryable. Server-confirmed success clears pending/error entries, without keeping
per-PDF success records. Once per completed upload, the journal is compacted
atomically to retain only current unresolved states, the most recent upload
summary, manual DOI confirmations, and real tidy/download audit events.
Append and compaction share a small hidden lock file; incomplete JSONL tails
are separated and ignored. No per-file rewrites and no background worker.

The only legacy exports are opt-in:
  nv upload . --report ./my-upload.json

The unified log stores outcomes; CSV DOI mapping and hash inventory files are
input data and stay independent. No SQLite database is written by new CLI runs.
Old .netvault-upload.sqlite3 files remain untouched and may be consulted
read-only for historical retries / confirmed identities. If one exists, a tiny
clear marker can prevent its older rejection from resurfacing after the server
confirms success; this marker can disappear once the legacy DB is removed. The new log is enough
to support retry-failed for new operations. Existing reports and databases are
not automatically migrated or deleted.

No new dependencies, service, or background tasks.
