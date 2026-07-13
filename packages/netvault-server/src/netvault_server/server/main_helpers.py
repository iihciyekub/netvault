import json
from dataclasses import replace

from fastapi import HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from netvault_server.server.crossref import CrossrefMetadata, fetch_crossref_metadata
from netvault_server.server.doi import extract_doi_evidence, normalize_doi
from netvault_server.server.models import Pdf, PdfFileAlias, UploadRecord, User, utc_now
from netvault_server.server.schemas import PdfRead, UploadResponse
from netvault_server.server.storage import (
    acquire_object_lock,
    object_path,
    promote_staged_pdf,
    release_object_lock,
    store_pdf,
)


CLIENT_DOI_SOURCES = {
    "download-index",
    "explicit",
    "filename",
    "pdf-content",
    "pdf-metadata",
    "user",
}


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


def apply_crossref_metadata(pdf: Pdf, metadata: CrossrefMetadata, *, overwrite: bool = False) -> None:
    pdf.crossref_status = metadata.status
    pdf.crossref_fetched_at = metadata.fetched_at or pdf.crossref_fetched_at
    if metadata.status != "ok":
        return
    if overwrite:
        pdf.title = metadata.title
        pdf.authors = metadata.authors
        pdf.container_title = metadata.container_title
        pdf.publisher = metadata.publisher
        pdf.published_year = metadata.published_year
        pdf.crossref_url = metadata.resource_url
    else:
        pdf.title = metadata.title or pdf.title
        pdf.authors = metadata.authors or pdf.authors
        pdf.container_title = metadata.container_title or pdf.container_title
        pdf.publisher = metadata.publisher or pdf.publisher
        pdf.published_year = metadata.published_year or pdf.published_year
        pdf.crossref_url = metadata.resource_url or pdf.crossref_url


def doi_evidence_json(evidence) -> str:
    return json.dumps(
        {
            "source": evidence.source,
            "candidates": [
                {
                    "doi": candidate.doi,
                    "source": candidate.source,
                    "detail": candidate.detail,
                    "score": candidate.score,
                    "context": candidate.context,
                }
                for candidate in evidence.candidates
            ],
        },
        ensure_ascii=False,
    )


def add_upload_record(
    pdf: Pdf,
    file: UploadFile,
    size: int,
    user: User,
    db: Session,
    idempotency_key: str | None = None,
) -> None:
    scoped_key = f"{user.id}:{idempotency_key}" if idempotency_key else None
    if scoped_key and db.scalar(
        select(UploadRecord.id).where(UploadRecord.idempotency_key == scoped_key)
    ):
        return
    db.add(
        UploadRecord(
            pdf_id=pdf.id,
            user_id=user.id,
            original_name=file.filename or pdf.original_name,
            size=size,
            idempotency_key=scoped_key,
        )
    )


async def process_upload(
    file: UploadFile,
    doi: str | None,
    no_crossref: bool,
    user: User,
    db: Session,
    idempotency_key: str | None = None,
    force: bool = False,
    doi_source: str | None = None,
) -> UploadResponse:
    if force and no_crossref:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Force upload requires Crossref metadata",
        )
    if doi_source is not None:
        if not doi or not doi.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="DOI source requires an explicit DOI",
            )
        if doi_source not in CLIENT_DOI_SOURCES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported DOI source",
            )
    sha256, size, relative_path, object_deduplicated, staged_path = await store_pdf(file)
    promoted_new_object = False
    object_lock = await run_in_threadpool(acquire_object_lock, sha256)
    try:
        pdf_by_sha = db.scalar(select(Pdf).where(Pdf.sha256 == sha256))
        if pdf_by_sha is not None and not force:
            if staged_path is not None:
                promote_staged_pdf(staged_path, sha256)
                staged_path = None
            if doi:
                try:
                    normalized_explicit_doi = normalize_doi(doi)
                except ValueError as exc:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
                if pdf_by_sha.doi and pdf_by_sha.doi != normalized_explicit_doi:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"This PDF is already linked to DOI {pdf_by_sha.doi}",
                    )
            if pdf_by_sha.is_deleted:
                pdf_by_sha.is_deleted = False
                pdf_by_sha.deleted_at = None
                pdf_by_sha.deleted_by_id = None
            if not no_crossref and (
                not pdf_by_sha.title or pdf_by_sha.crossref_status in (None, "pending", "unavailable", "skipped")
            ):
                metadata = await run_in_threadpool(fetch_crossref_metadata, pdf_by_sha.doi)
                apply_crossref_metadata(pdf_by_sha, metadata)
            add_upload_record(pdf_by_sha, file, size, user, db, idempotency_key)
            db.commit()
            from netvault_server.server.stats import invalidate_stats_cache

            invalidate_stats_cache()
            db.refresh(pdf_by_sha)
            return UploadResponse(pdf=pdf_to_read(pdf_by_sha), deduplicated=True)

        evidence_path = staged_path or object_path(sha256)
        evidence = extract_doi_evidence(evidence_path, explicit_doi=doi, filename=file.filename)
        if doi_source is not None and evidence.status == "ok":
            evidence = replace(evidence, source=doi_source)
        if evidence.status == "conflict":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=evidence.reason or "DOI conflict")
        if evidence.status != "ok" or not evidence.doi:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=evidence.reason or "No DOI found in PDF. Pass --doi DOI when uploading.",
            )
        normalized_doi = evidence.doi

        pdf_by_doi = db.scalar(
            select(Pdf).where(Pdf.doi == normalized_doi).with_for_update()
        )
        alias_pdf_id = db.scalar(
            select(PdfFileAlias.pdf_id).where(PdfFileAlias.sha256 == sha256)
        )
        if alias_pdf_id is not None and (pdf_by_doi is None or alias_pdf_id != pdf_by_doi.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This PDF hash is already linked to another DOI",
            )
        if force and pdf_by_sha is not None and pdf_by_sha.doi != normalized_doi:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"This PDF is already linked to DOI {pdf_by_sha.doi}",
            )
        if force and pdf_by_doi is not None:
            metadata = await run_in_threadpool(fetch_crossref_metadata, normalized_doi)
            if metadata.status != "ok":
                error_status = (
                    status.HTTP_404_NOT_FOUND
                    if metadata.status == "not_found"
                    else status.HTTP_503_SERVICE_UNAVAILABLE
                )
                raise HTTPException(
                    status_code=error_status,
                    detail=f"Crossref metadata refresh failed: {metadata.status}",
                )

            old_sha256 = pdf_by_doi.sha256
            storage_deduplicated = promote_staged_pdf(staged_path, sha256)
            promoted_new_object = staged_path is not None and not storage_deduplicated
            staged_path = None
            db.execute(delete(PdfFileAlias).where(PdfFileAlias.pdf_id == pdf_by_doi.id))
            pdf_by_doi.doi = normalized_doi
            pdf_by_doi.doi_source = evidence.source or "explicit"
            pdf_by_doi.doi_evidence = doi_evidence_json(evidence)
            pdf_by_doi.sha256 = sha256
            pdf_by_doi.original_name = file.filename or f"{sha256}.pdf"
            pdf_by_doi.size = size
            pdf_by_doi.storage_path = relative_path
            pdf_by_doi.uploaded_by_id = user.id
            pdf_by_doi.uploaded_at = utc_now()
            pdf_by_doi.is_deleted = False
            pdf_by_doi.deleted_at = None
            pdf_by_doi.deleted_by_id = None
            apply_crossref_metadata(pdf_by_doi, metadata, overwrite=True)
            add_upload_record(pdf_by_doi, file, size, user, db, idempotency_key)
            db.commit()
            from netvault_server.server.stats import invalidate_stats_cache

            invalidate_stats_cache()
            db.refresh(pdf_by_doi)
            db.expire(pdf_by_doi, ["uploaded_by"])
            if old_sha256 != sha256:
                try:
                    object_path(old_sha256).unlink(missing_ok=True)
                except OSError:
                    pass
            return UploadResponse(
                pdf=pdf_to_read(pdf_by_doi),
                deduplicated=object_deduplicated or storage_deduplicated,
                replaced=True,
            )

        if pdf_by_doi is not None and pdf_by_doi.sha256 != sha256:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"DOI {normalized_doi} is already linked to a different PDF",
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
            metadata = await run_in_threadpool(fetch_crossref_metadata, normalized_doi)
            apply_crossref_metadata(pdf, metadata)
        elif no_crossref:
            pdf.crossref_status = "skipped"

        storage_deduplicated = promote_staged_pdf(staged_path, sha256)
        promoted_new_object = staged_path is not None and not storage_deduplicated
        staged_path = None
        add_upload_record(pdf, file, size, user, db, idempotency_key)
        db.commit()
        from netvault_server.server.stats import invalidate_stats_cache

        invalidate_stats_cache()
        db.refresh(pdf)
        return UploadResponse(
            pdf=pdf_to_read(pdf),
            deduplicated=object_deduplicated or storage_deduplicated or not created_pdf,
        )
    except Exception:
        db.rollback()
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
        if promoted_new_object:
            object_path(sha256).unlink(missing_ok=True)
        raise
    finally:
        release_object_lock(object_lock)
