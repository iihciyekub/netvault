import logging
import re
import subprocess
from pathlib import Path

from pypdf import PdfReader

from netvault.identity_shared import (
    DoiCandidate as DoiCandidate,
    DoiEvidence as DoiEvidence,
    normalize_doi as normalize_doi,
    find_doi_in_text as find_doi_in_text,
    find_dois_in_text as find_dois_in_text,
    find_metadata_dois_in_text as find_metadata_dois_in_text,
    find_filename_doi_from_name as find_filename_doi_from_name,
    doi_matches as doi_matches,
    unique_dois as unique_dois,
    choose_candidate as choose_candidate,
    choose_source as choose_source,
    candidate_context as candidate_context,
    repair_doi_line_wraps as repair_doi_line_wraps,
    embedded_in_publisher_url as embedded_in_publisher_url,
    score_text_candidate as score_text_candidate,
    candidates_from_text as candidates_from_text,
    normalize_issn as normalize_issn,
    journal_display_name as journal_display_name,
    normalize_journal_key as normalize_journal_key,
    DOI_SUFFIX_RE as DOI_SUFFIX_RE,
    DOI_RE as DOI_RE,
    STRICT_DOI_RE as STRICT_DOI_RE,
    DOI_URL_PREFIX_RE as DOI_URL_PREFIX_RE,
    PDF_OBJECT_DELIMITERS as PDF_OBJECT_DELIMITERS,
    REFERENCE_HEADING_RE as REFERENCE_HEADING_RE,
    DOI_LABEL_RE as DOI_LABEL_RE,
    RAW_EXPLICIT_DOI_RE as RAW_EXPLICIT_DOI_RE,
    DOI_METADATA_PATTERNS as DOI_METADATA_PATTERNS,
    DOI_SOURCE_RANK as DOI_SOURCE_RANK,
    DOI_RESOLVER_VERSION as DOI_RESOLVER_VERSION,
    TRAILING_PUNCTUATION as TRAILING_PUNCTUATION,
 )

logging.getLogger("pypdf").setLevel(logging.CRITICAL)

def scan_with_pdftotext(path: Path) -> list[DoiCandidate]:
    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "3", str(path), "-"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not result.stdout:
        return []
    candidates = []
    for index, text in enumerate(result.stdout.split("\f")[:3], start=1):
        candidates.extend(candidates_from_text(text, "pdf-content", f"pdftotext-page-{index}", page=index))
    return candidates


def raw_explicit_candidates(raw_text: str) -> list[DoiCandidate]:
    reference_match = REFERENCE_HEADING_RE.search(raw_text)
    reference_start = reference_match.start() if reference_match else None
    candidates = []
    for match in RAW_EXPLICIT_DOI_RE.finditer(raw_text):
        try:
            doi = normalize_doi(match.group(1))
        except ValueError:
            continue
        in_references = reference_start is not None and match.start(1) >= reference_start
        candidates.append(
            DoiCandidate(
                doi,
                "pdf-content",
                "raw-explicit",
                20 if in_references else (88 if match.start() < 128 else 25),
                candidate_context(raw_text, match.start(1), match.end(1)),
            )
        )
    return candidates


def metadata_candidates(raw_text: str) -> list[DoiCandidate]:
    candidates = []
    for pattern in DOI_METADATA_PATTERNS:
        for match in pattern.finditer(raw_text):
            try:
                candidates.append(
                    DoiCandidate(
                        normalize_doi(match.group(1)),
                        "pdf-metadata",
                        "xmp",
                        96,
                        candidate_context(raw_text, match.start(), match.end()),
                    )
                )
            except ValueError:
                continue
    return candidates


def annotation_uri_candidates(reader: PdfReader) -> list[DoiCandidate]:
    candidates = []
    for page_index, page in enumerate(reader.pages[:3], start=1):
        try:
            annotations = page.get("/Annots") or []
        except Exception:
            continue
        for annotation_ref in annotations:
            try:
                annotation = annotation_ref.get_object()
                action = annotation.get("/A")
                if action is not None and hasattr(action, "get_object"):
                    action = action.get_object()
                uri = action.get("/URI") if action else None
            except Exception:
                continue
            if not isinstance(uri, str) or not DOI_URL_PREFIX_RE.match(uri.strip()):
                continue
            try:
                doi = normalize_doi(uri)
            except ValueError:
                continue
            candidates.append(
                DoiCandidate(
                    doi,
                    "pdf-metadata",
                    f"annotation-uri-page-{page_index}",
                    94,
                    uri[:220],
                )
            )
    return candidates


def document_info_candidates(reader: PdfReader) -> list[DoiCandidate]:
    candidates = []
    try:
        metadata = reader.metadata or {}
    except Exception:
        return candidates
    for key, value in metadata.items():
        if not isinstance(value, str) or re.search(r"(?:journal|issue|volume).*doi", str(key), re.I):
            continue
        if str(key).casefold() == "/doi":
            try:
                # A dedicated DOI field holds one complete identifier; the
                # text scanner intentionally supports a narrower alphabet.
                dois = [normalize_doi(value)]
            except ValueError:
                # Inspect whole whitespace-delimited tokens only. Never turn
                # an unsupported DOI character into a different, shorter DOI.
                dois = []
                for token in value.split():
                    try:
                        parsed = normalize_doi(token)
                    except ValueError:
                        continue
                    if parsed not in dois:
                        dois.append(parsed)
        else:
            dois = find_dois_in_text(value)
        for doi in dois:
            candidates.append(
                DoiCandidate(
                    doi,
                    "pdf-metadata",
                    f"document-info:{key}",
                    96,
                    value[:220],
                )
            )
    return candidates


def decoded_xmp_candidates(reader: PdfReader) -> list[DoiCandidate]:
    try:
        xmp = reader.xmp_metadata
        if xmp is None:
            return []
        text = xmp.stream.get_data().decode("utf-8", errors="replace")
    except Exception:
        return []
    return metadata_candidates(text)


def readable_pages(reader: PdfReader) -> list[tuple[int, str]]:
    texts = []
    for index in range(min(3, len(reader.pages))):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:
            continue
        texts.append((index + 1, text))
    return texts


def extract_doi_from_pdf(path: Path) -> str | None:
    evidence = extract_doi_evidence(path)
    return evidence.doi if evidence.status == "ok" else None


def extract_document_info_evidence(path: Path) -> DoiEvidence | None:
    """Read authoritative Info DOI evidence without scanning XMP or page content.

    None preserves the legacy resolver when Info has no usable article DOI.
    A conflict must be reported and must never enter candidate guessing.
    """
    try:
        embedded_reader = PdfReader(str(path))
        embedded = document_info_candidates(embedded_reader)
        embedded = [item for item in embedded if "/(issn)" not in item.doi.casefold()]
    except Exception:
        embedded = []
    embedded_dois = unique_dois(embedded, {"pdf-metadata"})
    if len(embedded_dois) > 1:
        return DoiEvidence(
            "conflict", None, None, embedded, "Multiple Document Info DOI values"
        )
    if embedded_dois:
        return DoiEvidence("ok", embedded_dois[0], "pdf-metadata", embedded)

    return None


def extract_doi_evidence(path: Path, explicit_doi: str | None = None, filename: str | None = None) -> DoiEvidence:
    embedded = extract_document_info_evidence(path)
    if embedded is not None:
        return embedded

    if explicit_doi:
        try:
            doi = normalize_doi(explicit_doi)
        except ValueError:
            return DoiEvidence("no-doi", None, None, [], "Explicit DOI is invalid")
        return DoiEvidence("ok", doi, "explicit", [DoiCandidate(doi, "explicit", "--doi")])

    candidates: list[DoiCandidate] = []
    positions = {}

    def add(candidate: DoiCandidate) -> None:
        if "/(issn)" in candidate.doi.casefold():
            return
        key = (candidate.doi, candidate.source)
        if key in positions:
            index = positions[key]
            if candidate.score > candidates[index].score:
                candidates[index] = candidate
            return
        positions[key] = len(candidates)
        candidates.append(candidate)

    for candidate in scan_with_pdftotext(path):
        add(candidate)

    try:
        with path.open("rb") as handle:
            raw_head = handle.read(512_000).decode("latin-1", errors="ignore")
    except OSError:
        raw_head = ""
    for candidate in raw_explicit_candidates(raw_head):
        add(candidate)

    try:
        reader = PdfReader(str(path))
        for candidate in document_info_candidates(reader):
            add(candidate)
        for candidate in annotation_uri_candidates(reader):
            add(candidate)
        page_texts = readable_pages(reader)
    except Exception:
        page_texts = []
    for index, text in page_texts:
        for candidate in candidates_from_text(text, "pdf-content", f"pypdf-page-{index}", page=index):
            add(candidate)

    filename_candidate = find_filename_doi_from_name(filename) if filename else find_filename_doi_from_name(path.name)
    if filename_candidate:
        add(filename_candidate)

    metadata_dois = unique_dois(candidates, {"pdf-metadata"})
    content_dois = unique_dois(candidates, {"pdf-content"})
    pdf_dois = unique_dois(candidates, {"pdf-content", "pdf-metadata"})
    filename_doi = filename_candidate.doi if filename_candidate else None
    filename_matched = None
    if filename_doi:
        filename_matched = next((doi for doi in pdf_dois if doi_matches(filename_doi, doi)), None)

    scored_dois = []
    for candidate_doi in unique_dois(candidates):
        best = choose_candidate(candidates, candidate_doi)
        if best:
            scored_dois.append((candidate_doi, best.score, best.source))
    scored_dois.sort(key=lambda row: (DOI_SOURCE_RANK.get(row[2], 0), row[1]), reverse=True)

    doi = None
    source = None
    if filename_doi:
        # Filename hints cannot veto a DOI actually present in the PDF.
        if filename_matched:
            doi = filename_matched
            source = choose_source(candidates, doi)
        elif len(metadata_dois) == 1:
            doi = metadata_dois[0]
            source = choose_source(candidates, doi)
        else:
            strong_content = [
                candidate
                for candidate in candidates
                if candidate.source == "pdf-content" and candidate.score >= 80
            ]
            strong_content.sort(key=lambda candidate: candidate.score, reverse=True)
            if strong_content and (
                len(strong_content) == 1
                or strong_content[0].score - strong_content[1].score >= 18
                or strong_content[0].doi == strong_content[1].doi
            ):
                doi = strong_content[0].doi
                source = strong_content[0].source
            else:
                doi = filename_doi
                source = "filename"
    elif metadata_dois:
        if len(metadata_dois) == 1:
            doi = metadata_dois[0]
            source = choose_source(candidates, doi)
        else:
            return DoiEvidence("conflict", None, None, candidates, "Multiple metadata DOI values")
    elif content_dois:
        best_doi, best_score, _ = scored_dois[0]
        second_score = scored_dois[1][1] if len(scored_dois) > 1 else 0
        if len(content_dois) > 1 and best_score - second_score < 18:
            return DoiEvidence(
                "conflict",
                None,
                None,
                candidates,
                "Multiple PDF content DOI values have similar confidence",
            )
        doi = best_doi
        source = choose_source(candidates, doi)

    if not doi:
        return DoiEvidence("no-doi", None, None, candidates, "No DOI found")
    return DoiEvidence("ok", doi, source, candidates)


def doi_evidence_requires_confirmation(evidence: DoiEvidence) -> bool:
    return bool(
        evidence.doi
        and any(
            candidate.doi == evidence.doi and candidate.embedded_publisher_url
            for candidate in evidence.candidates
        )
    )
