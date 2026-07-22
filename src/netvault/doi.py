import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

logging.getLogger("pypdf").setLevel(logging.CRITICAL)

DOI_SUFFIX_RE = r"[-._;()/:,A-Z0-9+%<>=&]+"
DOI_RE = re.compile(rf"\b(10\.\d{{4,9}}/{DOI_SUFFIX_RE})", re.IGNORECASE)
STRICT_DOI_RE = re.compile(rf"^10\.\d{{4,9}}/{DOI_SUFFIX_RE}$", re.IGNORECASE)
REFERENCE_HEADING_RE = re.compile(r"(?im)^\s*(references|bibliography|works cited)\s*$")
DOI_LABEL_RE = re.compile(r"\b(doi|digital object identifier|crossmark)\b", re.IGNORECASE)
DOI_METADATA_PATTERNS = [
    re.compile(
        r"(?:prism:doi|crossmark:DOI|pdfx:doi|dc:identifier|WPS-ARTICLEDOI)"
        rf"\s*(?:=|>|\\\(|\()?[^<>\r\n]{{0,240}}?(10\.\d{{4,9}}/{DOI_SUFFIX_RE})",
        re.IGNORECASE,
    )
]
DOI_SOURCE_RANK = {"pdf-content": 3, "filename": 4, "pdf-metadata": 5, "explicit": 6}
DOI_RESOLVER_VERSION = 3
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


def normalize_doi(value: str) -> str:
    doi = value.strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"</[a-z][^>\s]*.*$", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"\)/[a-z][a-z0-9_-].*$", ")", doi, flags=re.IGNORECASE)
    doi = doi.strip().rstrip(TRAILING_PUNCTUATION)
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    doi = doi.lower()
    if not STRICT_DOI_RE.fullmatch(doi):
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


def doi_loose_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_doi(value), flags=re.IGNORECASE)


def doi_matches(left: str, right: str) -> bool:
    return normalize_doi(left) == normalize_doi(right) or doi_loose_key(left) == doi_loose_key(right)


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
    return candidates_from_text(result.stdout, "pdf-content", "pdftotext")


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


def document_info_candidates(reader: PdfReader) -> list[DoiCandidate]:
    candidates = []
    try:
        metadata = reader.metadata or {}
    except Exception:
        return candidates
    for key, value in metadata.items():
        if not isinstance(value, str):
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
    seen = set()

    def add(candidate: DoiCandidate) -> None:
        key = (candidate.doi, candidate.source)
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    for candidate in scan_with_pdftotext(path):
        add(candidate)

    raw = path.read_bytes()
    raw_text = raw.decode("latin-1", errors="ignore")
    for candidate in metadata_candidates(raw_text):
        add(candidate)

    raw_head = raw_text[:250_000]
    for candidate in candidates_from_text(raw_head, "pdf-content", "raw-head")[:5]:
        add(candidate)

    try:
        reader = PdfReader(str(path))
        for candidate in document_info_candidates(reader):
            add(candidate)
        page_texts = [page.extract_text() or "" for page in reader.pages[:3]]
    except Exception:
        page_texts = []
    for index, text in enumerate(page_texts, start=1):
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
    scored_dois.sort(key=lambda row: (row[1], DOI_SOURCE_RANK.get(row[2], 0)), reverse=True)

    doi = None
    source = None
    if filename_doi:
        metadata_conflict = next((metadata_doi for metadata_doi in metadata_dois if not doi_matches(filename_doi, metadata_doi)), None)
        if metadata_conflict:
            return DoiEvidence("conflict", None, None, candidates, "Filename DOI conflicts with PDF metadata DOI")
        doi = filename_matched or filename_doi
        source = "filename" if not filename_matched else choose_source(candidates, doi)
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
