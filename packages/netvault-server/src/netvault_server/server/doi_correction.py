import json

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from netvault_server.server.crossref import fetch_doi_metadata
from netvault_server.server.doi import extract_pdf_text, normalize_doi
from netvault_server.server.main_helpers import apply_crossref_metadata, title_match_score
from netvault_server.server.models import Pdf, PdfDoiCorrection, User
from netvault_server.server.stats import invalidate_stats_cache
from netvault_server.server.storage import object_path


def _load_pdf(db: Session, pdf_id: int, *, for_update: bool) -> Pdf:
    query = select(Pdf).where(Pdf.id == pdf_id)
    if for_update:
        query = query.with_for_update()
    pdf = db.scalar(query)
    if pdf is None or pdf.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF not found")
    return pdf


def _prepare_correction(
    db: Session,
    pdf: Pdf,
    requested_doi: str,
    *,
    expected_sha256: str | None = None,
    expected_current_doi: str | None = None,
) -> tuple[str, object, float | None, bool]:
    try:
        normalized = normalize_doi(requested_doi)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if expected_sha256 and expected_sha256.lower() != pdf.sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The PDF changed after the correction was prepared",
        )
    if expected_current_doi and expected_current_doi.strip().casefold() != pdf.doi.strip().casefold():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The DOI changed after the correction was prepared",
        )

    metadata = fetch_doi_metadata(normalized)
    if metadata.status != "ok":
        error_status = (
            status.HTTP_404_NOT_FOUND
            if metadata.status == "not_found"
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        raise HTTPException(
            status_code=error_status,
            detail=f"DOI correction could not verify metadata: {metadata.status}",
        )

    try:
        canonical_doi = normalize_doi(metadata.canonical_doi or normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Metadata provider returned an invalid canonical DOI",
        ) from exc

    if canonical_doi == pdf.doi.strip().casefold():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The PDF already uses this DOI",
        )

    target = db.scalar(select(Pdf).where(Pdf.doi == canonical_doi, Pdf.is_deleted.is_(False)))
    if target is not None and target.id != pdf.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"DOI {canonical_doi} already belongs to PDF #{target.id}",
        )

    pdf_text = extract_pdf_text(object_path(pdf.sha256))
    match_score = title_match_score(metadata.title, pdf_text)
    requires_override = match_score is not None and match_score < 0.88
    return canonical_doi, metadata, match_score, requires_override


def correct_pdf_doi(
    db: Session,
    admin: User,
    pdf_id: int,
    requested_doi: str,
    reason: str,
    *,
    expected_sha256: str | None = None,
    expected_current_doi: str | None = None,
    allow_title_mismatch: bool = False,
    dry_run: bool = False,
) -> dict:
    reason = reason.strip()
    if not reason:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Provide a correction reason")

    pdf = _load_pdf(db, pdf_id, for_update=not dry_run)
    canonical_doi, metadata, match_score, requires_override = _prepare_correction(
        db,
        pdf,
        requested_doi,
        expected_sha256=expected_sha256,
        expected_current_doi=expected_current_doi,
    )

    result = {
        "pdf_id": pdf.id,
        "previous_doi": pdf.doi,
        "requested_doi": normalize_doi(requested_doi),
        "new_doi": canonical_doi,
        "sha256": pdf.sha256,
        "title": metadata.title,
        "authors": metadata.authors,
        "container_title": metadata.container_title,
        "published_year": metadata.published_year,
        "crossref_status": metadata.status,
        "metadata_provider": metadata.provider,
        "title_match_score": match_score,
        "title_match_status": (
            "unavailable" if match_score is None else "match" if not requires_override else "mismatch"
        ),
        "requires_title_override": requires_override,
        "correction_id": None,
        "dry_run": dry_run,
    }

    if dry_run:
        return result

    if requires_override and not allow_title_mismatch:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Metadata title does not match the PDF; explicit override is required",
        )

    previous_state = json.dumps(
        {
            "doi": pdf.doi,
            "doi_source": pdf.doi_source,
            "doi_evidence": pdf.doi_evidence,
            "title": pdf.title,
            "authors": pdf.authors,
            "container_title": pdf.container_title,
            "publisher": pdf.publisher,
            "published_year": pdf.published_year,
            "crossref_status": pdf.crossref_status,
            "crossref_url": pdf.crossref_url,
        },
        ensure_ascii=False,
    )
    correction = PdfDoiCorrection(
        pdf_id=pdf.id,
        old_doi=pdf.doi,
        new_doi=canonical_doi,
        reason=reason,
        previous_state=previous_state,
        corrected_by_id=admin.id,
    )
    db.add(correction)
    db.flush()

    previous_doi = pdf.doi
    pdf.doi = canonical_doi
    pdf.doi_source = "admin-corrected"
    pdf.doi_evidence = json.dumps(
        {
            "source": "admin-corrected",
            "correction_id": correction.id,
            "corrected_from": previous_doi,
            "requested_doi": normalize_doi(requested_doi),
            "canonical_doi": canonical_doi,
            "reason": reason,
            "metadata_provider": metadata.provider,
            "metadata_title": metadata.title,
            "title_match_score": match_score,
            "title_match_overridden": bool(requires_override and allow_title_mismatch),
        },
        ensure_ascii=False,
    )
    apply_crossref_metadata(pdf, metadata, overwrite=True)
    db.commit()
    invalidate_stats_cache()
    db.refresh(pdf)

    result.update(
        {
            "new_doi": pdf.doi,
            "title": pdf.title,
            "authors": pdf.authors,
            "container_title": pdf.container_title,
            "published_year": pdf.published_year,
            "correction_id": correction.id,
            "dry_run": False,
        }
    )
    return result
