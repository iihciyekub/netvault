from html import unescape
from time import monotonic
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from netvault_server.server.database import get_db
from netvault_server.server.deps import get_current_user
from netvault_server.server.journal_filters import (
    allowed_journals_for_filter,
    normalize_filter_key,
    normalize_journal_name,
    user_filter_options,
)
from netvault_server.server.models import DownloadRecord, Pdf, UploadRecord, User

router = APIRouter(prefix="/stats", tags=["stats"])
STATS_CACHE_TTL_SECONDS = 30.0
_dashboard_cache: dict[tuple[int | None, str, int], tuple[float, dict[str, Any]]] = {}
_dashboard_cache_expires_at = 0.0


def active_pdfs():
    return Pdf.is_deleted.is_(False), Pdf.doi.is_not(None)


def known_journal():
    journal = func.trim(Pdf.container_title)
    return (
        Pdf.container_title.is_not(None),
        journal != "",
        func.lower(journal).not_in(["unknown", "(unknown)"]),
    )


def clean_label(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = unescape(value).strip()
    return cleaned or None


def invalidate_stats_cache() -> None:
    global _dashboard_cache, _dashboard_cache_expires_at
    _dashboard_cache = {}
    _dashboard_cache_expires_at = 0.0


def get_dashboard_stats(
    db: Session,
    journal_filter: str = "all",
    include_journals: list[str] | None = None,
    user_id: int | None = None,
    journal_limit: int = 20,
) -> dict[str, Any]:
    global _dashboard_cache, _dashboard_cache_expires_at
    filter_key = normalize_filter_key(journal_filter)
    now = monotonic()
    included = [name for name in (include_journals or []) if name.strip()]
    effective_limit = max(1, journal_limit)
    cache_key = (user_id, filter_key, effective_limit)
    cached = _dashboard_cache.get(cache_key) if not included else None
    if cached is not None and now < cached[0]:
        return cached[1]
    filter_options = user_filter_options(db, user_id) if user_id is not None else ([], [], [])
    stats = {
        "summary": get_summary(db, filter_key, user_id=user_id),
        "journal_year": get_by_journal_year(
            db,
            filter_key,
            limit=effective_limit,
            include_journals=included,
            user_id=user_id,
        ),
        "journal_options": get_all_journal_names(db, filter_key, user_id=user_id),
        "journal_filter": filter_key,
        "journal_filters": filter_options[0],
        "abs_journal_filters": filter_options[1],
        "custom_journal_filters": filter_options[2],
    }
    if not included:
        _dashboard_cache[cache_key] = (now + STATS_CACHE_TTL_SECONDS, stats)
        _dashboard_cache_expires_at = now + STATS_CACHE_TTL_SECONDS
    return stats


def get_summary(
    db: Session,
    journal_filter: str = "all",
    user_id: int | None = None,
) -> dict[str, Any]:
    filter_key = normalize_filter_key(journal_filter)
    conditions = [*active_pdfs()]
    allowed = allowed_journals_for_filter(filter_key, db, user_id)
    if allowed is not None:
        conditions.extend((*known_journal(), Pdf.journal_key.in_(allowed)))
    row = db.execute(
        select(
            func.count(Pdf.id),
            func.coalesce(func.sum(Pdf.size), 0),
            func.count(distinct(Pdf.uploaded_by_id)),
            func.min(Pdf.published_year),
            func.max(Pdf.published_year),
        ).where(*conditions)
    ).one()
    return {
        "active_pdfs": int(row[0] or 0),
        "total_size": int(row[1] or 0),
        "uploaders": int(row[2] or 0),
        "min_year": row[3],
        "max_year": row[4],
    }


def get_by_year(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Pdf.published_year, func.count(Pdf.id))
        .where(*active_pdfs(), Pdf.published_year.is_not(None))
        .group_by(Pdf.published_year)
        .order_by(Pdf.published_year)
    ).all()
    return [{"year": year, "count": count} for year, count in rows]


def get_by_journal(db: Session, limit: int = 20) -> list[dict[str, Any]]:
    journal = func.trim(Pdf.container_title)
    rows = db.execute(
        select(journal.label("journal"), func.count(Pdf.id).label("count"))
        .where(*active_pdfs(), *known_journal())
        .group_by(journal)
        .order_by(func.count(Pdf.id).desc(), journal.asc())
        .limit(limit)
    ).all()
    return [{"journal": clean_label(row.journal), "count": row.count} for row in rows]


def get_by_journal_year(
    db: Session,
    journal_filter: str = "all",
    limit: int = 20,
    include_journals: list[str] | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    filter_key = normalize_filter_key(journal_filter)
    journal = func.trim(Pdf.container_title)
    conditions = [*active_pdfs(), *known_journal(), Pdf.published_year.is_not(None)]
    allowed = allowed_journals_for_filter(filter_key, db, user_id)
    if allowed is not None:
        conditions.append(Pdf.journal_key.in_(allowed))
    all_rows = db.execute(
        select(journal.label("journal"), Pdf.published_year, func.count(Pdf.id).label("count"))
        .where(*conditions)
        .group_by(journal, Pdf.published_year)
        .order_by(journal.asc(), Pdf.published_year.asc())
    ).all()
    totals: dict[str, int] = {}
    for row in all_rows:
        totals[row.journal] = totals.get(row.journal, 0) + int(row.count)
    top_journals = [
        journal_name
        for journal_name, _ in sorted(totals.items(), key=lambda item: (-item[1], clean_label(item[0]) or item[0]))[:limit]
    ]
    included_keys = {
        normalize_journal_name(name) for name in (include_journals or []) if name.strip()
    }
    for journal_name in totals:
        if normalize_journal_name(journal_name) in included_keys and journal_name not in top_journals:
            top_journals.append(journal_name)
    if not top_journals:
        return {"years": [], "max_count": 0, "rows": []}
    rows = [row for row in all_rows if row.journal in top_journals]
    years = sorted({row.published_year for row in rows}, reverse=True)
    display_names = {raw: clean_label(raw) or raw for raw in top_journals}
    by_journal = {name: {year: 0 for year in years} for name in top_journals}
    for row in rows:
        by_journal[row.journal][row.published_year] = row.count
    max_count = max((row.count for row in rows), default=0)

    def level(count: int) -> int:
        if count <= 0 or max_count <= 0:
            return 0
        if count >= max_count:
            return 4
        ratio = count / max_count
        if ratio >= 0.66:
            return 3
        if ratio >= 0.33:
            return 2
        return 1

    return {
        "years": years,
        "max_count": max_count,
        "rows": [
            {
                "journal": display_names[journal_name],
                "total": sum(counts.values()),
                "cells": [
                    {
                        "year": year,
                        "count": counts[year],
                        "level": level(counts[year]),
                    }
                    for year in years
                ],
            }
            for journal_name, counts in by_journal.items()
        ],
    }


def get_all_journal_names(
    db: Session,
    journal_filter: str = "all",
    user_id: int | None = None,
) -> list[str]:
    filter_key = normalize_filter_key(journal_filter)
    conditions = [*active_pdfs(), *known_journal()]
    allowed = allowed_journals_for_filter(filter_key, db, user_id)
    if allowed is not None:
        conditions.append(Pdf.journal_key.in_(allowed))
    journal = func.trim(Pdf.container_title)
    rows = db.scalars(select(journal).where(*conditions).distinct().order_by(journal.asc())).all()
    return [clean_label(name) or name for name in rows]


def get_recent_uploads(db: Session, limit: int = 10) -> list[dict[str, Any]]:
    rows = db.execute(
        select(UploadRecord, Pdf, User)
        .join(Pdf, UploadRecord.pdf_id == Pdf.id)
        .join(User, UploadRecord.user_id == User.id)
        .where(Pdf.is_deleted.is_(False))
        .order_by(UploadRecord.uploaded_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "doi": pdf.doi,
            "title": pdf.title or pdf.original_name,
            "username": user.username,
            "uploaded_at": upload.uploaded_at,
        }
        for upload, pdf, user in rows
    ]


def get_recent_downloads(db: Session, limit: int = 10) -> list[dict[str, Any]]:
    rows = db.execute(
        select(DownloadRecord, Pdf, User)
        .join(Pdf, DownloadRecord.pdf_id == Pdf.id)
        .join(User, DownloadRecord.user_id == User.id)
        .where(Pdf.is_deleted.is_(False))
        .order_by(DownloadRecord.downloaded_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "doi": pdf.doi,
            "title": pdf.title or pdf.original_name,
            "username": user.username,
            "downloaded_at": download.downloaded_at,
        }
        for download, pdf, user in rows
    ]


@router.get("/summary")
def summary(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    filter: str = "all",
) -> dict[str, Any]:
    return get_summary(db, filter, user_id=user.id)


@router.get("/by-year")
def by_year(
    _: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[dict[str, Any]]:
    return get_by_year(db)


@router.get("/by-journal")
def by_journal(
    _: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[dict[str, Any]]:
    return get_by_journal(db)


@router.get("/by-journal-year")
def by_journal_year(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    filter: str = "all",
) -> dict[str, Any]:
    return get_by_journal_year(db, filter, user_id=user.id)
