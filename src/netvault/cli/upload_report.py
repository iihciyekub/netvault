"""Atomic upload checkpoints, including failures and work still pending."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic


class UploadReport:
    def __init__(self, path: Path, pdfs: list[Path]):
        self.path = path.resolve()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.rows = {str(p.resolve()): {"path": str(p.resolve()), "status": "pending"} for p in pdfs}
        self.last_saved = 0.0
        self.checkpoint(force=True)

    def record(self, path: Path, status: str, *, sha256: str | None = None,
               pdf: dict | None = None, error: str | None = None):
        row = self.rows[str(path.resolve())]
        row.update(status=status, updated_at=datetime.now(timezone.utc).isoformat())
        if sha256:
            row["sha256"] = sha256
        if pdf:
            row.update({k: pdf.get(k) for k in ("id", "doi", "title", "container_title")})
        if error:
            row["error"] = error
        self.checkpoint(force=status == "failed")

    def checkpoint(self, *, force: bool = False, finished: bool = False):
        if not force and monotonic() - self.last_saved < 1:
            return
        summary = {status: sum(r["status"] == status for r in self.rows.values())
                   for status in ("pending", "uploaded", "existing", "replaced", "failed", "local-duplicate")}
        summary.update(found_paths=len(self.rows), processed=len(self.rows) - summary["pending"])
        payload = {"version": 1, "started_at": self.started_at,
                   "updated_at": datetime.now(timezone.utc).isoformat(), "finished": finished,
                   "summary": summary, "files": list(self.rows.values())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.last_saved = monotonic()


def failed_paths(report_path: Path) -> set[Path]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("files"), list):
        raise ValueError("Unsupported upload report")
    return {Path(row["path"]).resolve() for row in payload["files"]
            if row.get("status") in {"failed", "pending"}}
