"""Portable per-directory upload state. JSON reports remain per-run diagnostics."""

from collections import OrderedDict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

STATE_NAME = ".netvault-upload.sqlite3"
STATE_VERSION = 1
RETRY_STATUSES = {"pending", "failed", "needs-review"}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def signature(path):
    stat = path.stat()
    return json.dumps([stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                       stat.st_dev, stat.st_ino], separators=(",", ":"))


def stable_context(context):
    # Release numbers are not changes to the file or its identity evidence.
    return {key: value for key, value in context.items()
            if key not in {"client_version", "server_version"}}


class UploadState:
    """SQLite transactions commit individual outcomes, with bounded connections.

    Paths inside each ledger are relative to that ledger's directory. No TTL or
    cache eviction removes rejections. Only this layer writes the shared state.
    """

    def __init__(self):
        self.connections = OrderedDict()
        self.paths = set()

    def connection(self, directory, *, create=True):
        directory = Path(directory).resolve()
        target = directory / STATE_NAME
        connection = self.connections.pop(target, None)
        if connection is not None:
            self.connections[target] = connection
            return connection
        if not create and not target.exists():
            return None
        directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target, timeout=15)
        connection.row_factory = sqlite3.Row
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, STATE_VERSION):
            connection.close()
            raise RuntimeError(f"Unsupported upload state version {version}: {target}")
        try:
            with connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS files (
                    name TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                    signature TEXT NOT NULL, updated_at TEXT NOT NULL)""")
                connection.execute("""CREATE TABLE IF NOT EXISTS identities (
                    sha256 TEXT PRIMARY KEY, payload TEXT NOT NULL)""")
                connection.execute("""CREATE TABLE IF NOT EXISTS uploads (
                    server TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL,
                    doi TEXT, context TEXT, error TEXT, pdf TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL, PRIMARY KEY (server, sha256))""")
                connection.execute("""CREATE TABLE IF NOT EXISTS papers (
                    server TEXT NOT NULL, doi TEXT NOT NULL, present INTEGER NOT NULL,
                    checked_at TEXT NOT NULL, pdf TEXT, PRIMARY KEY (server, doi))""")
                connection.execute(f"PRAGMA user_version={STATE_VERSION}")
        except Exception:
            connection.close()
            raise
        self.connections[target] = connection
        self.paths.add(str(target))
        while len(self.connections) > 16:
            self.connections.popitem(last=False)[1].close()
        return connection

    def hash_file(self, path, hash_file):
        path = path.resolve()
        before = signature(path)
        connection = self.connection(path.parent)
        row = connection.execute("SELECT sha256, signature FROM files WHERE name=?",
                                 (path.name,)).fetchone()
        if row and row["signature"] == before:
            return row["sha256"], True
        sha256 = hash_file(path)
        if signature(path) != before:
            raise RuntimeError("File changed while hashing; retry after it stops changing")
        with connection:
            connection.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                               (path.name, sha256, before, timestamp()))
        return sha256, False

    def get(self, path, sha256, server):
        connection = self.connection(path.resolve().parent, create=False)
        if connection is None:
            return None
        row = connection.execute("SELECT * FROM uploads WHERE server=? AND sha256=?",
                                 (server.rstrip("/"), sha256)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for name in ("context", "pdf"):
            result[name] = json.loads(result[name]) if result[name] else None
        return result

    def path_status(self, path, server, *, hash_file=None):
        path = path.resolve()
        connection = self.connection(path.parent, create=False)
        if connection is None:
            return None
        row = connection.execute("SELECT sha256, signature FROM files WHERE name=?",
                                 (path.name,)).fetchone()
        if row is None or row["signature"] != signature(path):
            if hash_file is None:
                return "pending" if row else None
            sha256, _ = self.hash_file(path, hash_file)
        else:
            sha256 = row["sha256"]
        result = self.get(path, sha256, server)
        return result["status"] if result else "pending"

    def identity(self, path, sha256):
        connection = self.connection(path.resolve().parent, create=False)
        if connection is None:
            return None
        row = connection.execute("SELECT payload FROM identities WHERE sha256=?", (sha256,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_identity(self, path, sha256, identity):
        # Explicit CSV trust is restricted to the invocation that supplied it.
        if identity.get("source") == "csv-trusted":
            return
        connection = self.connection(path.resolve().parent)
        with connection:
            connection.execute("INSERT OR REPLACE INTO identities VALUES (?, ?)",
                               (sha256, json.dumps(identity, ensure_ascii=False)))

    def remove_identity(self, path, sha256):
        connection = self.connection(path.resolve().parent, create=False)
        if connection is None:
            return False
        with connection:
            removed = connection.execute("DELETE FROM identities WHERE sha256=?", (sha256,)).rowcount
            connection.execute("UPDATE uploads SET status='pending', context=NULL, error=NULL WHERE sha256=?",
                               (sha256,))
        return bool(removed)

    def pending(self, path, sha256, server):
        connection = self.connection(path.resolve().parent)
        with connection:
            connection.execute("""INSERT OR IGNORE INTO uploads
                (server, sha256, status, updated_at) VALUES (?, ?, 'pending', ?)""",
                               (server.rstrip("/"), sha256, timestamp()))

    def record(self, path, sha256, server, status, *, context=None, error=None,
               pdf=None, attempted=False):
        connection = self.connection(path.resolve().parent)
        old = self.get(path, sha256, server) or {}
        if status == "review-skipped":
            status = "needs-review"
        context = context if context is not None else old.get("context")
        doi = (pdf or {}).get("doi") or (context or {}).get("doi") or old.get("doi")
        payload = pdf if pdf is not None else old.get("pdf")
        with connection:
            connection.execute("INSERT OR REPLACE INTO uploads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                server.rstrip("/"), sha256, status, doi,
                json.dumps(context) if context is not None else None, error,
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                old.get("attempts", 0) + int(attempted), timestamp(),
            ))
            if status in {"uploaded", "existing", "replaced"} and doi:
                self.set_paper(path.resolve().parent, server, doi, True, pdf=payload)

    def rejection(self, path, sha256, server, context):
        record = self.get(path, sha256, server)
        if (record and record["status"] == "needs-review" and record["context"]
                and stable_context(record["context"]) == stable_context(context)):
            return record
        return None

    def set_paper(self, directory, server, doi, present, *, pdf=None):
        connection = self.connection(directory)
        with connection:
            connection.execute("INSERT OR REPLACE INTO papers VALUES (?, ?, ?, ?, ?)", (
                server.rstrip("/"), doi, int(present), timestamp(),
                json.dumps(pdf, ensure_ascii=False) if pdf else None,
            ))

    def papers(self, directory, server, dois):
        connection = self.connection(directory, create=False)
        if connection is None:
            return {}
        result = {}
        for doi in dois:
            row = connection.execute("SELECT present, checked_at FROM papers WHERE server=? AND doi=?",
                                     (server.rstrip("/"), doi)).fetchone()
            if row:
                result[doi] = dict(present=bool(row["present"]), checked_at=row["checked_at"],
                                   source="cached")
        return result

    def close(self):
        for connection in self.connections.values():
            connection.close()
        self.connections.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
