import re
from pathlib import Path

from pypdf import PdfReader

DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;:)>]}'\""


def normalize_doi(value: str) -> str:
    doi = value.strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    doi = doi.strip().rstrip(TRAILING_PUNCTUATION).lower()
    if not DOI_RE.fullmatch(doi):
        raise ValueError("Invalid DOI")
    return doi


def find_doi_in_text(text: str) -> str | None:
    match = DOI_RE.search(text)
    if not match:
        return None
    return normalize_doi(match.group(0))


def extract_doi_from_pdf(path: Path) -> str | None:
    raw = path.read_bytes()
    raw_text = raw.decode("latin-1", errors="ignore")
    if doi := find_doi_in_text(raw_text):
        return doi

    try:
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages[:3])
    except Exception:
        return None
    return find_doi_in_text(text)
