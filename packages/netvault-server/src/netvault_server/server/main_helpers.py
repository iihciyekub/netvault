import json

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from netvault_server.server.crossref import CrossrefMetadata, fetch_crossref_metadata
from netvault_server.server.doi import extract_doi_evidence
from netvault_server.server.models import Pdf, UploadRecord, User
from netvault_server.server.schemas import PdfRead, UploadResponse
from netvault_server.server.storage import object_path, store_pdf


def pdf_to_read(pdf: Pdf) -> PdfRead:
    return PdfRead(
        id=pdf.id,
        doi=pdf.doi,
        doi_source=pdf.doi_source,
        sha256=pdf.sha256,
        original_name=pdf.original_name,
        title=pdf.title,
        authors=pdf.authors,
        container_title=pdf.container_title,
        publisher=pdf.publisher,
        published_year=pdf.published_year,
        crossref_status=pdf.crossref_status or "pending",
        crossref_url=pdf.crossref_url,
        size=pdf.size,
        uploaded_at=pdf.uploaded_at,
        uploaded_by=pdf.uploaded_by.username,
    )


def apply_crossref_metadata(pdf: Pdf, metadata: CrossrefMetadata) -> None:
    pdf.crossref_status = metadata.status
    if metadata.status != "ok":
        return
    pdf.title = metadata.title or pdf.title
    pdf.authors = metadata.authors or pdf.authors
    pdf.container_title = metadata.container_title or pdf.container_title
    pdf.publisher = metadata.publisher or pdf.publisher
    pdf.published_year = metadata.published_year or pdf.published_year
    pdf.crossref_url = metadata.resource_url or pdf.crossref_url
    pdf.crossref_fetched_at = metadata.fetched_at or pdf.crossref_fetched_at


def doi_evidence_json(evidence) -> str:
    return json.dumps(
        {
            "source": evidence.source,
            "candidates": [
                {"doi": candidate.doi, "source": candidate.source, "detail": candidate.detail}
                for candidate in evidence.candidates
            ],
        },
        ensure_ascii=False,
    )


async def process_upload(
    file: UploadFile,
    doi: str | None,
    no_crossref: bool,
    user: User,
    db: Session,
) -> UploadResponse:
    sha256, size, relative_path, object_deduplicated = await store_pdf(file)
    evidence = extract_doi_evidence(object_path(sha256), explicit_doi=doi)
    if evidence.status == "conflict":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=evidence.reason or "DOI conflict")
    if evidence.status != "ok" or not evidence.doi:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=evidence.reason or "No DOI found in PDF. Pass --doi DOI when uploading.",
        )
    normalized_doi = evidence.doi

    pdf_by_doi = db.scalar(select(Pdf).where(Pdf.doi == normalized_doi))
    pdf_by_sha = db.scalar(select(Pdf).where(Pdf.sha256 == sha256))
    if pdf_by_doi is not None and pdf_by_doi.sha256 != sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"DOI {normalized_doi} is already linked to a different PDF",
        )
    if pdf_by_sha is not None and pdf_by_sha.doi and pdf_by_sha.doi != normalized_doi:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This PDF is already linked to DOI {pdf_by_sha.doi}",
        )

    pdf = pdf_by_doi or pdf_by_sha
    created_pdf = False

    if pdf is None:
        pdf = Pdf(
            doi=normalized_doi,
            doi_source=evidence.source,
            doi_evidence=doi_evidence_json(evidence),
            sha256=sha256,
            original_name=file.filename or f"{sha256}.pdf",
            size=size,
            storage_path=relative_path,
            uploaded_by_id=user.id,
        )
        db.add(pdf)
        db.flush()
        created_pdf = True
    elif not pdf.doi:
        pdf.doi = normalized_doi
        pdf.doi_source = evidence.source
        pdf.doi_evidence = doi_evidence_json(evidence)
    if pdf.is_deleted:
        pdf.is_deleted = False
        pdf.deleted_at = None
        pdf.deleted_by_id = None

    if evidence.source and not pdf.doi_source:
        pdf.doi_source = evidence.source
        pdf.doi_evidence = doi_evidence_json(evidence)
    if not no_crossref and (created_pdf or not pdf.title or pdf.crossref_status in (None, "pending", "unavailable")):
        apply_crossref_metadata(pdf, fetch_crossref_metadata(normalized_doi))
    elif no_crossref and not pdf.crossref_status:
        pdf.crossref_status = "skipped"

    db.add(
        UploadRecord(
            pdf_id=pdf.id,
            user_id=user.id,
            original_name=file.filename or pdf.original_name,
            size=size,
        )
    )
    db.commit()
    db.refresh(pdf)
    return UploadResponse(pdf=pdf_to_read(pdf), deduplicated=object_deduplicated or not created_pdf)
