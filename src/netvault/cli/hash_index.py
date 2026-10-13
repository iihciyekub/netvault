"""Exact hash indexes and a verified, per-run cache for PDF identification."""

from collections.abc import Callable, Iterable
import csv
from pathlib import Path
import re
from threading import Lock

from netvault.cli.identify import doi_filename
from netvault.doi import normalize_doi


def load_hash_index(path: Path) -> dict[str, dict]:
    """Validate the entire exported inventory before any PDF can be renamed."""
    records: dict[str, dict] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, strict=True)
            header = next(reader, None)
            if header is None or len(header) != 2:
                raise ValueError("Index CSV must have doi,sha256 columns")
            columns = [name.strip().lower() for name in header]
            if set(columns) != {"doi", "sha256"}:
                raise ValueError("Index CSV must have doi,sha256 columns")
            doi_column, hash_column = columns.index("doi"), columns.index("sha256")
            for row in reader:
                if len(row) != 2:
                    raise ValueError(f"Invalid index CSV row at line {reader.line_num}")
                doi = normalize_doi(row[doi_column].strip())
                doi_filename(doi)
                sha256 = row[hash_column].strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                    raise ValueError(f"Invalid SHA-256 at index CSV line {reader.line_num}")
                previous = records.get(sha256)
                if previous is not None and previous["doi"] != doi:
                    raise ValueError(f"Conflicting DOIs for SHA-256 {sha256}")
                records[sha256] = {"doi": doi}
    except (UnicodeError, csv.Error) as exc:
        raise ValueError(f"Invalid index CSV: {exc}") from exc
    return records


def index_lookup(records: dict[str, dict]) -> Callable[[Iterable[str]], dict[str, dict]]:
    return lambda hashes: {sha: records[sha] for sha in hashes if sha in records}


def hash_signature(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class RunHashCache:
    """Reuse a hash only while device, inode, size, mtime and ctime agree.

    Nothing is persisted between commands. Rechecking immediately before a move
    reuses unchanged bytes, but modifications (including restored mtime) rehash.
    """

    def __init__(self, hash_file: Callable[[Path], str]):
        self.hash_file = hash_file
        self.records: dict[tuple[int, int, int, int, int], str] = {}
        self.lock = Lock()
        self.reads = 0
        self.hits = 0

    def __call__(self, path: Path) -> str:
        before = hash_signature(path)
        with self.lock:
            cached = self.records.get(before)
            if cached is not None:
                self.hits += 1
                return cached
        sha256 = self.hash_file(path)
        if hash_signature(path) != before:
            raise ValueError("File changed while hashing; left in place")
        with self.lock:
            self.records[before] = sha256
            self.reads += 1
        return sha256
