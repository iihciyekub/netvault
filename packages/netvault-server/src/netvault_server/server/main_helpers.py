import hashlib
import json
import re
from dataclasses import replace
from time import monotonic
from difflib import SequenceMatcher

from fastapi import HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from netvault_server.server.crossref import CrossrefMetadata, fetch_doi_metadata, search_crossref_metadata
from netvault_server.server.doi import (
    DoiCandidate,
    DoiEvidence,
    extract_pdf_text,
    extract_pdf_title,
    extract_doi_evidence,
    normalize_doi,
    find_dois_in_text,
    RAW_EXPLICIT_DOI_RE,
)
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
    "download-record",
    "csv-trusted",
    "explicit",
    "filename",
    "pdf-content",
    "pdf-metadata",
    "user",
}
AUTOMATIC_CLIENT_DOI_SOURCES = {"filename", "pdf-content", "pdf-metadata", "download-record"}


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
    locally_pending = pdf.crossref_status == "pending" and bool(pdf.title)
    pdf.crossref_status = metadata.status
    pdf.crossref_fetched_at = metadata.fetched_at or pdf.crossref_fetched_at
    if metadata.status != "ok":
        if metadata.status == "pending" and metadata.provider == "pdf-local":
            pdf.title = metadata.title or pdf.title
        elif locally_pending:
            # A failed refresh must not erase the locally established title or
            # misrepresent a pending identity as externally verified.
            pdf.crossref_status = "pending"
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
    from netvault_server.server.journal_identity import serial_identifiers

    if overwrite or metadata.container_title:
        pdf.journal_raw_title = metadata.container_title
        pdf.journal_issns_json = json.dumps(serial_identifiers(metadata.issns))
        pdf.work_type = metadata.work_type


def doi_evidence_json(evidence, verification: list[dict] | None = None) -> str:
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
                    "embedded_publisher_url": candidate.embedded_publisher_url,
                }
                for candidate in evidence.candidates
            ],
            "verification": verification or [],
        },
        ensure_ascii=False,
    )


def _normalized_title_text(value: str) -> str:
    from netvault_server.server.article_title import normalized_title
    return normalized_title(value)

def title_match_score(title: str | None, pdf_text: str) -> float | None:
    from netvault_server.server.article_title import heading_match_score
    return heading_match_score(title, pdf_text)


def _ordered_automatic_candidates(
    evidence,
    client_doi: str | None,
    client_source: str | None,
) -> list[DoiCandidate]:
    source_order = {"explicit": -1, "download-index": 0, "download-record": 0, "pdf-metadata": 1, "pdf-content": 2, "filename": 3}
    candidates = list(evidence.candidates)
    if client_doi and not any(candidate.doi == client_doi for candidate in candidates):
        candidates.append(
            DoiCandidate(
                client_doi,
                client_source if client_source in AUTOMATIC_CLIENT_DOI_SOURCES else "pdf-content",
                "client-hint",
                80,
            )
        )
    candidates.sort(
        key=lambda candidate: (
            # A DOI from references or a raw low-confidence artifact must
            # not outrank a plausible filename hint.
            4 if candidate.source == "pdf-content" and candidate.score < 50
            else source_order.get(candidate.source, 9),
            candidate.embedded_publisher_url,
            "page-1" not in candidate.detail if candidate.source == "pdf-content" else False,
            -candidate.score,
            candidate.doi != client_doi,
            candidate.doi,
        )
    )
    unique = []
    seen = set()
    for candidate in candidates:
        if candidate.doi in seen:
            continue
        seen.add(candidate.doi)
        unique.append(candidate)
    return unique[:12]


async def verify_automatic_doi(
    evidence,
    path,
    client_doi: str | None = None,
    client_source: str | None = None,
) -> tuple[object, CrossrefMetadata, list[dict]]:
    pdf_text = evidence.pdf_text or await run_in_threadpool(extract_pdf_text, path)
    attempts: list[dict] = []
    crossref_unavailable = False
    verified_registry_candidate = False
    candidates = _ordered_automatic_candidates(evidence, client_doi, client_source)
    layout_text = None

    for candidate in candidates:
        metadata = await run_in_threadpool(fetch_doi_metadata, candidate.doi)
        attempt = {
            "doi": candidate.doi,
            "source": candidate.source,
            "metadata_status": metadata.status,
            "metadata_provider": metadata.provider,
        }
        if metadata.status == "unavailable":
            crossref_unavailable = True
            attempt["accepted"] = False
            attempt["reason"] = "DOI metadata provider unavailable"
            attempts.append(attempt)
            continue
        if metadata.status in {"ok", "not_found"}:
            verified_registry_candidate = True
        if metadata.status != "ok":
            attempt["accepted"] = False
            attempt["reason"] = "DOI not found by metadata providers"
            attempts.append(attempt)
            continue

        if metadata.work_type in {"journal", "journal-volume", "journal-issue", "book-series", "periodical", "dataset", "component"} or candidate.doi.startswith("10.17605/osf.io/"):
            attempt.update(accepted=False, reason="DOI identifies a publication container or research data, not an article")
            attempts.append(attempt)
            continue
        match_score = await run_in_threadpool(title_match_score, metadata.title, pdf_text)

        attempt["title_match_method"] = "heading-text"
        if match_score is not None and match_score < 0.88:
            from netvault_server.server.article_title import layout_title_text
            if layout_text is None:
                layout_text = await run_in_threadpool(layout_title_text, path)
            if layout_text:
                layout_score = await run_in_threadpool(title_match_score, metadata.title, layout_text)
                if layout_score is not None and layout_score > match_score:
                    match_score = layout_score
                    attempt["title_match_method"] = "heading-layout"

        attempt["metadata_title"] = metadata.title
        attempt["title_match_score"] = match_score
        if metadata.provider == "semantic-scholar":
            # An index record is weaker than registry metadata: require an exact
            # first-page title plus independent author and year corroboration.
            first_page = pdf_text.split("\f", 1)[0]
            header = re.split(r"(?im)^\s*(?:abstract|references|bibliography)\b", first_page, maxsplit=1)[0]
            header_words = set(_normalized_title_text(header).split())
            names = [name.strip().split()[-1] for name in (metadata.authors or "").split(";") if name.strip()]
            corroborated = (
                (await run_in_threadpool(title_match_score, metadata.title, header) or 0) >= 0.98
                and any(_normalized_title_text(name) in header_words for name in names)
                and metadata.published_year is not None
                and re.search(rf"\b{metadata.published_year}\b", header) is not None
            )
            attempt["corroborated"] = corroborated
            if not corroborated:
                attempt.update(accepted=False, reason="Indexed DOI requires a matching first-page title, author and year")
                attempts.append(attempt)
                continue
        if (match_score is None and candidate.source != "explicit") or (match_score is not None and match_score < 0.88):
            attempt["accepted"] = False
            attempt["reason"] = "Metadata title does not match the PDF"
            attempts.append(attempt)
            continue

        try:
            canonical_doi = normalize_doi(metadata.canonical_doi or candidate.doi)
        except ValueError:
            attempt["accepted"] = False
            attempt["reason"] = "Metadata provider returned an invalid canonical DOI"
            attempts.append(attempt)
            continue
        attempt["accepted"] = True
        attempt["canonical_doi"] = canonical_doi
        attempts.append(attempt)
        return (
            replace(
                evidence,
                status="ok",
                doi=canonical_doi,
                source=candidate.source,
                reason=None,
            ),
            replace(metadata, canonical_doi=canonical_doi),
            attempts,
        )

    # Missing registry metadata is distinct from conflicting metadata. Admit a
    # locally supported identity as pending, never as an externally verified DOI.
    pending = await run_in_threadpool(pending_pdf_identity, path, pdf_text, candidates, attempts)
    if pending is not None:
        local_doi, local_title, local_page, prominence = pending
        attempts.append({
            "doi": local_doi, "source": "pdf-local", "accepted": True,
            "metadata_status": "pending", "metadata_verified": False,
            "local_title": local_title, "title_page": local_page,
            "heading_prominence": round(prominence, 3),
            "reason": "Unique printed DOI and prominent article heading; external metadata pending",
        })
        return (
            replace(evidence, status="ok", doi=local_doi, source="pdf-content", reason=None),
            CrossrefMetadata(status="pending", canonical_doi=local_doi,
                             title=local_title, provider="pdf-local"),
            attempts,
        )

    # Search only for automatic identities. Require title, journal, year and author
    # corroboration in the PDF, and reject ambiguous matches rather than picking rank 1.
    # Guess a document title only after DOI-led verification fails; a masthead or
    # invitation label must never override a verified metadata title.
    document_title = await run_in_threadpool(extract_pdf_title, path)
    if document_title and not any(c.source == "explicit" for c in candidates) and (
        await run_in_threadpool(title_match_score, document_title, pdf_text) or 0
    ) >= 0.98:
        results = await run_in_threadpool(search_crossref_metadata, document_title)
        accepted = {}
        normalized_pdf = _normalized_title_text(pdf_text).replace(" ", "")
        for metadata in results:
            if metadata.status == "unavailable":
                crossref_unavailable = True
                continue
            exact_title = SequenceMatcher(None, _normalized_title_text(document_title),
                                          _normalized_title_text(metadata.title or "")).ratio()
            journal = _normalized_title_text(metadata.container_title or "").replace(" ", "")
            author_names = [name.strip().split()[-1] for name in (metadata.authors or "").split(";") if name.strip()]
            pdf_words = set(_normalized_title_text(pdf_text).split())
            author_match = any(_normalized_title_text(name) in pdf_words for name in author_names)
            corroborated = (metadata.work_type == "journal-article" and exact_title >= 0.98
                            and len(journal) >= 8 and journal in normalized_pdf
                            and metadata.published_year is not None
                            and re.search(rf"\b{metadata.published_year}\b", pdf_text) is not None
                            and author_match)
            attempt = {"doi": metadata.canonical_doi, "source": "crossref-title",
                       "metadata_title": metadata.title, "title_match_score": exact_title,
                       "accepted": False, "corroborated": corroborated}
            attempts.append(attempt)
            if corroborated:
                try:
                    canonical = normalize_doi(metadata.canonical_doi or "")
                except ValueError:
                    continue
                accepted[canonical] = (metadata, attempt)
        if len(accepted) == 1:
            canonical, (metadata, attempt) = next(iter(accepted.items()))
            attempt["accepted"] = True
            candidate = DoiCandidate(canonical, "pdf-metadata", "crossref-title", 98, document_title)
            return (replace(evidence, status="ok", doi=canonical, source="pdf-metadata",
                            candidates=[*evidence.candidates, candidate], reason=None),
                    replace(metadata, canonical_doi=canonical), attempts)

    if crossref_unavailable and not verified_registry_candidate:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DOI metadata providers were unavailable; retry when connectivity recovers",
        )
    publisher_url = any(candidate.embedded_publisher_url for candidate in candidates)
    detail = "No DOI candidate could be verified against metadata and the PDF title"
    if publisher_url:
        detail += "; a candidate appeared only in a publisher download URL"
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def pending_pdf_identity(path, text, candidates, attempts):
    from netvault_server.server.article_title import local_title_evidence

    layout = local_title_evidence(path, text)
    if layout is None:
        return None
    page = text.split("\f", 2)[layout.page - 1]
    header = re.split(
        r"(?im)^\s*(?:abstract\b|keywords\b|references\b|bibliography\b|to cite this article\b)",
        page, maxsplit=1,
    )[0]
    # A publication masthead, publication history and author contact information
    # independently support this local article identity. References and embedded
    # download links cannot establish it.
    if not re.search(r"\bISSN\s+\d{4}-[\dX]{4}\b", header, re.I):
        return None
    if not re.search(r"\b(?:received|accepted|published|copyright)\b", header, re.I):
        return None
    if not re.search(r"[\w.+-]+@[\w.-]+\.\w+|orcid\.org/\d", header, re.I):
        return None
    printed = set(find_dois_in_text(header))
    if len(printed) != 1:
        return None
    doi = next(iter(printed))
    # The DOI must stand on its own labelled line, not inside a citation or prose.
    labelled = any(
        RAW_EXPLICIT_DOI_RE.fullmatch(line.strip()) and doi in find_dois_in_text(line)
        for line in header.splitlines()
    )
    if not labelled or doi.startswith("10.17605/osf.io/"):
        return None
    if not any(candidate.doi == doi for candidate in candidates):
        return None
    explicit = {c.doi for c in candidates if c.source == "explicit"}
    if explicit and explicit != {doi}:
        return None
    relevant = [attempt for attempt in attempts if attempt["doi"] == doi]
    if not relevant or any(a.get("metadata_status") not in {"not_found", "unavailable"} for a in relevant):
        return None
    return doi, layout.title, layout.page, layout.prominence


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
    timings: dict[str, float] | None = None,
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
    timings = timings if timings is not None else {}
    started = monotonic()
    persist_started = None
    sha256, size, relative_path, object_deduplicated, staged_path = await store_pdf(file)
    timings["store"] = monotonic() - started
    promoted_new_object = False
    # Authentication has opened a read transaction. Do not hold its connection
    # while waiting for an object lock owned by another upload.
    db.rollback()
    object_lock = await run_in_threadpool(acquire_object_lock, sha256)
    doi_lock = None
    try:
        pdf_by_sha = db.scalar(select(Pdf).where(Pdf.sha256 == sha256))
        if pdf_by_sha is None and not force:
            pdf_by_sha = db.scalar(select(Pdf).join(PdfFileAlias).where(PdfFileAlias.sha256 == sha256))
        if pdf_by_sha is not None and not force:
            changed = pdf_by_sha.is_deleted
            if doi and doi_source not in AUTOMATIC_CLIENT_DOI_SOURCES:
                try:
                    normalized_explicit_doi = normalize_doi(doi)
                except ValueError as exc:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
                if pdf_by_sha.doi and pdf_by_sha.doi != normalized_explicit_doi:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"This PDF is already linked to DOI {pdf_by_sha.doi}. "
                            "Force upload replaces a file for the same DOI; an administrator must "
                            "correct a wrong DOI identity."
                        ),
                    )
            if staged_path is not None:
                if pdf_by_sha.sha256 == sha256:
                    promote_staged_pdf(staged_path, sha256)
                else:
                    if not object_path(pdf_by_sha.sha256).is_file():
                        raise HTTPException(status_code=500, detail="Stored PDF missing")
                    staged_path.unlink(missing_ok=True)
                staged_path = None
            if pdf_by_sha.is_deleted:
                pdf_by_sha.is_deleted = False
                pdf_by_sha.deleted_at = None
                pdf_by_sha.deleted_by_id = None
            if not no_crossref and (
                not pdf_by_sha.title or pdf_by_sha.crossref_status in (None, "pending", "unavailable", "skipped")
            ):
                metadata = await run_in_threadpool(fetch_doi_metadata, pdf_by_sha.doi)
                apply_crossref_metadata(pdf_by_sha, metadata)
                changed = True
            add_upload_record(pdf_by_sha, file, size, user, db, idempotency_key)
            db.commit()
            from netvault_server.server.stats import invalidate_stats_cache

            if changed:
                invalidate_stats_cache()
            db.refresh(pdf_by_sha)
            return UploadResponse(pdf=pdf_to_read(pdf_by_sha), deduplicated=True)

        # DOI parsing and external metadata calls need no open read transaction.
        db.rollback()
        evidence_path = staged_path or object_path(sha256)
        verified_metadata: CrossrefMetadata | None = None
        verification: list[dict] = []
        automatic_resolution = doi_source in AUTOMATIC_CLIENT_DOI_SOURCES or doi is None
        trusted_csv = doi_source == "csv-trusted"
        client_doi = None
        parse_started = monotonic()
        if trusted_csv:
            try:
                normalized_csv_doi = normalize_doi(doi)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
            evidence = DoiEvidence(
                "ok", normalized_csv_doi, "csv-trusted",
                [DoiCandidate(normalized_csv_doi, "csv-trusted", "user-specified-csv", 100)],
            )
            verification = [{
                "doi": normalized_csv_doi, "source": "csv-trusted", "accepted": True,
                "pdf_content_verified": False,
                "reason": "User trusts the DOI/PDF pair in an explicitly specified CSV",
            }]
            timings["parse"] = monotonic() - parse_started
        elif automatic_resolution:
            evidence = await run_in_threadpool(
                extract_doi_evidence, evidence_path, filename=file.filename
            )
            timings["parse"] = monotonic() - parse_started
            if doi:
                try:
                    client_doi = normalize_doi(doi)
                except ValueError as exc:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
            if not evidence.candidates and not client_doi and (
                no_crossref or not await run_in_threadpool(extract_pdf_title, evidence_path)
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=evidence.reason or "No DOI found in PDF. Pass --doi DOI when uploading.",
                )
            if not no_crossref:
                verification_started = monotonic()
                evidence, verified_metadata, verification = await verify_automatic_doi(
                    evidence,
                    evidence_path,
                    client_doi=client_doi,
                    client_source=doi_source,
                )
                timings["verify"] = monotonic() - verification_started
        else:
            evidence = await run_in_threadpool(
                extract_doi_evidence, evidence_path,
                explicit_doi=doi,
                filename=file.filename,
            )
            timings["parse"] = monotonic() - parse_started
            if not no_crossref and evidence.status == "ok":
                verification_started = monotonic()
                evidence, verified_metadata, verification = await verify_automatic_doi(
                    evidence,
                    evidence_path,
                )
                timings["verify"] = monotonic() - verification_started
        if doi_source is not None and not automatic_resolution and evidence.status == "ok":
            evidence = replace(evidence, source=doi_source)
        if evidence.status == "conflict":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=evidence.reason or "DOI candidates are ambiguous; confirm with --doi",
            )
        if evidence.status != "ok" or not evidence.doi:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=evidence.reason or "No DOI found in PDF. Pass --doi DOI when uploading.",
            )
        normalized_doi = evidence.doi

        if force and verified_metadata is not None and verified_metadata.status != "ok":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Force upload requires externally verified DOI metadata",
            )

        persist_started = monotonic()
        # Different byte hashes can resolve to the same DOI concurrently. Serialize
        # the lookup/create decision even when the DOI row does not exist yet.
        # Drop the read transaction before waiting so it holds no DB connection
        # and the next lookup sees the previous upload's committed row.
        db.rollback()
        doi_lock = await run_in_threadpool(
            acquire_object_lock,
            "doi-" + hashlib.sha256(normalized_doi.encode()).hexdigest(),
        )
        pdf_by_sha = db.scalar(select(Pdf).where(Pdf.sha256 == sha256))
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
                detail=(
                    f"This PDF is already linked to DOI {pdf_by_sha.doi}. "
                    "Force upload cannot change DOI identity; ask an administrator to correct it."
                ),
            )
        if force and pdf_by_doi is not None:
            metadata = verified_metadata or await run_in_threadpool(
                fetch_doi_metadata,
                normalized_doi,
            )
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
            pdf_by_doi.doi_evidence = doi_evidence_json(evidence, verification)
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

        if (
            pdf_by_doi is not None
            and pdf_by_doi.sha256 != sha256
            and (trusted_csv or (
                verified_metadata is not None and verified_metadata.status == "ok"
            ))
            and not force
        ):
            restored = pdf_by_doi.is_deleted
            if restored:
                pdf_by_doi.is_deleted = False
                pdf_by_doi.deleted_at = None
                pdf_by_doi.deleted_by_id = None
            if alias_pdf_id is None:
                db.add(
                    PdfFileAlias(
                        pdf_id=pdf_by_doi.id,
                        sha256=sha256,
                        source="csv-trusted" if trusted_csv else "doi-verified",
                        asserted_by_id=user.id,
                    )
                )
            add_upload_record(pdf_by_doi, file, size, user, db, idempotency_key)
            db.commit()
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
                staged_path = None
            from netvault_server.server.stats import invalidate_stats_cache

            if restored:
                invalidate_stats_cache()
            db.refresh(pdf_by_doi)
            return UploadResponse(pdf=pdf_to_read(pdf_by_doi), deduplicated=True)

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
                doi_evidence=doi_evidence_json(evidence, verification),
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
            pdf.doi_evidence = doi_evidence_json(evidence, verification)
        if pdf.is_deleted:
            pdf.is_deleted = False
            pdf.deleted_at = None
            pdf.deleted_by_id = None

        if evidence.source and not pdf.doi_source:
            pdf.doi_source = evidence.source
            pdf.doi_evidence = doi_evidence_json(evidence, verification)
        if not no_crossref and (created_pdf or not pdf.title or pdf.crossref_status in (None, "pending", "unavailable")):
            metadata = verified_metadata or await run_in_threadpool(
                fetch_doi_metadata,
                normalized_doi,
            )
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
        timings["total"] = monotonic() - started
        if persist_started is not None:
            timings["persist"] = monotonic() - persist_started
        if doi_lock is not None:
            release_object_lock(doi_lock)
        release_object_lock(object_lock)
