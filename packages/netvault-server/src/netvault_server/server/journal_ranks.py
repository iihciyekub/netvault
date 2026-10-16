"""Shared PostgreSQL rank cache and resumable, nonblocking journal-list jobs.

Every authenticated user can populate the same cache. No external provider
lookup is needed for cached journals unless refresh=True is explicit.
"""
import json
import hashlib
import logging
import threading
import time
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from netvault_server.server import easyscholar
from netvault_server.server.database import SessionLocal
from netvault_server.server.journal_filters import (
    deduplicate_journal_names,
    effective_journal_names,
    get_user_filter_override,
    is_custom_filter_key,
    normalize_filter_key,
)
from netvault_server.server.models import JournalRankBatchJob, JournalRankCache, utc_now

logger = logging.getLogger(__name__)
_WAKE = threading.Event()
_START_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_MAX_BATCH_JOURNALS = 2000
_MAX_OPEN_JOBS = 3


def journal_key(name: str) -> str:
    normalized = " ".join(name.split())
    if not normalized or len(normalized) > 300 or any(ord(char) < 32 for char in normalized):
        raise ValueError("Choose a valid journal name.")
    return normalized.casefold()


def cached_rank(db: Session, name: str) -> dict | None:
    row = db.get(JournalRankCache, journal_key(name))
    if row is None:
        return None
    return {
        "journal_name": row.journal_name,
        "rows": json.loads(row.rows_json),
        "queried_at": row.queried_at,
        "cached": True,
        "cache_scope": "server",
    }


def lookup_rank(db: Session, name: str, refresh: bool = False) -> dict:
    key = journal_key(name)
    if not refresh:
        saved = cached_rank(db, name)
        if saved:
            # Preserve the name a client requested, including its spelling.
            saved["journal_name"] = " ".join(name.split())
            return saved
    result = easyscholar.lookup(name, refresh=refresh)
    rows = result.get("rows")
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError("Invalid rank data from easyScholar.")
    for rank in rows:
        if not isinstance(rank, dict) or any(not isinstance(rank.get(field), str)
                                             for field in ("key", "name", "rank", "source")):
            raise ValueError("Invalid rank data from easyScholar.")
    timestamp = float(result["queried_at"])
    values = {
        "journal_key": key,
        "journal_name": " ".join(name.split()),
        "rows_json": json.dumps(rows, ensure_ascii=False),
        "queried_at": timestamp,
        "updated_at": utc_now(),
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        statement = pg_insert(JournalRankCache).values(**values)
    elif dialect == "sqlite":
        statement = sqlite_insert(JournalRankCache).values(**values)
    else:
        raise RuntimeError("Unsupported ranking database")
    db.execute(statement.on_conflict_do_update(
        index_elements=["journal_key"],
        set_={k: v for k, v in values.items() if k != "journal_key"},
        where=JournalRankCache.queried_at <= timestamp,
    ))
    db.commit()
    return {
        "journal_name": values["journal_name"],
        "rows": rows,
        "queried_at": timestamp,
        "cached": False,
        "cache_scope": "server",
    }


def _names_for_list(db: Session, user_id: int, filter_key: str) -> tuple[str, list[str]]:
    key = normalize_filter_key(filter_key)
    if filter_key.strip().lower() != key:
        raise ValueError("Invalid journal list.")
    if key == "all":
        raise ValueError("Select a journal list instead of All journals.")
    if is_custom_filter_key(key) and key != "custom":
        if get_user_filter_override(db, user_id, key) is None:
            raise ValueError("Journal list not found.")
    names = deduplicate_journal_names(effective_journal_names(key, db, user_id) or [])
    if not names:
        raise ValueError("This journal list is empty.")
    if len(names) > _MAX_BATCH_JOURNALS:
        raise ValueError("Too many journals in this list.")
    return key, names


def job_status(job: JournalRankBatchJob) -> dict:
    return {
        "id": job.id,
        "filter": job.filter_key,
        "status": job.status,
        "total": job.total,
        "processed": job.processed,
        "cached": job.cached,
        "fetched": job.fetched,
        "failed": job.failed,
        "refresh": job.force_refresh,
        "last_error": job.last_error,
    }


def _snapshot_hash(names: list[str]) -> str:
    """Different users' overrides can share a filter label, but not contents."""
    values = sorted(journal_key(name) for name in names)
    return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()


def latest_batch(db: Session, user_id: int, filter_key: str) -> dict | None:
    key, names = _names_for_list(db, user_id, filter_key)
    job = db.scalar(select(JournalRankBatchJob)
                    .where(JournalRankBatchJob.snapshot_hash == _snapshot_hash(names))
                    .order_by(JournalRankBatchJob.created_at.desc(), JournalRankBatchJob.id.desc())
                    .limit(1))
    if job is None:
        return None
    result = job_status(job)
    result["filter"] = key
    return result


def enqueue_batch(db: Session, user_id: int, filter_key: str, refresh: bool = False) -> dict:
    key, names = _names_for_list(db, user_id, filter_key)
    digest = _snapshot_hash(names)
    running = db.scalar(
        select(JournalRankBatchJob)
        .where(JournalRankBatchJob.snapshot_hash == digest,
               JournalRankBatchJob.status.in_(("queued", "running")))
        .order_by(JournalRankBatchJob.created_at)
        .limit(1)
    )
    if running:
        result = job_status(running)
        result["filter"] = key
        wake_worker()
        return result
    unfinished = db.scalar(select(func.count(JournalRankBatchJob.id))
                           .where(JournalRankBatchJob.status.in_(("queued", "running")))) or 0
    if unfinished >= _MAX_OPEN_JOBS:
        raise ValueError("Too many ranking jobs are already running. Try again later.")
    job = JournalRankBatchJob(
        id=uuid4().hex, filter_key=key, snapshot_hash=digest, created_by_id=user_id,
        names_json=json.dumps(names, ensure_ascii=False),
        force_refresh=bool(refresh), status="queued",
        total=len(names), processed=0, cached=0, fetched=0, failed=0,
        created_at=utc_now(), updated_at=utc_now(),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # Simultaneous clicks on different devices must share a single job.
        # The partial unique index guarantees no two active tasks per filter.
        db.rollback()
        existing = db.scalar(
            select(JournalRankBatchJob)
            .where(JournalRankBatchJob.snapshot_hash == digest,
                   JournalRankBatchJob.status.in_(("queued", "running")))
            .limit(1)
        )
        if existing is None:
            raise
        result = job_status(existing)
        result["filter"] = key
        wake_worker()
        return result
    result = job_status(job)
    wake_worker()
    return result


def _claim_job() -> str | None:
    with SessionLocal() as db:
        query = (select(JournalRankBatchJob)
                 .where(JournalRankBatchJob.status == "queued")
                 .order_by(JournalRankBatchJob.created_at)
                 .limit(1))
        if db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        job = db.scalar(query)
        if not job:
            return None
        job.status = "running"
        job.updated_at = utc_now()
        db.commit()
        return job.id


def process_batch(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(JournalRankBatchJob, job_id)
        if job is None:
            return
        names = json.loads(job.names_json)
        start, refresh = job.processed, job.force_refresh
    for name in names[start:]:
        outcome = "cached"
        error = None
        try:
            with SessionLocal() as db:
                if not refresh and cached_rank(db, name) is not None:
                    pass
                else:
                    lookup_rank(db, name, refresh=refresh)
                    outcome = "fetched"
        except (ValueError, OSError, RuntimeError) as exc:
            outcome = "failed"
            error = str(exc)[:300]
        except Exception:
            outcome = "failed"
            error = "Unexpected ranking lookup failure."
            logger.exception("Journal ranking lookup failed")
        with SessionLocal() as db:
            job = db.get(JournalRankBatchJob, job_id)
            if job is None:
                return
            job.processed += 1
            if outcome == "fetched":
                job.fetched += 1
            elif outcome == "cached":
                job.cached += 1
            else:
                job.failed += 1
                job.last_error = error
            job.updated_at = utc_now()
            db.commit()
        if outcome == "fetched":
            # Be polite to the provider and keep the main request thread free.
            time.sleep(0.3)
    with SessionLocal() as db:
        job = db.get(JournalRankBatchJob, job_id)
        if job:
            job.status = "partial" if job.failed else "completed"
            job.updated_at = utc_now()
            db.commit()


def _worker_loop() -> None:
    while True:
        job_id = None
        try:
            job_id = _claim_job()
            if job_id:
                process_batch(job_id)
                continue
        except Exception:
            logger.exception("Ranking batch worker failed; queued jobs can be resumed")
            if job_id:
                with SessionLocal() as db:
                    job = db.get(JournalRankBatchJob, job_id)
                    if job is not None and job.status == "running":
                        job.status = "queued"
                        db.commit()
            time.sleep(2)
        _WAKE.wait(timeout=5)
        _WAKE.clear()


def wake_worker() -> None:
    global _WORKER
    with _START_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker_loop, name="netvault-ranking-jobs", daemon=True)
            _WORKER.start()
    _WAKE.set()


def recover_ranking_batches() -> None:
    """Resume interrupted work after restart using each job's last committed index."""
    with SessionLocal() as db:
        db.execute(update(JournalRankBatchJob).where(
            JournalRankBatchJob.status == "running"
        ).values(status="queued", updated_at=utc_now()))
        db.commit()
        pending = db.scalar(select(func.count(JournalRankBatchJob.id)).where(
            JournalRankBatchJob.status == "queued"
        )) or 0
    if pending:
        wake_worker()
