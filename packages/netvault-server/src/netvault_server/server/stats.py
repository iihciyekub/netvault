from html import unescape
from uuid import uuid4
from types import SimpleNamespace
from time import monotonic
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import distinct, func, select, text
from sqlalchemy.orm import Session

from netvault_server.server.database import get_db
from netvault_server.server.deps import get_current_user
from netvault_server.server.journal_filters import (
    allowed_journals_for_filter,
    effective_journal_names,
    normalize_filter_key,
    normalize_journal_name,
    user_filter_options,
)
from netvault_server.server.models import DownloadRecord, Pdf, UploadRecord, User

router = APIRouter(prefix="/stats", tags=["stats"])
STATS_CACHE_TTL_SECONDS = 30.0
_dashboard_cache: dict[tuple[int | None, str], tuple[float, dict[str, Any]]] = {}
_dashboard_cache_expires_at = 0.0
_dashboard_revision = uuid4().hex


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
    global _dashboard_cache, _dashboard_cache_expires_at, _dashboard_revision
    _dashboard_cache = {}
    _dashboard_cache_expires_at = 0.0
    _dashboard_revision = uuid4().hex


def get_dashboard_revision() -> str:
    return _dashboard_revision


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
    # The server always includes every journal; display limit is client-side.
    cache_key = (user_id, filter_key)
    cached = _dashboard_cache.get(cache_key) if not included else None
    if cached is not None and now < cached[0]:
        return cached[1]
    # Reuse the full-vault summary and picker names after a user visits All.
    all_cache = _dashboard_cache.get((user_id, "all")) if filter_key != "all" else None
    all_shared = all_cache[1] if all_cache and now < all_cache[0] else None
    filter_options = user_filter_options(db, user_id) if user_id is not None else ([], [], [])
    if filter_key == "all" and not included and db.get_bind().dialect.name == "postgresql":
        # One PostgreSQL scan calculates the global summary, journal counts
        # and name options; SQLite keeps the portable three-query path.
        summary, year_rows, journal_names = _load_all_dashboard_groupings(db)
        journal_year = _format_journal_year(year_rows, 100000, [])
    else:
        summary = get_summary(db, filter_key, user_id=user_id)
        journal_year = get_by_journal_year(
            db, filter_key, limit=100000, include_journals=included, user_id=user_id
        )
        journal_names = (
            all_shared["journal_options"]
            if all_shared is not None
            else get_all_journal_names(db, "all", user_id=user_id)
        )
    vault_summary = (
        summary if filter_key == "all"
        else all_shared["summary"] if all_shared is not None
        else get_summary(db, "all", user_id=user_id)
    )
    stats = {
        "summary": summary,
        "vault_summary": vault_summary,
        "journal_year": journal_year,
        "journal_options": journal_names,
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



def _format_journal_year(
    all_rows: list[Any],
    limit: int,
    include_journals: list[str] | None = None,
    configured_journals: list[str] | None = None,
) -> dict[str, Any]:
    def canonical_journal(name: str) -> str:
        from netvault_server.server.journal_identity import journal_display_name
        return journal_display_name(name) or name

    totals: dict[str, int] = {}
    for row in all_rows:
        name = canonical_journal(row.journal)
        totals[name] = totals.get(name, 0) + int(row.count)
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
    # A saved journal list is a catalog selection, not just a query result.
    # Preserve all configured names, including journals without a PDF/year yet.
    # Compare normalized keys to avoid duplicate rows for aliases/case variants.
    represented_keys = {normalize_journal_name(name) for name in top_journals}
    for name in configured_journals or []:
        label = clean_label(name)
        key = normalize_journal_name(label or "")
        if label and key and key not in represented_keys:
            top_journals.append(label)
            represented_keys.add(key)
    if not top_journals:
        return {"years": [], "max_count": 0, "rows": []}
    rows = [row for row in all_rows if canonical_journal(row.journal) in top_journals]
    years = sorted({row.published_year for row in rows}, reverse=True)
    display_names = {raw: clean_label(raw) or raw for raw in top_journals}
    by_journal = {name: {year: 0 for year in years} for name in top_journals}
    for row in rows:
        by_journal[canonical_journal(row.journal)][row.published_year] += row.count
    max_count = max((count for counts in by_journal.values() for count in counts.values()), default=0)

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



def _load_all_dashboard_groupings(db: Session) -> tuple[dict[str, Any], list[Any], list[str]]:
    """Combine full-vault summary, journal/year data and names in one PG scan."""
    records = db.execute(text("""
        SELECT
            GROUPING(BTRIM(container_title), published_year) AS group_id,
            BTRIM(container_title) AS journal,
            published_year,
            COUNT(*) AS pdf_count,
            SUM(size) AS total_size,
            COUNT(DISTINCT uploaded_by_id) AS uploader_count,
            MIN(published_year) AS earliest_year,
            MAX(published_year) AS latest_year
        FROM pdfs
        WHERE is_deleted IS FALSE AND doi IS NOT NULL
        GROUP BY GROUPING SETS ((), (BTRIM(container_title), published_year))
        ORDER BY journal ASC NULLS LAST, published_year ASC NULLS LAST
    """)).mappings().all()
    names: dict[str, None] = {}
    journal_year: list[Any] = []
    summary: dict[str, Any] | None = None
    for record in records:
        if record["group_id"] == 3:
            summary = {
                "active_pdfs": int(record["pdf_count"] or 0),
                "total_size": int(record["total_size"] or 0),
                "uploaders": int(record["uploader_count"] or 0),
                "min_year": record["earliest_year"],
                "max_year": record["latest_year"],
            }
            continue
        name = record["journal"]
        if not name or name.casefold() in {"unknown", "(unknown)"}:
            continue
        names[name] = None
        if record["published_year"] is not None:
            journal_year.append(SimpleNamespace(
                journal=name,
                published_year=record["published_year"],
                count=record["pdf_count"],
            ))
    if summary is None:
        raise RuntimeError("PostgreSQL dashboard grouping did not return a summary")
    return summary, journal_year, [clean_label(name) or name for name in names]


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
    configured = effective_journal_names(filter_key, db, user_id) if filter_key != "all" else None
    return _format_journal_year(all_rows, limit, include_journals, configured)


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
