#!/usr/bin/env python3
"""Host-side NetVault admin queue worker (run from a root-owned cron entry).

Never accept arbitrary shell commands, executable paths, or CLI arguments from
the database. The only supported jobs are fixed, bounded maintenance workflows.
The FastAPI web service is not granted Docker access.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from contextlib import suppress
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from audit_pdf_maintenance import ROOT
from repair_pdf_bibliography import open_engine, validate_backup
from netvault_server.server.models import MaintenanceJob, Pdf, utc_now
from audit_pdf_maintenance import write_report

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv" / "bin" / "python"
TMP = ROOT / "tmp" / "admin-maintenance"
BACKUPS = Path("/root/netvault-data-backups")
MAX_REPORT_BYTES = 2_000_000
TIMEOUTS = {
    "audit": 300, "metadata_preview": 1800, "metadata_apply": 1800,
    "compression": 3600, "full_compression": 48 * 3600,
}
PROGRESS_FIELDS = ("total", "attempted", "replaced", "unchanged", "failed", "saved_bytes")
FULL_STATE = ROOT / "tmp" / "pdfcraft-unprocessed-state.json"
FULL_FIELDS = ("attempted", "replaced", "unchanged", "failed", "doi_written", "old_bytes", "new_bytes")


def atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def latest_manifest() -> Path | None:
    if not BACKUPS.is_dir():
        return None
    candidates = sorted(BACKUPS.glob("20??????T??????Z/manifest.txt"), reverse=True)
    return candidates[0] if candidates else None


def heartbeat() -> None:
    backup = latest_manifest()
    created = backup.parent.name if backup else None
    atomic_json(TMP / "worker.json", {
        "heartbeat_epoch": time.time(),
        "backup_created_utc": created,
    })


def _fail_interrupted(db: Session) -> None:
    """Only called once the exclusive worker lock proves no host job is alive."""
    running = db.scalars(select(MaintenanceJob).where(MaintenanceJob.status == "running")).all()
    for job in running:
        job.status = "failed"
        job.last_error = "Host worker restarted before this task completed; verify its log and report."
        job.finished_at = utc_now()
    if running:
        db.commit()


def claim(db: Session) -> str | None:
    query = (select(MaintenanceJob).where(MaintenanceJob.status == "queued")
             .order_by(MaintenanceJob.created_at, MaintenanceJob.id).limit(1))
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    job = db.scalar(query)
    if not job:
        return None
    job.status = "running"
    job.started_at = utc_now()
    db.commit()
    return job.id


def _load_source(db: Session, job: MaintenanceJob, expected: str) -> dict:
    from netvault_server.server.maintenance import load_report

    source = db.get(MaintenanceJob, job.source_job_id)
    if not source or source.kind != expected or source.status != "completed":
        raise ValueError("The approved preview is unavailable.")
    options = json.loads(job.options_json)
    if source.result_sha256 != options.get("approved_digest"):
        raise ValueError("The approved preview checksum has changed.")
    end = source.finished_at
    if end is None:
        raise ValueError("The preview has no completion time.")
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if not timedelta(0) <= utc_now() - end <= timedelta(minutes=60):
        raise ValueError("The approved preview expired.")
    report = load_report(source)
    if report is None:
        raise ValueError("The approved preview report cannot be verified.")
    return report


def _safe_backup() -> Path:
    manifest = latest_manifest()
    if manifest is None:
        raise ValueError("No complete backup snapshot found.")
    validate_backup(manifest)
    if shutil.disk_usage(ROOT).free < 5 * 1024**3:
        raise ValueError("Less than 5 GiB storage free; refusing write operation.")
    return manifest


def _approved_candidates(report: dict, limit: int) -> list[dict]:
    if report.get("read_only") is not True:
        raise ValueError("Compression approval requires a read-only audit.")
    entries = report.get("compression_candidates", [])
    if not isinstance(entries, list) or not 1 <= limit <= min(10, len(entries)):
        raise ValueError("Invalid approved compression count.")
    rows = []
    ids = set()
    for entry in entries[:limit]:
        ident, sha = entry.get("id"), entry.get("sha256")
        if type(ident) is not int or ident <= 0 or not isinstance(sha, str):
            raise ValueError("Invalid candidate identity.")
        if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha) or ident in ids:
            raise ValueError("Invalid or duplicate candidate fingerprint.")
        rows.append({"id": ident, "sha256": sha})
        ids.add(ident)
    return rows


def command_for(db: Session, job: MaintenanceJob, manifest: Path | None) -> list[str]:
    """Build an argument vector from exact job types; never shell=True."""
    options = json.loads(job.options_json)
    if job.kind == "full_compression":
        if options.get("no_backup") is not True or type(options.get("jobs")) is not int:
            raise ValueError("Full-library authorization is invalid.")
        if not 1 <= options["jobs"] <= 4:
            raise ValueError("Full-library worker count is invalid.")
        return [str(PYTHON), "scripts/compress_pdfs_parallel.py",
                "--all", "--jobs", str(options["jobs"])]
    limit = options["limit"]
    if type(limit) is not int:
        raise ValueError("Invalid stored task limit.")
    output = TMP / f"{job.id}.json"
    if job.kind == "audit" and 1 <= limit <= 100:
        return [str(PYTHON), "scripts/audit_pdf_maintenance.py",
                "--min-age-hours", "2", "--recent-days", "7",
                "--limit", str(limit), "--output", str(output)]
    if job.kind == "metadata_preview" and 1 <= limit <= 10:
        return [str(PYTHON), "scripts/repair_pdf_bibliography.py",
                "--limit", str(limit), "--output", str(output),
                "--progress-output", str(TMP / f"{job.id}.progress.json")]
    if job.kind == "metadata_apply" and 1 <= limit <= 10 and manifest:
        report = _load_source(db, job, "metadata_preview")
        if report.get("mode") != "preview" or report.get("verified", 0) < 1:
            raise ValueError("No verified metadata to apply.")
        approved = TMP / f"{job.source_job_id}.json"
        return [str(PYTHON), "scripts/repair_pdf_bibliography.py",
                "--limit", str(limit), "--approved-proposals", str(approved),
                "--apply", "--confirm-apply", "--backup-manifest", str(manifest),
                "--output", str(output),
                "--progress-output", str(TMP / f"{job.id}.progress.json")]
    if job.kind == "compression" and 1 <= limit <= 10 and manifest:
        report = _load_source(db, job, "audit")
        candidates = _approved_candidates(report, limit)
        manifest_path = TMP / f"{job.id}.approved.json"
        atomic_json(manifest_path, {"candidates": candidates})
        return [str(PYTHON), "scripts/compress_pdfs_parallel.py",
                "--pilot", "--limit", str(limit), "--jobs", "1",
                "--approved-candidates", str(manifest_path),
                "--result-output", str(output),
                "--progress-output", str(TMP / f"{job.id}.progress.json")]
    raise ValueError("Unrecognized or unsafe maintenance task.")


def _read_full_state() -> dict:
    if FULL_STATE.is_symlink():
        raise ValueError("Unexpected PDFCraft state symlink.")
    if not FULL_STATE.is_file():
        return {"full": {}, "full_cursor": 0}
    if FULL_STATE.stat().st_size > 200_000:
        raise ValueError("PDFCraft state file is too large.")
    state = json.loads(FULL_STATE.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("full", {}), dict):
        raise ValueError("PDFCraft state file has invalid structure.")
    return state


def _full_counts(state: dict) -> dict:
    full = state.get("full", {})
    fields = {}
    for field in FULL_FIELDS:
        value = full.get(field, 0)
        if type(value) is not int or value < 0:
            raise ValueError("Invalid PDFCraft cumulative counter.")
        fields[field] = value
    return fields


def prepare_full_run(db: Session, job: MaintenanceJob) -> None:
    """Snapshot the existing cumulative counters and fresh backlog (no writes)."""
    if shutil.disk_usage(ROOT).free < 5 * 1024**3:
        raise ValueError("Less than 5 GiB free; refusing full PDF optimization.")
    state = _read_full_state()
    cursor = int(state.get("full_cursor", 0))
    if cursor < 0:
        raise ValueError("Invalid PDFCraft resume cursor.")
    eligible = db.scalar(
        select(func.count(Pdf.id)).where(
            Pdf.is_deleted.is_(False), Pdf.id > cursor,
            or_(
                Pdf.pdfcraft_processed_sha256.is_distinct_from(Pdf.sha256),
                Pdf.pdfcraft_ppi.is_distinct_from(150),
                Pdf.pdfcraft_quality.is_distinct_from(75),
            ),
        )
    )
    baseline = {
        "mode": "full_compression", "total": int(eligible or 0),
        "baseline": _full_counts(state),
        "last_full_scan_at": state.get("last_full_scan_at"),
        "resume_cursor": cursor,
    }
    atomic_json(TMP / f"{job.id}.baseline.json", baseline)


def full_snapshot(job: MaintenanceJob) -> dict:
    """Derive job-local progress from the legacy script's checkpointed totals."""
    path = TMP / f"{job.id}.baseline.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("Full-library progress baseline is unavailable.")
    base = json.loads(path.read_text(encoding="utf-8"))
    if base.get("mode") != "full_compression" or type(base.get("total")) is not int:
        raise ValueError("Full-library progress baseline is invalid.")
    current = _read_full_state()
    counters = _full_counts(current)
    delta = {}
    for key in FULL_FIELDS:
        before = base["baseline"][key]
        if counters[key] < before:
            raise ValueError("PDFCraft counters went backwards; another worker changed the state.")
        delta[key] = counters[key] - before
    total = base["total"]
    # New uploads may arrive after the initial scan, but the script is bounded
    # by its initial max(id); it never reprocesses completed markers.
    return {
        "mode": "full_compression", "total": total,
        "attempted": delta["attempted"], "replaced": delta["replaced"],
        "unchanged": delta["unchanged"], "failed": delta["failed"],
        "doi_written": delta["doi_written"],
        "saved_bytes": max(0, delta["old_bytes"] - delta["new_bytes"]),
        "completed_sweep": bool(
            current.get("last_full_scan_at")
            and current.get("last_full_scan_at") != base.get("last_full_scan_at")
        ),
    }


def refresh_progress(db: Session, job: MaintenanceJob) -> None:
    """Publish only bounded aggregate counters; never reveal host logs in the UI."""
    if job.kind == "full_compression":
        try:
            progress = full_snapshot(job)
            summary = {
                key: progress[key] for key in (
                    "total", "attempted", "replaced", "unchanged", "failed",
                    "saved_bytes", "doi_written",
                )
            }
            encoded = json.dumps(summary, sort_keys=True)
            if job.result_json != encoded:
                job.result_json = encoded
                db.commit()
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass
        return
    if job.kind not in {"compression", "metadata_preview", "metadata_apply"}:
        return
    path = TMP / f"{job.id}.progress.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return
        values = json.loads(path.read_text(encoding="utf-8"))
        expected_mode = "approved_compression" if job.kind == "compression" else job.kind
        if values.get("mode") != expected_mode:
            return
        total = json.loads(job.options_json)["limit"]
        if type(values.get("total")) is not int or not 0 <= values["total"] <= total:
            return
        fields = PROGRESS_FIELDS if job.kind == "compression" else (
            "total", "processed", "verified", "applied",
        )
        counters = {key: values[key] for key in fields}
        if any(type(value) is not int or value < 0 for value in counters.values()):
            return
        if job.kind == "compression":
            if counters["total"] != total or counters["attempted"] > total:
                return
        elif (counters["processed"] > counters["total"]
              or counters["verified"] > counters["processed"]
              or counters["applied"] > counters["verified"]):
            return
        new_summary = json.dumps(counters, sort_keys=True)
        if job.result_json != new_summary:
            job.result_json = new_summary
            db.commit()
    except (ValueError, KeyError, OSError, TypeError):
        return


def terminate_group(process: subprocess.Popen) -> None:
    """Stop the entire script tree, not only its Python coordinator."""
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=45)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_host_command(
    cmd: list[str], logfile, *, kind: str, db: Session,
    job: MaintenanceJob, worker_lock_fd: int | None,
) -> int:
    """Refresh heartbeat while active; preserve the OS lock in orphaned children."""
    inherited = (worker_lock_fd,) if worker_lock_fd is not None else ()
    process = subprocess.Popen(
        cmd, cwd=REPO, stdout=logfile, stderr=subprocess.STDOUT,
        start_new_session=True, pass_fds=inherited,
    )
    deadline = time.monotonic() + TIMEOUTS[kind]
    stop_sent_at = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, TIMEOUTS[kind])
            try:
                return process.wait(timeout=min(10, remaining))
            except subprocess.TimeoutExpired:
                heartbeat()
                refresh_progress(db, job)
                if kind == "full_compression":
                    # The coordinator drains already running PDFs and writes
                    # its cursor before exiting. Do not signal child pdfcraft
                    # processes directly unless graceful drain times out.
                    db.refresh(job, attribute_names=["cancel_requested"])
                    if job.cancel_requested and stop_sent_at is None:
                        process.send_signal(signal.SIGTERM)
                        stop_sent_at = time.monotonic()
                    if stop_sent_at is not None and time.monotonic() - stop_sent_at > 1200:
                        raise RuntimeError("Full-library stop exceeded the graceful drain limit.")
    except BaseException:
        terminate_group(process)
        raise
    finally:
        heartbeat()
        refresh_progress(db, job)


def execute(db: Session, job_id: str, worker_lock_fd: int | None = None) -> None:
    job = db.get(MaintenanceJob, job_id)
    if job is None or job.status != "running":
        return
    try:
        manifest = _safe_backup() if job.kind in {"metadata_apply", "compression"} else None
        cmd = command_for(db, job, manifest)
        if job.kind == "full_compression":
            prepare_full_run(db, job)
        if not PYTHON.is_file():
            raise RuntimeError("Host Python environment is missing.")
        log_path = TMP / f"{job.id}.log"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as logfile:
            try:
                status = run_host_command(
                    cmd, logfile, kind=job.kind, db=db, job=job,
                    worker_lock_fd=worker_lock_fd,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Task exceeded the host execution time limit.") from exc
        result_path = TMP / f"{job.id}.json"
        if job.kind == "full_compression":
            snapshot = full_snapshot(job)
            write_report(result_path, snapshot)
        if status and not result_path.is_file():
            raise RuntimeError(f"Maintenance process exited with status {status}; see host log.")
        if result_path.is_symlink() or not result_path.is_file():
            raise ValueError("Maintenance report was not generated.")
        if result_path.stat().st_size > MAX_REPORT_BYTES:
            raise ValueError("Maintenance report is too large.")
        data = result_path.read_bytes()
        report = json.loads(data)
        if not isinstance(report, dict):
            raise ValueError("Maintenance report format is invalid.")
        if job.kind == "audit":
            if report.get("read_only") is not True:
                raise ValueError("Audit did not mark its output as read-only.")
            summary = {**report.get("counts", {}),
                       "candidates": len(report.get("compression_candidates", []))}
        elif job.kind in {"metadata_preview", "metadata_apply"}:
            expected = "preview" if job.kind == "metadata_preview" else "apply"
            if report.get("mode") != expected:
                raise ValueError("Metadata report mode mismatch.")
            summary = {field: report.get(field) for field in ("selected_rows", "verified", "applied")}
        elif job.kind == "compression":
            if report.get("mode") != "approved_compression":
                raise ValueError("Compression report mode mismatch.")
            summary = {field: report.get(field) for field in (
                "attempted", "replaced", "unchanged", "failed", "saved_bytes",
            )}
        elif job.kind == "full_compression":
            if report.get("mode") != "full_compression":
                raise ValueError("Full-library report mode mismatch.")
            summary = {field: report.get(field) for field in (
                "total", "attempted", "replaced", "unchanged",
                "failed", "saved_bytes", "doi_written",
            )}
        else:
            raise ValueError("Unexpected maintenance job type.")
        job.result_json = json.dumps(summary, ensure_ascii=False)[:10000]
        job.result_sha256 = hashlib.sha256(data).hexdigest()
        if job.kind == "full_compression":
            db.refresh(job, attribute_names=["cancel_requested"])
            if report.get("completed_sweep") and status == 0:
                job.status = "completed"
            elif job.cancel_requested:
                job.status = "cancelled"
                job.last_error = "Stopped by administrator. Remaining PDF work is resumable."
            else:
                job.status = "failed"
                job.last_error = "Full sweep ended before completion; rerun to resume."
        else:
            job.status = "failed" if status else "completed"
        if status:
            job.last_error = f"Maintenance process exited with status {status}; review partial report and host log."
    except Exception as exc:
        job.status = "failed"
        # Keep paths, SQL URLs and other process details out of the browser.
        job.last_error = str(exc)[:240] if isinstance(exc, (ValueError, RuntimeError)) else "Host task failed; check the worker log."
    finally:
        job.finished_at = utc_now()
        db.commit()
        heartbeat()


def main() -> int:
    if os.geteuid() != 0 and REPO == Path("/root/iiaide/netvault"):
        raise SystemExit("The host maintenance worker must run under its configured service account.")
    TMP.mkdir(parents=True, exist_ok=True)
    with (TMP / "worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        engine = open_engine()
        try:
            with Session(engine) as db:
                _fail_interrupted(db)
                heartbeat()
                job_id = claim(db)
                if job_id:
                    execute(db, job_id, lock.fileno())
        finally:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
