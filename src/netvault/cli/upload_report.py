"""Atomic upload checkpoints, including failures and work still pending."""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from netvault.cli.cache import atomic_json


class UploadReport:
    def __init__(self, path: Path, pdfs: list[Path], *, warnings: list[str] | None = None):
        self.path = path.resolve()
        self.warnings = warnings if warnings is not None else []
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.rows = {str(p.resolve()): {"path": str(p.resolve()), "status": "pending"} for p in pdfs}
        self.counts = Counter(pending=len(self.rows))
        self.last_saved = 0.0
        self.checkpoint(force=True)

    def record(self, path: Path, status: str, *, sha256: str | None = None,
               pdf: dict | None = None, error: str | None = None):
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
        self.checkpoint()

    def checkpoint(self, *, force: bool = False, finished: bool = False):
        if not force and monotonic() - self.last_saved < 1:
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
        atomic_json(self.path, payload, compact=False)
        self.last_saved = monotonic()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, *_):
        if exception_type is not None:
            self.checkpoint(force=True)


def failed_paths(report_path: Path) -> set[Path]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("files"), list):
        raise ValueError("Unsupported upload report")
    return {Path(row["path"]).resolve() for row in payload["files"]
            if row.get("status") in {"failed", "pending", "needs-review", "review-skipped"}}
