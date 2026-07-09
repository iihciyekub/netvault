from html import unescape
from time import monotonic
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from netvault_server.server.database import get_db
from netvault_server.server.deps import get_current_user
from netvault_server.server.models import DownloadRecord, Pdf, UploadRecord, User

router = APIRouter(prefix="/stats", tags=["stats"])
STATS_CACHE_TTL_SECONDS = 30.0
_dashboard_cache: dict[str, Any] | None = None
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
    _dashboard_cache = None
    _dashboard_cache_expires_at = 0.0


def get_dashboard_stats(db: Session) -> dict[str, Any]:
    global _dashboard_cache, _dashboard_cache_expires_at
    now = monotonic()
    if _dashboard_cache is not None and now < _dashboard_cache_expires_at:
        return _dashboard_cache
    _dashboard_cache = {
        "summary": get_summary(db),
        "journal_year": get_by_journal_year(db),
    }
    _dashboard_cache_expires_at = now + STATS_CACHE_TTL_SECONDS
    return _dashboard_cache


def get_summary(db: Session) -> dict[str, Any]:
    row = db.execute(
        select(
            func.count(Pdf.id),
            func.coalesce(func.sum(Pdf.size), 0),
            func.count(distinct(Pdf.uploaded_by_id)),
            func.min(Pdf.published_year),
            func.max(Pdf.published_year),
        ).where(*active_pdfs())
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


def get_by_journal_year(db: Session, limit: int = 20) -> dict[str, Any]:
    journal = func.trim(Pdf.container_title)
    top_rows = db.execute(
        select(journal.label("journal"), func.count(Pdf.id).label("count"))
        .where(*active_pdfs(), *known_journal())
        .group_by(journal)
        .order_by(func.count(Pdf.id).desc(), journal.asc())
        .limit(limit)
    ).all()
    top_journals = [row.journal for row in top_rows]
    if not top_journals:
        return {"years": [], "max_count": 0, "rows": []}
    rows = db.execute(
        select(journal.label("journal"), Pdf.published_year, func.count(Pdf.id).label("count"))
        .where(*active_pdfs(), *known_journal(), journal.in_(top_journals), Pdf.published_year.is_not(None))
        .group_by(journal, Pdf.published_year)
        .order_by(journal.asc(), Pdf.published_year.asc())
    ).all()
    years = sorted({row.published_year for row in rows})
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
    _: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    return get_summary(db)


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
    _: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    return get_by_journal_year(db)
