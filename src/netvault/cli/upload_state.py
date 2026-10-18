"""JSONL-backed upload state: no SQLite writes or database creation.

The original per-folder SQLite is consulted read-only when needed for legacy
retries / confirmed identities. All new persistence uses history.jsonl.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from netvault.cli.history import History, file_signature

STATE_NAME = ".netvault-upload.sqlite3"  # legacy read-only compatibility
RETRY_STATUSES = {"pending", "failed", "needs-review"}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def signature(path):
    return json.dumps(file_signature(path), separators=(",", ":"))


def stable_context(context):
    return {key: value for key, value in context.items()
            if key not in {"client_version", "server_version"}}


class UploadState:
    def __init__(self, root: Path | None = None, *, record_events: bool = True):
        self.root = root.resolve() if root is not None else None
        self.record_events = record_events
        self._histories: dict[Path, History] = {}
        self._cached_events: dict[tuple[Path, str], dict[str, dict]] = {}
        self._cached_doi: dict[Path, dict] = {}
        self._cached_rejected_doi: dict[Path, dict] = {}
        self._uploads: dict[tuple[str, str, str], dict] = {}
        self._file_hashes: dict[Path, tuple[str, str]] = {}
        self._identities: dict[str, dict | None] = {}
        self._paper_cache: dict[tuple[Path, str, str], dict] = {}
        self.paths: set[str] = set()

    def _history(self, path: Path) -> History:
        root = self.root or Path(path).resolve().parent
        journal = self._histories.get(root)
        if journal is None:
            journal = History(root, "upload")
            self._histories[root] = journal
        return journal

    def _events(self, history: History, server: str) -> dict[str, dict]:
        key = history.root, server.rstrip("/")
        if key not in self._cached_events:
            self._cached_events[key] = history.latest_uploads(server)
        return self._cached_events[key]

    def _legacy(self, path: Path, sql: str, args: tuple):
        target = path.resolve().parent / STATE_NAME
        if not target.is_file():
            return None
        import sqlite3
        try:
            with sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=2) as conn:
                conn.row_factory = sqlite3.Row
                value = conn.execute(sql, args).fetchone()
                return dict(value) if value else None
        except (sqlite3.Error, OSError):
            return None

    def hash_file(self, path, hash_file, *, force=False):
        path = Path(path).resolve()
        before = signature(path)
        cached = self._file_hashes.get(path)
        if cached and cached[0] == before and not force:
            return cached[1], True
        value = hash_file(path)
        if signature(path) != before:
            raise RuntimeError("File changed while hashing; retry after it stops changing")
        self._file_hashes[path] = before, value
        return value, False

    def _legacy_status(self, path: Path, server: str):
        row = self._legacy(
            path, "SELECT sha256, signature FROM files WHERE name=?", (path.name,),
        )
        if row is None:
            return None
        if row["signature"] != signature(path):
            return "pending"
        state = self._legacy(
            path, "SELECT status FROM uploads WHERE server=? AND sha256=?",
            (server.rstrip("/"), row["sha256"]),
        )
        return state["status"] if state else "pending"

    def path_status(self, path, server, *, hash_file=None):
        path = Path(path).resolve()
        history = self._history(path)
        row = self._events(history, server).get(history.file_key(path))
        if row and (not row.get("server") or row["server"] == server.rstrip("/")):
            if row.get("signature") != file_signature(path):
                return "pending"
            if row.get("action") == "clear":
                return None
            if row.get("action") == "error":
                return "needs-review" if row.get("result") == "review" else "failed"
            if row.get("action") == "pending":
                return "pending"
            return "uploaded" if row["action"] == "upload" else "replaced"
        return self._legacy_status(path, server)

    def get(self, path, sha256, server):
        path = Path(path).resolve()
        key = (str(path), sha256, server.rstrip("/"))
        record = self._uploads.get(key)
        if record is not None:
            return record
        journal = self._history(path)
        events = self._events(journal, server)
        row = events.get(journal.file_key(path))
        if row is None:
            # Hash equality is only a fallback for restoring a known failed
            # upload after a rename; never use it to decide DOI deduplication.
            row = next((entry for entry in events.values()
                        if entry.get("sha256") == sha256
                        and entry.get("action") in {"error", "pending"}), None)
        if row and row.get("action") == "clear":
            return None
        if row and row.get("sha256") == sha256 and row.get("server") == server.rstrip("/"):
            state = ("needs-review" if row.get("result") == "review"
                     else "failed" if row.get("action") == "error"
                     else "pending" if row.get("action") == "pending"
                     else "replaced" if row.get("action") == "replace"
                     else "uploaded")
            return dict(status=state, attempts=1, sha256=sha256, doi=row.get("doi"),
                        context=row.get("context"), error=row.get("reason"),
                        pdf=row.get("pdf"))
        old = self._legacy(
            path, "SELECT * FROM uploads WHERE server=? AND sha256=?",
            (server.rstrip("/"), sha256),
        )
        if old:
            for column in ("context", "pdf"):
                try:
                    old[column] = json.loads(old[column]) if old[column] else None
                except ValueError:
                    old[column] = None
        return old

    def pending(self, path, sha256, server):
        path = Path(path).resolve()
        key = (str(path), sha256, server.rstrip("/"))
        if key not in self._uploads:
            # Never shadow a previously rejected identity when preparing a
            # retry. SQLite used INSERT OR IGNORE for this same guarantee.
            self._uploads[key] = self.get(path, sha256, server) or dict(
                status="pending", attempts=0, context=None, error=None, pdf=None,
            )

    def record(self, path, sha256, server, status, *, context=None,
               error=None, pdf=None, attempted=False):
        path = Path(path).resolve()
        key = (str(path), sha256, server.rstrip("/"))
        prev = self.get(path, sha256, server) or {}
        if status == "review-skipped":
            status = "needs-review"
        value = dict(status=status, sha256=sha256, server=server.rstrip("/"),
                     context=context if context is not None else prev.get("context"),
                     error=error, pdf=pdf if pdf is not None else prev.get("pdf"),
                     attempts=prev.get("attempts", 0) + int(attempted))
        self._uploads[key] = value
        if self.record_events and status in {"failed", "needs-review"}:
            journal = self._history(path)
            journal.write(
                {"uploaded": "upload", "replaced": "replace"}.get(status, "error"),
                file=path, sha256=sha256, server=server, doi=(pdf or {}).get("doi")
                or (context or {}).get("doi"),
                result="review" if status == "needs-review" else "failed" if status == "failed" else "ok",
                reason=error, signature=file_signature(path),
                context=value.get("context"), pdf=value.get("pdf"),
            )
            self._cached_events.pop((journal.root, server.rstrip("/")), None)

    def identity(self, path, sha256):
        if sha256 in self._identities:
            return self._identities[sha256]
        path = Path(path).resolve()
        journal = self._history(path)
        if journal.root not in self._cached_doi:
            confirmed = {}
            retry_hints = {}
            # One streaming replay for all PDFs in this invocation, rather than
            # rescanning JSONL for each unresolved article.
            for record in journal.records():
                digest = record.get("sha256")
                if not digest:
                    continue
                if record.get("cmd") == "doi":
                    if record.get("action") == "confirm":
                        confirmed[digest] = record.get("identity")
                    elif record.get("action") == "forget":
                        confirmed[digest] = None
                elif record.get("cmd") == "upload" and record.get("action") == "error":
                    context = record.get("context")
                    if not isinstance(context, dict):
                        continue
                    source, doi = context.get("source"), context.get("doi")
                    if source == "user" and isinstance(doi, str):
                        retry_hints[digest] = {
                            "status": "confirmed", "source": "user", "doi": doi,
                        }
                    elif source and isinstance(doi, str):
                        retry_hints[digest] = {
                            "status": "ok", "source": source, "doi": doi,
                            "resolver_version": context.get("resolver_version"),
                        }
                    elif doi is None:
                        retry_hints[digest] = {
                            "status": "no-doi", "source": source,
                            "resolver_version": context.get("resolver_version"),
                        }
            self._cached_doi[journal.root] = confirmed
            self._cached_rejected_doi[journal.root] = retry_hints
        if sha256 in self._cached_doi[journal.root]:
            return self._cached_doi[journal.root][sha256]
        if sha256 in self._cached_rejected_doi[journal.root]:
            return self._cached_rejected_doi[journal.root][sha256]
        old = self._legacy(path, "SELECT payload FROM identities WHERE sha256=?", (sha256,))
        if old:
            try:
                return json.loads(old["payload"])
            except ValueError:
                return None
        return None

    def save_identity(self, path, sha256, identity):
        if identity.get("source") == "csv-trusted":
            return
        self._identities[sha256] = identity
        if identity.get("source") == "user":
            journal = self._history(path)
            journal.cmd = "doi"
            journal.write("confirm", file=path, sha256=sha256, doi=identity.get("doi"),
                          identity=identity)
            journal.cmd = "upload"
            self._cached_doi.pop(journal.root, None)
            self._cached_rejected_doi.pop(journal.root, None)

    def remove_identity(self, path, sha256):
        previous = self.identity(path, sha256)
        self._identities[sha256] = None
        if previous is not None:
            journal = self._history(path)
            journal.cmd = "doi"
            journal.write("forget", file=path, sha256=sha256)
            journal.cmd = "upload"
            self._cached_doi.pop(journal.root, None)
            self._cached_rejected_doi.pop(journal.root, None)
        return previous is not None

    def rejection(self, path, sha256, server, context):
        prior = self.get(path, sha256, server)
        return prior if prior and prior["status"] == "needs-review" and prior.get("context") \
            and stable_context(prior["context"]) == stable_context(context) else None

    def set_paper(self, directory, server, doi, present, *, pdf=None):
        key = (Path(directory).resolve(), server.rstrip("/"), doi)
        self._paper_cache[key] = dict(
            present=bool(present), checked_at=timestamp(), source="cached",
        )

    def set_papers(self, directory, server, papers):
        directory = Path(directory).resolve()
        checked_at = timestamp()
        for doi, pdf in papers.items():
            self._paper_cache[(directory, server.rstrip("/"), doi)] = {
                "present": pdf is not None, "checked_at": checked_at, "source": "cached",
            }
        # One bounded event per sync batch rather than a record for every DOI.
        history = self._history(directory / "_sync-state")
        history.cmd = "sync"
        history.write("cache", server=server, checked_at=checked_at,
                      papers={doi: pdf is not None for doi, pdf in papers.items()})
        history.cmd = "upload"
        return checked_at

    def papers(self, directory, server, dois):
        directory = Path(directory).resolve()
        history = self._history(directory / "_sync-state")
        result = {}
        for row in history.records():
            if row.get("cmd") != "sync" or row.get("server") != server.rstrip("/"):
                continue
            if row.get("action") == "cache" and isinstance(row.get("papers"), dict):
                for doi, present in row["papers"].items():
                    if doi in dois:
                        result[doi] = dict(present=present, checked_at=row["checked_at"], source="cached")
        for doi in dois:
            value = self._paper_cache.get((directory, server.rstrip("/"), doi))
            if value:
                result[doi] = value
        return result

    def close(self):
        self._uploads.clear()
        self._file_hashes.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
