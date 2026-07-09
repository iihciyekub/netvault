import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

DOI_SUFFIX_RE = r"[-._;()/:,A-Z0-9+%<>=]+"
DOI_RE = re.compile(rf"\b(10\.\d{{4,9}}/{DOI_SUFFIX_RE})", re.IGNORECASE)
STRICT_DOI_RE = re.compile(rf"^10\.\d{{4,9}}/{DOI_SUFFIX_RE}$", re.IGNORECASE)
DOI_METADATA_PATTERNS = [
    re.compile(
        r"(?:prism:doi|crossmark:DOI|pdfx:doi|dc:identifier|WPS-ARTICLEDOI|/DOI|/doi)"
        rf"\s*(?:=|>|\\\(|\()?[^<>\r\n]{{0,240}}?(10\.\d{{4,9}}/{DOI_SUFFIX_RE})",
        re.IGNORECASE,
    )
]
DOI_SOURCE_RANK = {"filename": 2, "pdf-content": 3, "pdf-metadata": 4, "explicit": 6}
TRAILING_PUNCTUATION = " \t\r\n.,;:]>}'\""


@dataclass(frozen=True)
class DoiCandidate:
    doi: str
    source: str
    detail: str = ""


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


def find_filename_doi(path: Path) -> DoiCandidate | None:
    direct = find_doi_in_text(path.stem)
    if direct:
        return DoiCandidate(direct, "filename", path.name)
    safe_name = re.match(r"^10[._](\d{4,9})[_-](.+)$", path.stem, re.IGNORECASE)
    if not safe_name:
        return None
    try:
        return DoiCandidate(normalize_doi(f"10.{safe_name.group(1)}/{safe_name.group(2)}"), "filename", path.name)
    except ValueError:
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
    return [
        doi
        for doi in dois
        if not any(other != doi and other.startswith(doi) and len(other) > len(doi) for other in dois)
    ]


def choose_source(candidates: list[DoiCandidate], doi: str) -> str:
    matching = [candidate.source for candidate in candidates if candidate.doi == doi]
    return max(matching, key=lambda source: DOI_SOURCE_RANK.get(source, 0)) if matching else "pdf-content"


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
    return [DoiCandidate(doi, "pdf-content", "pdftotext") for doi in find_dois_in_text(result.stdout)]


def extract_doi_from_pdf(path: Path) -> str | None:
    evidence = extract_doi_evidence(path)
    return evidence.doi if evidence.status == "ok" else None


def extract_doi_evidence(path: Path, explicit_doi: str | None = None) -> DoiEvidence:
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
    for candidate in find_metadata_dois_in_text(raw_text):
        add(candidate)
    for doi in find_dois_in_text(raw_text)[:5]:
        add(DoiCandidate(doi, "pdf-content"))

    try:
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages[:3])
    except Exception:
        text = ""
    for doi in find_dois_in_text(text):
        add(DoiCandidate(doi, "pdf-content", "pypdf"))

    filename_candidate = find_filename_doi(path)
    if filename_candidate:
        add(filename_candidate)

    metadata_dois = unique_dois(candidates, {"pdf-metadata"})
    content_dois = unique_dois(candidates, {"pdf-content"})
    pdf_dois = unique_dois(candidates, {"pdf-content", "pdf-metadata"})
    filename_doi = filename_candidate.doi if filename_candidate else None
    filename_matched = None
    if filename_doi:
        filename_matched = next((doi for doi in pdf_dois if doi_matches(filename_doi, doi)), None)

    doi = None
    source = None
    if filename_matched:
        doi = filename_matched
        source = choose_source(candidates, doi)
    elif content_dois:
        if len(content_dois) > 1 and not filename_doi:
            return DoiEvidence("conflict", None, None, candidates, "Multiple PDF content DOI values")
        doi = content_dois[0] if len(content_dois) == 1 else filename_doi
        source = choose_source(candidates, doi)
    elif metadata_dois:
        if len(metadata_dois) > 1 and not filename_doi:
            return DoiEvidence("conflict", None, None, candidates, "Multiple metadata DOI values")
        doi = metadata_dois[0] if len(metadata_dois) == 1 else filename_doi
        source = choose_source(candidates, doi)
    elif filename_doi:
        doi = filename_doi
        source = "filename"

    if not doi:
        return DoiEvidence("no-doi", None, None, candidates, "No DOI found")
    if filename_doi and not doi_matches(filename_doi, doi):
        return DoiEvidence("conflict", None, None, candidates, "Filename DOI conflicts with PDF DOI")
    return DoiEvidence("ok", doi, source, candidates)
