import logging
import re
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from pypdf import PdfReader

logging.getLogger("pypdf").setLevel(logging.CRITICAL)

DOI_SUFFIX_RE = r"[-._;()/:,A-Z0-9+%=&]+"
DOI_RE = re.compile(rf"\b(10\.\d{{4,9}}/{DOI_SUFFIX_RE})", re.IGNORECASE)
STRICT_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
DOI_URL_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
PDF_OBJECT_DELIMITERS = (">>", "<<")
REFERENCE_HEADING_RE = re.compile(r"(?im)^\s*(references|bibliography|works cited)\s*$")
DOI_LABEL_RE = re.compile(r"\b(doi|digital object identifier|crossmark)\b", re.IGNORECASE)
RAW_EXPLICIT_DOI_RE = re.compile(
    rf"(?:\bdoi\b\s*:?\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{{4,9}}/{DOI_SUFFIX_RE})",
    re.IGNORECASE,
)
DOI_METADATA_PATTERNS = [
    re.compile(
        r"(?:prism:doi|crossmark:DOI|pdfx:doi|dc:identifier|WPS-ARTICLEDOI)"
        rf"\s*(?:=|>|\\\(|\()?[^<>\r\n]{{0,240}}?(10\.\d{{4,9}}/{DOI_SUFFIX_RE})",
        re.IGNORECASE,
    )
]
DOI_SOURCE_RANK = {"pdf-content": 3, "filename": 4, "pdf-metadata": 5, "explicit": 6}
TRAILING_PUNCTUATION = " \t\r\n.,;:]>}'\""


@dataclass(frozen=True)
class DoiCandidate:
    doi: str
    source: str
    detail: str = ""
    score: int = 0
    context: str = ""
    embedded_publisher_url: bool = False


@dataclass(frozen=True)
class DoiEvidence:
    status: str
    doi: str | None
    source: str | None
    candidates: list[DoiCandidate]
    reason: str | None = None
    pdf_text: str | None = field(default=None, compare=False, repr=False)


def normalize_doi(value: str) -> str:
    doi = unicodedata.normalize("NFKC", value.strip())
    if DOI_URL_PREFIX_RE.match(doi):
        doi = DOI_URL_PREFIX_RE.sub("", doi)
        doi = unquote(doi)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    delimiter_positions = [doi.find(token) for token in PDF_OBJECT_DELIMITERS if token in doi]
    if delimiter_positions:
        doi = doi[: min(position for position in delimiter_positions if position >= 0)]
    doi = re.sub(r"</[a-z][^>\s]*.*$", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"\)/[a-z][a-z0-9_-].*$", ")", doi, flags=re.IGNORECASE)
    doi = doi.strip().rstrip(TRAILING_PUNCTUATION)
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    doi = doi.lower()
    if not STRICT_DOI_RE.fullmatch(doi) or any(char.isspace() for char in doi):
        raise ValueError("Invalid DOI")
    return doi


def find_doi_in_text(text: str) -> str | None:
    match = DOI_RE.search(text)
    if not match:
        return None
    return normalize_doi(match.group(1))


def find_dois_in_text(text: str) -> list[str]:
    dois = []
    for match in DOI_RE.finditer(text):
        try:
            doi = normalize_doi(match.group(1))
        except ValueError:
            continue
        if doi not in dois:
            dois.append(doi)
    return dois


def find_metadata_dois_in_text(text: str) -> list[DoiCandidate]:
    candidates = []
    for pattern in DOI_METADATA_PATTERNS:
        for match in pattern.finditer(text):
            try:
                candidates.append(DoiCandidate(normalize_doi(match.group(1)), "pdf-metadata"))
            except ValueError:
                continue
    return candidates


def find_filename_doi_from_name(filename: str) -> DoiCandidate | None:
    path = Path(filename)
    direct = find_doi_in_text(path.stem)
    if direct:
        return DoiCandidate(direct, "filename", path.name, 90)
    safe_name = re.match(r"^10[._](\d{4,9})[_-](.+)$", path.stem, re.IGNORECASE)
    if safe_name:
        try:
            return DoiCandidate(normalize_doi(f"10.{safe_name.group(1)}/{safe_name.group(2)}"), "filename", path.name, 90)
        except ValueError:
            return None

    springer = re.match(r"^(s\d{4,9}-\d{3}-\d{5}(?:-\d)?)", path.stem, re.IGNORECASE)
    if springer:
        try:
            return DoiCandidate(normalize_doi(f"10.1007/{springer.group(1)}"), "filename", path.name, 86)
        except ValueError:
            return None

    plos = re.match(r"^(journal\.pone\.\d+)", path.stem, re.IGNORECASE)
    if plos:
        try:
            return DoiCandidate(normalize_doi(f"10.1371/{plos.group(1)}"), "filename", path.name, 86)
        except ValueError:
            return None

    frontiers = re.match(r"^(fpsyg)-(\d+)-(\d+)", path.stem, re.IGNORECASE)
    if frontiers:
        volume = int(frontiers.group(2))
        # Frontiers in Psychology volume 13 is 2022, volume 14 is 2023, etc.
        year = 2009 + volume
        try:
            return DoiCandidate(
                normalize_doi(f"10.3389/{frontiers.group(1)}.{year}.{frontiers.group(3)}"),
                "filename",
                path.name,
                82,
            )
        except ValueError:
            return None

    return None


def doi_matches(left: str, right: str) -> bool:
    return normalize_doi(left) == normalize_doi(right)


def unique_dois(candidates: list[DoiCandidate], sources: set[str] | None = None) -> list[str]:
    dois = []
    for candidate in candidates:
        if sources and candidate.source not in sources:
            continue
        if candidate.doi not in dois:
            dois.append(candidate.doi)
    return dois


def choose_candidate(candidates: list[DoiCandidate], doi: str) -> DoiCandidate | None:
    matching = [candidate for candidate in candidates if candidate.doi == doi]
    if not matching:
        return None
    return max(matching, key=lambda candidate: (candidate.score, DOI_SOURCE_RANK.get(candidate.source, 0)))


def choose_source(candidates: list[DoiCandidate], doi: str) -> str:
    candidate = choose_candidate(candidates, doi)
    return candidate.source if candidate else "pdf-content"


def candidate_context(text: str, start: int, end: int, width: int = 90) -> str:
    context = re.sub(r"\s+", " ", text[max(0, start - width) : min(len(text), end + width)]).strip()
    return context[:220]


def repair_doi_line_wraps(text: str) -> str:
    # Join only DOI tokens, never arbitrary PDF prose or adjacent reference titles.
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "").replace("\u200b", "")
    text = re.sub(r"\b(10\.\d{4,9}/)[ \t]*\r?\n[ \t]*(?=[A-Z0-9])", r"\1", text, flags=re.I)
    pattern = re.compile(
        rf"\b(10\.\d{{4,9}}/{DOI_SUFFIX_RE}[-/_.])[ \t]*\r?\n[ \t]*"
        rf"([A-Z0-9]{DOI_SUFFIX_RE})(?=\s|$)",
        re.I,
    )
    # Require punctuation or numbers in the continuation to avoid swallowing prose.
    for _ in range(3):
        text = pattern.sub(
            lambda m: m[1] + m[2] if re.search(r"[0-9/_.-]", m[2]) else m[0], text
        )
    return text


def embedded_in_publisher_url(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - 500) : match.start()]
    url_match = re.search(r"https?://([^/\s<>'\"]+)[^\s<>'\"]*$", before, re.IGNORECASE)
    if not url_match:
        return False
    hostname = url_match.group(1).lower().split(":", 1)[0].removeprefix("www.")
    return hostname not in {"doi.org", "dx.doi.org"}


def score_text_candidate(
    text: str,
    match: re.Match[str],
    page: int | None = None,
    in_references: bool = False,
    embedded_publisher_url: bool = False,
) -> int:
    before = text[max(0, match.start() - 140) : match.start()]
    after = text[match.end() : min(len(text), match.end() + 80)]
    score = 54
    if page == 1:
        score += 18
    elif page == 2:
        score += 8
    if DOI_LABEL_RE.search(before + after):
        score += 18
    if embedded_publisher_url:
        score -= 40
    if in_references or re.search(r"(?i)\b(references|bibliography|works cited|cited by)\b", before[-80:]):
        score -= 38
    return max(5, min(score, 88))


def candidates_from_text(text: str, source: str, detail: str, page: int | None = None) -> list[DoiCandidate]:
    text = repair_doi_line_wraps(text)
    reference_match = REFERENCE_HEADING_RE.search(text)
    reference_start = reference_match.start() if reference_match else None
    candidates = []
    for match in DOI_RE.finditer(text):
        try:
            doi = normalize_doi(match.group(1))
        except ValueError:
            continue
        in_references = reference_start is not None and match.start() >= reference_start
        in_publisher_url = embedded_in_publisher_url(text, match)
        candidates.append(
            DoiCandidate(
                doi=doi,
                source=source,
                detail=detail,
                score=score_text_candidate(
                    text,
                    match,
                    page=page,
                    in_references=in_references,
                    embedded_publisher_url=in_publisher_url,
                ),
                context=candidate_context(text, match.start(), match.end()),
                embedded_publisher_url=in_publisher_url,
            )
        )
    return candidates


def _pdftotext(path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "3", str(path), "-"],
            check=False, capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _text_candidates(text: str) -> list[DoiCandidate]:
    candidates = []
    for index, page_text in enumerate(text.split("\f")[:3], start=1):
        candidates.extend(candidates_from_text(
            page_text, "pdf-content", f"pdftotext-page-{index}", page=index,
        ))
    return candidates


def scan_with_pdftotext(path: Path) -> list[DoiCandidate]:
    return _text_candidates(_pdftotext(path))


def extract_pdf_text(path: Path) -> str:
    """Return usable text from the first three pages for identity verification."""
    text = _pdftotext(path)
    if text.strip():
        return text
    try:
        reader = PdfReader(str(path))
        return _joined_page_texts(readable_pages(reader))
    except Exception:
        return ""


def extract_pdf_title(path: Path) -> str | None:
    """Use document title metadata only; never guess from a downloaded filename."""
    try:
        title = (PdfReader(str(path)).metadata or {}).get("/Title")
    except Exception:
        return None
    if isinstance(title, str) and sum(character.isalnum() for character in title) >= 2:
        return title.strip()
    return None


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
                30 if in_references else 88,
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
        for doi in find_dois_in_text(value):
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


def _joined_page_texts(pages: list[tuple[int, str]]) -> str:
    """Preserve page boundaries and damaged-page gaps for first-page verification."""
    texts = dict(pages)
    return "\f".join(texts.get(index, "") for index in range(1, max(texts, default=0) + 1))


def extract_doi_from_pdf(path: Path) -> str | None:
    evidence = extract_doi_evidence(path)
    return evidence.doi if evidence.status == "ok" else None


def extract_doi_evidence(path: Path, explicit_doi: str | None = None, filename: str | None = None) -> DoiEvidence:
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

    pdf_text = _pdftotext(path)
    for candidate in _text_candidates(pdf_text):
        add(candidate)

    try:
        with path.open("rb") as handle:
            raw_head = handle.read(512_000).decode("latin-1", errors="ignore")
    except OSError:
        raw_head = ""
    for candidate in metadata_candidates(raw_head):
        add(candidate)
    for candidate in raw_explicit_candidates(raw_head):
        add(candidate)

    try:
        reader = PdfReader(str(path))
        for candidate in document_info_candidates(reader):
            add(candidate)
        for candidate in decoded_xmp_candidates(reader):
            add(candidate)
        for candidate in annotation_uri_candidates(reader):
            add(candidate)
        page_texts = readable_pages(reader)
    except Exception:
        page_texts = []
    for index, text in page_texts:
        for candidate in candidates_from_text(text, "pdf-content", f"pypdf-page-{index}", page=index):
            add(candidate)

    if not pdf_text.strip():
        pdf_text = _joined_page_texts(page_texts)

    def resolved(*args) -> DoiEvidence:
        return DoiEvidence(*args, pdf_text=pdf_text)

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
    scored_dois.sort(key=lambda row: (row[1], DOI_SOURCE_RANK.get(row[2], 0)), reverse=True)

    doi = None
    source = None
    if filename_doi:
        metadata_conflict = next(
            (
                metadata_doi
                for metadata_doi in metadata_dois
                if not doi_matches(filename_doi, metadata_doi)
            ),
            None,
        )
        if metadata_conflict:
            return resolved(
                "conflict",
                None,
                None,
                candidates,
                "Filename DOI conflicts with PDF metadata DOI",
            )
        if filename_matched:
            doi = filename_matched
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
            return resolved("conflict", None, None, candidates, "Multiple metadata DOI values")
    elif content_dois:
        best_doi, best_score, _ = scored_dois[0]
        second_score = scored_dois[1][1] if len(scored_dois) > 1 else 0
        if len(content_dois) > 1 and best_score - second_score < 18:
            return resolved(
                "conflict",
                None,
                None,
                candidates,
                "Multiple PDF content DOI values have similar confidence",
            )
        doi = best_doi
        source = choose_source(candidates, doi)

    if not doi:
        return resolved("no-doi", None, None, candidates, "No DOI found")
    return resolved("ok", doi, source, candidates)


def doi_evidence_requires_confirmation(evidence: DoiEvidence) -> bool:
    return bool(
        evidence.doi
        and any(
            candidate.doi == evidence.doi and candidate.embedded_publisher_url
            for candidate in evidence.candidates
        )
    )
