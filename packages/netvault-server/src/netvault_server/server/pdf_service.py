"""Shared PDF lookup, search and delivery rules for API and web routes."""

from pathlib import Path
import logging
import re

from fastapi import HTTPException, status
from fastapi.responses import FileResponse
from pypdf import PdfReader
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from netvault_server import __version__
from netvault_server.server.config import get_settings
from netvault_server.server.doi import document_info_candidates, normalize_doi
from netvault_server.server.download_audit import record_completed_downloads
from netvault_server.server.main_helpers import pdf_to_read
from netvault_server.server.journal_filters import normalize_journal_name
from netvault_server.server.journal_identity import journal_display_name
from netvault_server.server.models import Pdf, PdfDoiCorrection, PdfFileAlias, User, UserRole
from netvault_server.server.queries import pdf_contains_query, pdf_read_options
from netvault_server.server.schemas import PdfDoiCorrectionRead, Sha256ExistsResponse
from netvault_server.server.security import activity_is_allowed
from netvault_server.server.storage import object_path

logger = logging.getLogger(__name__)


def read_pdf_document_info(path: Path) -> dict:
    """Inspect the stored PDF's Document Info without changing the file or reading XMP.

    Keep embedded PDF metadata separate from the database's verified DOI record.
    A damaged or encrypted file must not hide the still-useful database record.
    """
    empty = {"status": "unavailable", "doi": None, "dois": [], "fields": {}, "page_count": None}
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            return {**empty, "status": "encrypted"}
        fields = {
            str(key)[:120]: str(value)[:4096]
            for key, value in list((reader.metadata or {}).items())[:64]
            if value is not None
        }
        dois = sorted({item.doi for item in document_info_candidates(reader)})
        return {
            "status": "conflict" if len(dois) > 1 else "ok" if dois else "no-doi",
            "doi": dois[0] if len(dois) == 1 else None,
            "dois": dois,
            "fields": fields,
            "page_count": len(reader.pages),
        }
    except Exception as exc:
        logger.warning("Could not inspect stored PDF Document Info: %s", type(exc).__name__)
        return empty


def existing_pdfs(db: Session, hashes=(), dois=()) -> Sha256ExistsResponse:
    hashes = sorted(
        {
            value.lower()
            for value in hashes
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value)
        }
    )
    normalized_dois = set()
    for value in dois:
        try:
            normalized_dois.add(normalize_doi(value))
        except (TypeError, AttributeError, ValueError):
            continue
    active = Pdf.is_deleted.is_(False)
    query = select(Pdf).options(*pdf_read_options()).where(active)
    pdfs = db.scalars(query.where(Pdf.sha256.in_(hashes))).all() if hashes else []
    aliases = (
        db.execute(
            select(PdfFileAlias.sha256, Pdf)
            .join(Pdf, Pdf.id == PdfFileAlias.pdf_id)
            .options(*pdf_read_options())
            .where(active, PdfFileAlias.sha256.in_(hashes))
        ).all()
        if hashes
        else []
    )
    by_doi = db.scalars(query.where(Pdf.doi.in_(normalized_dois))).all() if normalized_dois else []
    existing = {pdf.sha256: pdf_to_read(pdf) for pdf in pdfs}
    existing.update({sha: pdf_to_read(pdf) for sha, pdf in aliases})
    return Sha256ExistsResponse(
        existing=existing,
        existing_doi={pdf.doi: pdf_to_read(pdf) for pdf in by_doi},
        server_version=__version__,
    )


def find_pdf(identifier: str, db: Session) -> Pdf:
    value = identifier.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        sha = value.lower()
        pdf = db.scalar(select(Pdf).where(Pdf.sha256 == sha))
        if pdf is None:
            pdf = db.scalar(select(Pdf).join(PdfFileAlias).where(PdfFileAlias.sha256 == sha))
    elif value.isascii() and value.isdigit() and int(value) <= 2**31 - 1:
        pdf = db.get(Pdf, int(value))
    else:
        pdf = None
    if pdf is None or pdf.is_deleted:
        raise HTTPException(status_code=404, detail="PDF not found")
    return pdf


def find_pdf_by_doi(doi: str, db: Session) -> Pdf:
    try:
        normalized = normalize_doi(doi)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pdf = db.scalar(select(Pdf).where(Pdf.doi == normalized, Pdf.is_deleted.is_(False)))
    if pdf is None:
        raise HTTPException(status_code=404, detail="PDF not found for DOI")
    return pdf


def search_pdfs_query(db: Session, value: str, *, journal: str = "", year: int | None = None):
    value = value.strip()
    if len(value) > get_settings().search_query_max_chars:
        raise HTTPException(status_code=400, detail="Search query is too long")
    query = (
        select(Pdf)
        .options(*pdf_read_options())
        .where(Pdf.is_deleted.is_(False), Pdf.doi.is_not(None))
        .order_by(Pdf.uploaded_at.desc(), Pdf.id.desc())
    )
    if journal:
        query = query.where(
            Pdf.journal_key == normalize_journal_name(journal),
            Pdf.container_title == journal_display_name(journal),
        )
    if year is not None:
        query = query.where(Pdf.published_year == year)
    if not value:
        return query
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        try:
            found = find_pdf(value, db)
            condition = Pdf.id == found.id
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            condition = pdf_contains_query(value)
    elif re.fullmatch(r"[0-9a-fA-F]{8,63}", value):
        sha = value.lower()
        condition = or_(
            Pdf.sha256.startswith(sha),
            Pdf.id.in_(select(PdfFileAlias.pdf_id).where(PdfFileAlias.sha256.startswith(sha))),
            pdf_contains_query(value),
        )
    else:
        try:
            doi = normalize_doi(value)
        except ValueError:
            doi = None
        exact = (
            db.scalar(select(Pdf.id).where(Pdf.is_deleted.is_(False), Pdf.doi == doi).limit(1))
            if doi
            else None
        )
        condition = Pdf.doi == doi if exact else pdf_contains_query(value)
    return query.where(condition)


def search_pdfs_by_dois_query(db: Session, dois: tuple[str, ...]):
    """Find only exact DOI matches, using the same PDF loading as standard search."""
    return (
        select(Pdf)
        .options(*pdf_read_options())
        .where(Pdf.is_deleted.is_(False), Pdf.doi.in_(dois))
        .order_by(Pdf.uploaded_at.desc(), Pdf.id.desc())
    )


def resolve_pdf_file(db: Session, *, doi: str | None = None, pdf_id: int | None = None):
    if pdf_id is not None:
        pdf = find_pdf(str(pdf_id), db)
    elif doi:
        pdf = find_pdf_by_doi(doi, db)
    else:
        raise HTTPException(status_code=400, detail="Missing PDF identifier")
    path = object_path(pdf.sha256)
    if not path.is_file():
        raise HTTPException(status_code=500, detail="Stored PDF missing")
    return pdf, path


def send_pdf_file(pdf: Pdf, user: User, db: Session, *, preview: bool = False) -> FileResponse:
    action = "preview" if preview else "download"
    if (preview or user.role != UserRole.admin) and not activity_is_allowed(
        f"{action}:{user.id}", get_settings().download_rate_per_hour
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"{action.capitalize()} rate limit exceeded",
        )
    path = object_path(pdf.sha256)
    if not path.is_file():
        raise HTTPException(status_code=500, detail="Stored PDF missing")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=Path(pdf.original_name).name,
        content_disposition_type="inline" if preview else "attachment",
        background=None
        if preview
        else BackgroundTask(record_completed_downloads, [pdf.id], user.id),
        headers={"ETag": f'"{pdf.sha256}"'},
    )


def doi_correction_history(db: Session, pdf_id: int) -> list[PdfDoiCorrectionRead]:
    rows = db.execute(
        select(PdfDoiCorrection, User)
        .join(User, User.id == PdfDoiCorrection.corrected_by_id)
        .where(PdfDoiCorrection.pdf_id == pdf_id)
        .order_by(PdfDoiCorrection.corrected_at.desc(), PdfDoiCorrection.id.desc())
    ).all()
    return [
        PdfDoiCorrectionRead(
            id=row.id,
            pdf_id=row.pdf_id,
            old_doi=row.old_doi,
            new_doi=row.new_doi,
            reason=row.reason,
            corrected_by=user.username,
            corrected_at=row.corrected_at,
        )
        for row, user in rows
    ]
