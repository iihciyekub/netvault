"""Optional legacy JSON export with one shared JSONL history as the default."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from netvault.cli.cache import atomic_json
from netvault.cli.history import History, file_signature


class UploadReport:
    """Keep in-memory per-file outcomes; write only noteworthy history events.

    Legacy --report PATH still explicitly exports the old JSON structure for
    automation scripts. Normal nv upload does not create any report JSON.
    """

    def __init__(self, path: Path | None, pdfs: list[Path], *,
                 history: History | None = None, warnings: list[str] | None = None):
        self.path = Path(path).resolve() if path is not None else (
            history.path if history is not None else None
        )
        self.legacy_json_path = Path(path).resolve() if path is not None else None
        self.history = history
        self.server: str | None = None
        self.warnings = warnings if warnings is not None else []
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.rows = {str(p.resolve()): {"path": str(p.resolve()), "status": "pending"} for p in pdfs}
        self.counts = Counter(pending=len(self.rows))
        self.state_files = []
        self._active_keys: set[str] | None = None
        self.checkpoint(force=True)

    def _active_uploads(self) -> set[str]:
        if self._active_keys is None:
            latest = self.history.latest_uploads(self.server) if self.history else {}
            self._active_keys = {
                key for key, value in latest.items()
                if value.get("action") in {"error", "pending"}
            }
        return self._active_keys

    def record_pending(self, path: Path, *, doi: str | None = None,
                       sha256: str | None = None) -> None:
        """Only actual network candidates enter the retryable pending queue."""
        if self.history:
            self.history.write(
                "pending", file=path, server=self.server, doi=doi,
                sha256=sha256, result="pending", signature=file_signature(path),
            )
            self._active_uploads().add(self.history.file_key(path))


    def record(self, path: Path, status: str, *, sha256: str | None = None,
               pdf: dict | None = None, error: str | None = None,
               metadata_writeback: dict | None = None):
        row = self.rows[str(path.resolve())]
        self.counts[row["status"]] -= 1
        self.counts[status] += 1
        row.update(status=status, updated_at=datetime.now(timezone.utc).isoformat())
        if sha256:
            row["sha256"] = sha256
        if pdf:
            row.update({k: pdf.get(k) for k in ("id", "doi", "title", "container_title")})
        if error:
            row["error"] = error
        if metadata_writeback is not None:
            row["metadata_writeback"] = metadata_writeback
        if self.history and status in {"uploaded", "replaced", "existing",
                                       "failed", "needs-review"}:
            key = self.history.file_key(path)
            active = self._active_uploads()
            if status in {"failed", "needs-review"}:
                self.history.write(
                    "error", file=path, result="review" if status == "needs-review" else "failed",
                    doi=row.get("doi"), sha256=sha256, reason=error,
                    server=self.server, signature=file_signature(path),
                )
                active.add(key)
            else:
                if key not in active and (path.resolve().parent / ".netvault-upload.sqlite3").is_file():
                    from netvault.cli.upload_state import UploadState, RETRY_STATUSES
                    with UploadState(root=self.history.root, record_events=False) as state:
                        if state._legacy_status(path, self.server) in RETRY_STATUSES:
                            active.add(key)
                if key in active:
                    self.history.write(
                        "clear", file=path, result="ok",
                        server=self.server, signature=file_signature(path),
                    )
                    active.discard(key)

    def record_metadata_writeback(self, path: Path, result: dict):
        self.rows[str(path.resolve())]["metadata_writeback"] = dict(result)
        self.checkpoint()

    def checkpoint(self, *, force: bool = False, finished: bool = False):
        if not self.legacy_json_path:
            if finished and self.history:
                summary = {
                    status: self.counts[status]
                    for status in ("pending", "uploaded", "existing", "replaced", "failed",
                                   "local-duplicate", "needs-review", "review-skipped")
                }
                self.history.write(
                    "summary", result="ok" if not (summary["failed"] or summary["needs-review"])
                    else "partial", counts=summary, scanned=len(self.rows),
                )
            return
        summary = {status: self.counts[status]
                   for status in ("pending", "uploaded", "existing", "replaced", "failed",
                                  "local-duplicate", "needs-review", "review-skipped")}
        summary.update(found_paths=len(self.rows), processed=len(self.rows) - summary["pending"])
        payload = {"version": 1, "started_at": self.started_at,
                   "updated_at": datetime.now(timezone.utc).isoformat(), "finished": finished,
                   "summary": summary, "files": list(self.rows.values())}
        if self.warnings:
            payload["warnings"] = self.warnings
        if self.state_files:
            payload["state_files"] = self.state_files
        atomic_json(self.legacy_json_path, payload, compact=False)
        if finished and self.history:
            self.history.write(
                "summary", result="ok" if not (summary["failed"] or summary["needs-review"])
                else "partial", counts=summary, scanned=len(self.rows),
            )

    def __enter__(self):
        return self

    def __exit__(self, exception_type, *_):
        if exception_type is not None:
            self.checkpoint(force=True)


def failed_paths(report_path: Path) -> set[Path]:
    """Legacy explicit --report JSON input for --retry-failed."""
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("files"), list):
        raise ValueError("Unsupported upload report")
    return {Path(row["path"]).resolve() for row in payload["files"]
            if row.get("status") in {"failed", "pending", "needs-review", "review-skipped"}}
