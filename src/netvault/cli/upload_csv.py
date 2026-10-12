"""Safe DOI hints from cli-downloads.csv, including renamed PDFs."""
import csv
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

from pypdf import PdfReader

from netvault.doi import normalize_doi

MAX_CSV_BYTES = 64 * 1024 * 1024
MAX_CSV_RECORDS = 100_000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def normalized_title(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def first_page_text(path: Path) -> str:
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted or not reader.pages:
            return ""
        return reader.pages[0].extract_text() or ""
    except Exception:
        return ""


@dataclass(frozen=True)
class CsvRow:
    filename: str
    doi: str
    size: int
    sha256: str
    title: str
    url: str


class DownloadCsv:
    def __init__(self, path: Path, warning_callback=None):
        self.path = path
        self.by_name: dict[str, CsvRow] = {}
        self.by_sha256: dict[str, CsvRow] = {}
        self.by_title: dict[str, CsvRow] = {}
        self._read(warning_callback)

    def _read(self, warn):
        if self.path.stat().st_size > MAX_CSV_BYTES:
            raise ValueError("download CSV exceeds the 64 MiB limit")
        names: dict[str, list[CsvRow]] = {}
        hashes: dict[str, list[CsvRow]] = {}
        titles: dict[str, list[CsvRow]] = {}
        with self.path.open(encoding="utf-8-sig", newline="") as handle:
            for number, original in enumerate(csv.DictReader(handle)):
                if number >= MAX_CSV_RECORDS:
                    raise ValueError("download CSV exceeds the 100,000 record limit")
                row = {str(key).strip().lower(): (value or "").strip()
                       for key, value in original.items() if key is not None}
                name = row.get("file") or row.get("filename") or ""
                if row.get("status", "").lower() != "downloaded":
                    continue
                if not name or Path(name).name != name or Path(name).suffix.lower() != ".pdf":
                    continue
                try:
                    doi = normalize_doi(row.get("doi", ""))
                    size = int(row.get("bytes") or row.get("size") or "0")
                except (ValueError, TypeError):
                    continue
                if size <= 0:
                    continue
                sha = row.get("sha256", "").lower()
                if sha and not SHA256_RE.fullmatch(sha):
                    if warn:
                        warn(f"Ignoring malformed SHA-256 for CSV row {number + 2}: {name}")
                    continue
                title = normalized_title(row.get("title") or row.get("article_title")
                                         or row.get("paper_title") or "")
                entry = CsvRow(name, doi, size, sha, title,
                               row.get("url") or row.get("source_url") or "")
                names.setdefault(name, []).append(entry)
                if sha:
                    hashes.setdefault(sha, []).append(entry)
                if len(title) >= 35 and len(title.split()) >= 5:
                    titles.setdefault(title, []).append(entry)

        def unique(groups, label):
            safe = {}
            for key, candidates in groups.items():
                # Repeated records are safe only when identity and size agree.
                if len({(candidate.doi, candidate.size, candidate.sha256)
                        for candidate in candidates}) == 1:
                    safe[key] = candidates[0]
                elif warn:
                    warn(f"Ignoring ambiguous CSV {label}: {key}")
            return safe

        self.by_name = unique(names, "filename")
        self.by_sha256 = unique(hashes, "SHA-256")
        self.ambiguous_names = set(names) - self.by_name.keys()
        self.ambiguous_hashes = set(hashes) - self.by_sha256.keys()
        # A title is only usable if it identifies a unique DOI. PDF text must also
        # contain the full title near its beginning, not in its references section.
        for title, candidates in titles.items():
            if len({entry.doi for entry in candidates}) == 1:
                self.by_title[title] = candidates[0]
            elif warn:
                warn(f"Ignoring ambiguous CSV title: {title[:80]}")

    def match(self, path: Path, sha256: str, size: int, *, allow_title: bool = True):
        """Return (row, method) or None; explicit CSV matching never reads PDF text."""
        if path.name in self.ambiguous_names or sha256.lower() in self.ambiguous_hashes:
            return None
        hash_row = self.by_sha256.get(sha256.lower())
        if hash_row is not None:
            if hash_row.size != size:
                raise ValueError(f"CSV SHA-256 matches but file size differs: {path.name}")
            named = self.by_name.get(path.name)
            if named is not None and named.doi != hash_row.doi:
                raise ValueError(f"CSV filename DOI conflicts with SHA-256 DOI: {path.name}")
            if named is not None and named.sha256 and named.sha256 != sha256.lower():
                raise ValueError(f"CSV filename conflicts with SHA-256: {path.name}")
            return hash_row, "sha256"

        named = self.by_name.get(path.name)
        if named is not None and named.size == size and (
            not named.sha256 or named.sha256 == sha256.lower()
        ):
            return named, "filename-size"

        if not allow_title or not self.by_title:
            return None
        raw = first_page_text(path)[:2500]
        raw = re.split(r"(?im)^\s*(?:references|bibliography|works cited)\s*$", raw, maxsplit=1)[0]
        text = normalized_title(raw)
        if not text:
            return None
        # Search only early front matter; no fuzzy matches or references.
        start = text[:900]
        candidates = [(title, row) for title, row in self.by_title.items()
                      if title in start]
        if len({row.doi for _, row in candidates}) != 1:
            return None
        return candidates[0][1], "title"
