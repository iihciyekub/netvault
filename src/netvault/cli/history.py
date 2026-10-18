"""One append-only local audit stream for NetVault CLI operations.

No database or per-command output reports are created by default.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid

from netvault.cli.cache import cache_lock

SCHEMA_VERSION = 1
HISTORY_RELATIVE_PATH = Path(".netvault") / "history.jsonl"


def run_root(paths: list[Path]) -> Path:
    """Select a single journal for a recursive invocation."""
    roots = [p.expanduser().resolve() if p.is_dir() else p.expanduser().resolve().parent
             for p in paths]
    if not roots:
        return Path.cwd().resolve()
    if len(roots) == 1:
        return roots[0]
    shared = Path(os.path.commonpath([str(path) for path in roots]))
    # Unrelated paths on the same volume should not write to /.
    return Path.cwd().resolve() if shared == Path(shared.anchor) else shared


def file_signature(path: Path) -> list[int] | None:
    try:
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino]
    except OSError:
        return None


class History:
    def __init__(self, root: Path, cmd: str):
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / HISTORY_RELATIVE_PATH
        self.cmd = cmd
        self.run_id = uuid.uuid4().hex[:16]

    def file_key(self, value: Path | str | None) -> str:
        if value is None:
            return ""
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        # /var and /private/var refer to the same location on macOS.
        # Canonicalize before storing keys so retries survive that alias.
        path = path.resolve()
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def absolute_path(self, key: str) -> Path:
        path = Path(key)
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()

    def write(
        self, action: str, *, file: Path | str | None = None,
        target: Path | str | None = None, doi: str | None = None,
        result: str = "ok", reason: str | None = None,
        sha256: str | None = None, server: str | None = None,
        signature: list[int] | None = None, **extra,
    ) -> dict:
        record = {
            "v": SCHEMA_VERSION,
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run": self.run_id,
            "cmd": self.cmd,
            "action": action,
            "file": self.file_key(file),
            "result": result,
        }
        if target is not None:
            record["target"] = self.file_key(target)
        if doi:
            record["doi"] = doi
        if reason:
            record["reason"] = reason
        if sha256:
            record["sha256"] = sha256
        if server:
            record["server"] = server.rstrip("/")
        if signature is not None:
            record["signature"] = signature
        record.update(extra)
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(encoded) > 2_000_000:
            raise ValueError("History record is unexpectedly large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The same lock is used by compaction; do not lock the JSONL inode
        # itself, since atomic replacement changes that inode.
        with cache_lock(self.path):
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                size = os.lseek(fd, 0, os.SEEK_END)
                if size:
                    os.lseek(fd, -1, os.SEEK_END)
                    if os.read(fd, 1) != b"\n":
                        os.write(fd, b"\n")
                written = os.write(fd, encoded)
                if written != len(encoded):
                    raise OSError("Incomplete history journal write")
                os.fsync(fd)
            finally:
                os.close(fd)
        return record

    def records(self):
        if not self.path.is_file():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                # Ignore an incomplete tail produced by an interrupted write.
                if not line.endswith("\n"):
                    break
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(value, dict) and value.get("v") == SCHEMA_VERSION:
                    yield value

    def latest_uploads(self, server: str | None = None) -> dict[str, dict]:
        """Only the latest upload state for a server and path can be active."""
        latest = {}
        for row in self.records():
            if row.get("cmd") != "upload" or not row.get("file"):
                continue
            if row.get("action") not in {"upload", "replace", "error", "clear", "pending"}:
                continue
            if server is not None and row.get("server") != server.rstrip("/"):
                continue
            latest[row["file"]] = row
        return latest

    def compact_uploads(self) -> dict[str, int]:
        """Remove resolved upload state, retaining unresolved and audit events.

        Only the newest upload outcome per (server, file) survives. An old
        read-only SQLite file requires a tiny 'clear' tombstone to prevent an
        old rejection resurfacing; otherwise successful files disappear
        entirely. Atomic replacement preserves original data on failures.
        """
        if not self.path.exists():
            return {"before": 0, "after": 0}
        with cache_lock(self.path):
            records = list(self.records())
            latest_upload: dict[tuple[str, str], int] = {}
            latest_summary: dict[str, int] = {}
            latest_doi: dict[str, int] = {}
            for idx, row in enumerate(records):
                cmd, action = row.get("cmd"), row.get("action")
                if cmd == "upload" and action in {
                    "upload", "replace", "error", "pending", "clear",
                } and row.get("file"):
                    latest_upload[(row.get("server", ""), row["file"])] = idx
                elif cmd == "upload" and action == "summary":
                    latest_summary["upload"] = idx
                elif cmd == "doi" and action in {"confirm", "forget"} and row.get("sha256"):
                    latest_doi[row["sha256"]] = idx

            kept = []
            for idx, row in enumerate(records):
                cmd, action = row.get("cmd"), row.get("action")
                if cmd == "upload" and action in {
                    "upload", "replace", "error", "pending", "clear",
                } and row.get("file"):
                    if latest_upload[(row.get("server", ""), row["file"])] != idx:
                        continue
                    if action in {"upload", "replace", "clear"}:
                        old_sqlite = self.absolute_path(row["file"]).parent / ".netvault-upload.sqlite3"
                        if not old_sqlite.is_file():
                            continue
                        row = {key: row[key] for key in (
                            "v", "time", "run", "cmd", "file", "server", "signature",
                        ) if key in row}
                        row["action"], row["result"] = "clear", "ok"
                elif cmd == "upload" and action == "summary":
                    if latest_summary.get("upload") != idx:
                        continue
                elif cmd == "doi" and action in {"confirm", "forget"} and row.get("sha256"):
                    if latest_doi[row["sha256"]] != idx:
                        continue
                kept.append(row)

            if kept == records:
                return {"before": len(records), "after": len(kept)}
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=".history-", suffix=".tmp", delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    os.chmod(temporary, 0o600)
                    for row in kept:
                        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return {"before": len(records), "after": len(kept)}
