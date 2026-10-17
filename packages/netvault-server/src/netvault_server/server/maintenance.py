"""Admin-only maintenance overview, durable job approval and report access.

The web container NEVER runs shell commands. A separate root-owned host worker
claims whitelisted jobs from PostgreSQL and operates on the local PDF storage.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from netvault_server.server.config import get_settings
from netvault_server.server.models import MaintenanceJob, Pdf, User, utc_now

REPORT_BYTES_LIMIT = 2_000_000
REVIEW_TTL = timedelta(minutes=60)
OPEN_STATUSES = ("queued", "running")
KINDS = {"audit", "metadata_preview"}
APPROVAL_WORDS = {"metadata_apply": "APPLY METADATA", "compression": "COMPRESS PDFS"}
FULL_CONFIRMATION = "RUN FULL LIBRARY"
STOP_CONFIRMATION = "STOP FULL RUN"


def reports_dir() -> Path:
    return get_settings().storage_root / "tmp" / "admin-maintenance"


def report_path(job_id: str) -> Path:
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise ValueError("Invalid job identifier")
    return reports_dir() / f"{job_id}.json"


def sanitized_full_log(job_id: str) -> list[str]:
    """Show only selected PDFCraft status lines, never raw host stderr or secrets."""
    path = report_path(job_id).with_suffix(".log")
    try:
        if path.is_symlink() or not path.is_file():
            return []
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 64 * 1024))
            data = stream.read(64 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return []
    accepted = []
    for line in data.splitlines():
        match = re.match(
            r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[^ ]*)\s+"
            r"(START|OK id=|SKIP id=|STOPPED|END)(.*)$", line,
        )
        if not match:
            continue
        date, verb, rest = match.groups()
        if verb == "SKIP id=":
            number = re.match(r"(\d+)", rest)
            if not number:
                continue
            accepted.append(f"{date} SKIP id={number.group(1)} (inspect host error log)")
        else:
            # Numeric counters and known PDFCraft status only. Avoid passing
            # arbitrary subprocess exceptions to the web.
            rest = re.sub(r"[^a-zA-Z0-9_ =:>{},.\-]", "", rest[:220])
            accepted.append(f"{date} {verb}{rest}")
    return accepted[-60:]


def load_report(job: MaintenanceJob) -> dict | None:
    if job.status not in ("completed", "failed") or not job.result_sha256:
        return None
    path = report_path(job.id)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > REPORT_BYTES_LIMIT:
        return None
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != job.result_sha256:
        return None
    result = json.loads(data)
    return result if isinstance(result, dict) else None


def _aware(date: datetime | None) -> datetime | None:
    if date is not None and date.tzinfo is None:
        return date.replace(tzinfo=timezone.utc)
    return date


def worker_state() -> tuple[dict, bool]:
    """Only use the host's heartbeat as liveness, not as backup verification."""
    worker: dict = {}
    path = reports_dir() / "worker.json"
    try:
        if path.is_file() and not path.is_symlink() and path.stat().st_size < 4096:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                worker = parsed
    except (ValueError, OSError):
        pass
    heartbeat = worker.get("heartbeat_epoch", 0)
    try:
        online = 0 <= time.time() - float(heartbeat) <= 180
    except (ValueError, TypeError):
        online = False
    return worker, online


def worker_is_online() -> bool:
    return worker_state()[1]


def overview(db: Session) -> dict:
    processed = (
        Pdf.pdfcraft_processed_sha256 == Pdf.sha256,
        Pdf.pdfcraft_ppi == 150,
        Pdf.pdfcraft_quality == 75,
    )
    missing = or_(
        Pdf.crossref_status.is_distinct_from("ok"),
        Pdf.title.is_(None),
        func.trim(Pdf.title) == "",
    )
    count, total_bytes, compressed, missing_metadata = db.execute(
        select(
            func.count(Pdf.id),
            func.coalesce(func.sum(Pdf.size), 0),
            func.coalesce(func.sum(case((and_(*processed), 1), else_=0)), 0),
            func.coalesce(func.sum(case((missing, 1), else_=0)), 0),
        ).where(Pdf.is_deleted.is_(False))
    ).one()
    database_bytes = None
    if db.get_bind().dialect.name == "postgresql":
        database_bytes = db.scalar(text("SELECT pg_database_size(current_database())"))
    free_bytes = None
    try:
        free_bytes = shutil.disk_usage(get_settings().storage_root).free
    except OSError:
        pass

    worker, worker_online = worker_state()
    return {
        "documents": int(count),
        "pdf_bytes": int(total_bytes),
        "processed": int(compressed),
        "pending_compression": int(count) - int(compressed),
        "missing_metadata": int(missing_metadata),
        "database_bytes": int(database_bytes) if database_bytes is not None else None,
        "disk_free_bytes": free_bytes,
        "worker_online": worker_online,
        "last_backup": worker.get("backup_created_utc") if worker_online else None,
    }


def recent_jobs(db: Session, limit: int = 30, offset: int = 0) -> list[dict]:
    rows = db.execute(
        select(MaintenanceJob, User.username)
        .join(User, User.id == MaintenanceJob.created_by_id)
        .order_by(MaintenanceJob.created_at.desc(), MaintenanceJob.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    sources = [job.id for job, _username in rows
               if job.status == "completed" and job.kind in KINDS]
    approved_sources = set()
    if sources:
        approved_sources = {
            source_id for (source_id,) in db.execute(
                select(MaintenanceJob.source_job_id)
                .where(MaintenanceJob.source_job_id.in_(sources))
            ).all()
        }
    result = []
    for job, username in rows:
        summary = {}
        if job.result_json:
            try:
                summary = json.loads(job.result_json)
            except ValueError:
                pass
        result.append({
            "id": job.id, "kind": job.kind, "status": job.status,
            "cancel_requested": bool(job.cancel_requested),
            "username": username, "created_at": job.created_at,
            "started_at": job.started_at, "finished_at": job.finished_at,
            "source_job_id": job.source_job_id,
            "summary": summary, "error": job.last_error,
            "has_report": job.status in ("completed", "failed") and bool(job.result_sha256),
            "reviewable": (
                job.status == "completed" and job.kind in ("audit", "metadata_preview")
                and job.id not in approved_sources and bool(job.result_sha256)
                and bool(_aware(job.finished_at))
                and timedelta(0) <= utc_now() - _aware(job.finished_at) <= REVIEW_TTL
            ),
        })
    return result


def job_count(db: Session) -> int:
    return int(db.scalar(select(func.count(MaintenanceJob.id))) or 0)


def jobs_revision(jobs: list[dict]) -> str:
    """Revision changes on both task transitions and compression progress."""
    return ";".join(
        f"{job['id']}:{job['status']}:"
        f"{job['summary'].get('attempted', job['summary'].get('processed', 0))}:"
        f"{int(job.get('cancel_requested', False))}"
        for job in jobs[:5]
    )


def _save_queued(db: Session, job: MaintenanceJob) -> MaintenanceJob:
    if db.scalar(select(MaintenanceJob.id).where(MaintenanceJob.status.in_(OPEN_STATUSES)).limit(1)):
        raise HTTPException(409, "Another maintenance task is already queued or running.")
    db.add(job)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "A maintenance task or approval already exists.") from exc
    return job


def enqueue_preview(db: Session, user_id: int, kind: str, limit: int) -> MaintenanceJob:
    if kind not in KINDS:
        raise HTTPException(400, "Unknown maintenance task.")
    if kind == "audit":
        if not 1 <= limit <= 100:
            raise HTTPException(400, "Audit limit must be 1 to 100.")
    elif not 1 <= limit <= 10:
        raise HTTPException(400, "Metadata preview limit must be 1 to 10.")
    job = MaintenanceJob(
        id=uuid4().hex, kind=kind, status="queued", created_by_id=user_id,
        singleton_key="maintenance", options_json=json.dumps({"limit": limit}),
    )
    return _save_queued(db, job)


def enqueue_full_library(
    db: Session, user_id: int, *,
    confirmation: str, acknowledge_no_backup: bool, jobs: int,
) -> MaintenanceJob:
    """Explicit admin approval for the existing idempotent --all PDFCraft sweep.

    This mode intentionally does not create or require backups (as requested).
    It may delete replaced originals; the UI and confirmation must disclose it.
    """
    if confirmation != FULL_CONFIRMATION or not acknowledge_no_backup:
        raise HTTPException(400, "Confirm the irreversible full-library operation.")
    if type(jobs) is not int or not 1 <= jobs <= 4:
        raise HTTPException(400, "Parallel workers must be between 1 and 4.")
    if not worker_is_online():
        raise HTTPException(503, "The host maintenance worker is offline.")
    task = MaintenanceJob(
        id=uuid4().hex, kind="full_compression", status="queued",
        created_by_id=user_id, singleton_key="maintenance",
        options_json=json.dumps({"jobs": jobs, "no_backup": True}),
    )
    return _save_queued(db, task)


def stop_full_library(db: Session, job_id: str, *, confirmation: str) -> None:
    """Request a cooperative stop; leave the job locked until the host exits."""
    if confirmation != STOP_CONFIRMATION:
        raise HTTPException(400, "Type STOP FULL RUN to confirm cancellation.")
    query = select(MaintenanceJob).where(MaintenanceJob.id == job_id)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    job = db.scalar(query)
    if job is None or job.kind != "full_compression":
        raise HTTPException(404, "Full-library job not found.")
    if job.status == "queued":
        job.status = "cancelled"
        job.cancel_requested = True
        job.finished_at = utc_now()
    elif job.status == "running":
        job.cancel_requested = True
    else:
        raise HTTPException(409, "This job is no longer active.")
    db.commit()


def approve(
    db: Session, user_id: int, source_id: str, operation: str, confirmation: str,
    limit: int = 2,
) -> MaintenanceJob:
    if operation not in APPROVAL_WORDS or confirmation != APPROVAL_WORDS[operation]:
        raise HTTPException(400, "Type the exact confirmation phrase to proceed.")
    source = db.get(MaintenanceJob, source_id)
    expected = "metadata_preview" if operation == "metadata_apply" else "audit"
    if source is None or source.kind != expected or source.status != "completed":
        raise HTTPException(409, "A completed matching preview is required.")
    finished = _aware(source.finished_at)
    if not finished or not timedelta(0) <= utc_now() - finished <= REVIEW_TTL:
        raise HTTPException(409, "The preview has expired. Run a new preview.")
    if db.scalar(select(MaintenanceJob.id).where(MaintenanceJob.source_job_id == source_id)):
        raise HTTPException(409, "This preview has already been approved.")
    try:
        report = load_report(source)
    except (OSError, ValueError, json.JSONDecodeError):
        report = None
    if report is None:
        raise HTTPException(409, "The preview report is missing or has changed.")
    if operation == "metadata_apply":
        if report.get("mode") != "preview" or not 1 <= report.get("verified", 0) <= 10:
            raise HTTPException(409, "No verified metadata changes to approve.")
        limit = int(report.get("selected_rows", 0))
        if not 1 <= limit <= 10:
            raise HTTPException(409, "Metadata preview size is invalid.")
    else:
        if report.get("read_only") is not True:
            raise HTTPException(409, "Only a read-only audit can authorize compression.")
        eligible = report.get("compression_candidates")
        if not isinstance(eligible, list) or not eligible:
            raise HTTPException(409, "No compression candidates were found.")
        if not 1 <= limit <= min(10, len(eligible)):
            raise HTTPException(400, "Compression limit exceeds the reviewed candidates.")
    job = MaintenanceJob(
        id=uuid4().hex, kind=operation, status="queued", created_by_id=user_id,
        source_job_id=source_id, singleton_key="maintenance",
        options_json=json.dumps({"limit": limit, "approved_digest": source.result_sha256}),
    )
    return _save_queued(db, job)
