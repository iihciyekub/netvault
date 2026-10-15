"""Independent, conservative evidence for filling an empty PDF Info DOI.

This policy is deliberately stricter than accepting an upload. Neither a
trusted embedded DOI nor a previously accepted upload is writeback evidence.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re

from pypdf import PdfReader

from netvault_server.server.article_title import heading_match_score, normalized_title
from netvault_server.server.crossref import fetch_crossref_metadata
from netvault_server.server.doi import (
    document_info_candidates,
    embedded_in_publisher_url,
    extract_pdf_text,
    normalize_doi,
    repair_doi_line_wraps,
)


POLICY_VERSION = 1
MAX_CANDIDATES = 4
PRINTED_DOI = re.compile(
    r"(?:\bdoi\s*[:=]\s*|https?://(?:dx\.)?doi\.org/)"
    r"(10\.\d{4,9}/[^\s<>\"']+)",
    re.IGNORECASE,
)
REFERENCE_START = re.compile(
    r"(?im)^\s*(?:references|bibliography|works cited|to cite this article)\b"
)
HEADER_END = re.compile(
    r"(?im)^\s*(?:abstract\b|keywords\b|(?:1[. ]+)?introduction\b|references\b)"
)
VERSION_NOTICE = re.compile(
    r"(?im)^\s*(?:correction\b|corrigendum\b|erratum\b|retraction\b|"
    r"comment on\b|reply to\b|response to\b|retracted article\b|"
    r"author manuscript\b|accepted manuscript\b|preprint\b|uncorrected proof\b)"
)


def _receipt(sha256: str, decision: str, reason: str, **values) -> dict:
    result = {
        "policy_version": POLICY_VERSION,
        "input_sha256": sha256,
        "doi": None,
        "decision": decision,
        "writeback_allowed": False,
        "metadata_status": "skipped",
        "metadata_provider": None,
        "canonical_doi": None,
        "printed_doi_confirmed": False,
        "title_match_score": None,
        "author_match": False,
        "year_match": False,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "attempts": [],
    }
    result.update(values)
    return result


def _protected_pdf(reader: PdfReader) -> bool:
    root = reader.trailer["/Root"]
    if "/Perms" in root:
        return True
    form = root.get("/AcroForm")
    if form is None:
        return False
    form = form.get_object()
    if int(form.get("/SigFlags", 0)):
        return True
    pending = [(field, None) for field in form.get("/Fields", [])]
    visited = set()
    while pending:
        reference, inherited_type = pending.pop()
        field = reference.get_object()
        if id(field) in visited:
            continue
        visited.add(id(field))
        if len(visited) > 10000:
            return True
        kind = field.get("/FT", inherited_type)
        if kind == "/Sig" or "/ByteRange" in field:
            return True
        pending.extend((child, kind) for child in field.get("/Kids", []))
    return False


def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _author_line_matches(authors: str | None, heading: str, title: str | None,
                         journal: str | None) -> bool:
    """Require a name on a byline, independently of title and masthead words."""
    target = normalized_title(title or "").replace(" ", "")
    masthead = normalized_title(journal or "").replace(" ", "")
    excluded = re.compile(
        r"\b(?:copyright|university|institute|department|school|faculty|hospital|"
        r"laboratory|publisher|press|edited|editor|journal)\b"
    )
    lines = []
    for raw_line in heading.splitlines():
        line = normalized_title(raw_line)
        compact = line.replace(" ", "")
        if (not compact or (target and compact in target) or (masthead and compact in masthead)
                or excluded.search(line) or re.search(r"https?://|@|©", raw_line)):
            continue
        lines.append(" " + line + " ")
    for name in (authors or "").split(";"):
        parts = normalized_title(name).split()
        if len(parts) < 2:
            continue
        complete = " " + " ".join(parts) + " "
        # Initialled given names are common in article bylines. A surname
        # appearing only in a title or journal cannot satisfy this test.
        initialled = " " + parts[0][0] + " " + parts[-1] + " "
        if any(complete in line or initialled in line for line in lines):
            return True
    return False


def _printed_dois(first_page: str) -> list[str]:
    text = repair_doi_line_wraps(REFERENCE_START.split(first_page, maxsplit=1)[0])
    values = []
    for match in PRINTED_DOI.finditer(text):
        if embedded_in_publisher_url(text, match):
            continue
        try:
            doi = normalize_doi(match.group(1))
        except ValueError:
            continue
        if "/(issn)" not in doi and doi not in values:
            values.append(doi)
    return values


def _publication_year_matches(year: int | None, page: str, title: str | None) -> bool:
    if year is None:
        return False
    # A year embedded in the identifier or article title is not independent
    # publication evidence. Retain publisher dates and footer copyright years.
    text = PRINTED_DOI.sub("", page)
    target = normalized_title(title or "").replace(" ", "")
    for line in text.splitlines():
        compact = normalized_title(line).replace(" ", "")
        if (not compact or re.search(r"https?://|\bdoi\b", line, re.I)
                or (target and (compact in target or target in compact))):
            continue
        if re.search(rf"\b{year}\b", line):
            return True
    return False


def verify_pdf_doi_writeback(path: Path, input_sha256: str) -> dict:
    """Return evidence bound to the exact uploaded bytes; never modify a PDF."""
    if _file_sha256(path) != input_sha256:
        return _receipt(input_sha256, "blocked", "Input PDF hash changed before verification")
    try:
        reader = PdfReader(str(path), strict=True)
        if reader.is_encrypted:
            return _receipt(input_sha256, "blocked", "Encrypted PDFs require manual review")
        if not reader.pages:
            return _receipt(input_sha256, "blocked", "PDF contains no readable pages")
        info = reader.metadata or {}
        for key, value in info.items():
            if str(key).casefold() != "/doi":
                continue
            if not isinstance(value, str):
                return _receipt(input_sha256, "conflict", "Non-text Document Info DOI requires correction")
            if value.strip():
                try:
                    normalize_doi(str(value))
                except ValueError:
                    return _receipt(input_sha256, "conflict", "Nonempty invalid Document Info DOI requires correction")
        embedded = {
            item.doi for item in document_info_candidates(reader)
            if "/(issn)" not in item.doi
        }
        if len(embedded) > 1:
            return _receipt(input_sha256, "conflict", "Multiple Document Info DOI values")
        if embedded:
            return _receipt(input_sha256, "already-present", "Document Info already contains a DOI; no automatic correction", doi=next(iter(embedded)))
        if _protected_pdf(reader):
            return _receipt(input_sha256, "blocked", "Signed PDFs or signature fields require manual review")
        # Accessing every page object catches structural damage before granting
        # a write receipt; page text is still bounded to the first page.
        for page in reader.pages:
            _ = page.mediabox
    except Exception:
        return _receipt(input_sha256, "blocked", "PDF structure or metadata could not be safely read")

    first_page = extract_pdf_text(path).split("\f", 1)[0]
    if not first_page.strip():
        return _receipt(input_sha256, "unverified", "First-page text is unavailable; automatic OCR guesses are not written")
    candidates = _printed_dois(first_page)
    if not candidates:
        return _receipt(input_sha256, "unverified", "No labeled article DOI or DOI resolver URL is printed on the first page")
    if len(candidates) > MAX_CANDIDATES:
        return _receipt(input_sha256, "unverified", "Too many first-page DOI candidates for unambiguous writeback")

    # Author evidence must occur in the article heading, not in abstract/body
    # prose or references. Year evidence may appear in the publisher footer.
    heading = HEADER_END.split(first_page, maxsplit=1)[0]
    if VERSION_NOTICE.search(heading):
        return _receipt(input_sha256, "unverified", "Correction, commentary or manuscript version notices require manual identity review")
    page_before_references = REFERENCE_START.split(first_page, maxsplit=1)[0]
    attempts = []
    verified = []
    unavailable = False
    for candidate in candidates:
        metadata = fetch_crossref_metadata(candidate)
        attempt = {
            "doi": candidate,
            "metadata_status": metadata.status,
            "metadata_provider": metadata.provider,
            "accepted": False,
        }
        attempts.append(attempt)
        if metadata.status != "ok":
            unavailable = unavailable or metadata.status == "unavailable"
            attempt["reason"] = "Crossref metadata is unavailable" if metadata.status == "unavailable" else "Crossref has no record for this DOI"
            continue
        try:
            canonical = normalize_doi(metadata.canonical_doi or "")
        except ValueError:
            canonical = None
        if metadata.provider != "crossref" or canonical != candidate:
            attempt["reason"] = "Crossref canonical DOI does not match the printed DOI"
            continue
        if metadata.work_type not in {"journal-article", "proceedings-article"}:
            attempt["reason"] = "DOI is not a supported article type"
            continue
        score = heading_match_score(metadata.title, first_page) or 0.0
        author_match = _author_line_matches(metadata.authors, heading, metadata.title,
                                            metadata.container_title)
        year_match = _publication_year_matches(metadata.published_year,
                                               page_before_references, metadata.title)
        attempt.update(canonical_doi=canonical, title_match_score=score,
                       author_match=author_match, year_match=year_match,
                       metadata_title=metadata.title)
        if score < 0.98 or not author_match or not year_match:
            attempt["reason"] = "First-page title, author and publication year must independently corroborate the DOI"
            continue
        attempt.update(accepted=True, reason="Printed DOI and independent first-page identity agree with Crossref")
        verified.append((candidate, score))

    if len(verified) > 1:
        return _receipt(input_sha256, "conflict", "Multiple independently supported DOI identities; manual review required", attempts=attempts)
    if unavailable:
        return _receipt(input_sha256, "unavailable", "Crossref availability is insufficient to rule out competing identities", metadata_status="unavailable", metadata_provider="crossref", attempts=attempts)
    if len(verified) != 1:
        return _receipt(input_sha256, "unverified", "No DOI met the independent writeback requirements", metadata_provider="crossref", attempts=attempts)
    if _file_sha256(path) != input_sha256:
        return _receipt(input_sha256, "blocked", "Input PDF hash changed during verification", attempts=attempts)
    doi, score = verified[0]
    return _receipt(
        input_sha256, "verified", "One printed article DOI is corroborated by Crossref, first-page title, author and year",
        doi=doi, canonical_doi=doi, writeback_allowed=True,
        metadata_status="ok", metadata_provider="crossref",
        printed_doi_confirmed=True, title_match_score=score,
        author_match=True, year_match=True, attempts=attempts,
    )
