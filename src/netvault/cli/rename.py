"""Exact hash indexes and a verified, per-run cache for fast PDF renames."""

from collections.abc import Callable, Iterable
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import csv
import os
from pathlib import Path
import re
from threading import Lock

from netvault.cli.identify import (
    CSV_FIELDS, LocalPdf, doi_filename, file_signature, move_without_overwrite,
    open_report, scan_pdfs,
)
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


def fast_rename_pdfs(
    directory: Path, *, rename: bool, csv_path: Path | None,
    hash_file: Callable[[Path], str], lookup: Callable[[Iterable[str]], dict[str, dict]],
    workers: int = 4, batch_size: int = 1, progress=None,
) -> tuple[Path, dict[str, int]]:
    """Bound hashing to 2 * workers futures and overlap it with matching/moves.

    Offline indexes match one file at a time. Server lookups use batches of 500.
    Freeze the path list before starting so renamed files are not scanned again.
    """
    directory = directory.expanduser().resolve()
    pdfs = scan_pdfs(directory, set())
    requested = csv_path.expanduser().absolute() if csv_path is not None else None
    report_path, report = open_report(directory, requested)
    counts: dict[str, int] = {}
    reserved: set[str] = set()

    def hash_pdf(pdf: LocalPdf) -> LocalPdf:
        try:
            if pdf.path.is_symlink() or not pdf.path.is_file():
                raise ValueError("Symbolic links and non-regular files are skipped")
            before = file_signature(pdf.path)
            pdf.sha256 = hash_file(pdf.path)
            pdf.signature = file_signature(pdf.path)
            if before != pdf.signature:
                raise ValueError("File changed while hashing")
        except (OSError, ValueError) as exc:
            pdf.error = str(exc)
        return pdf

    def hashed_batches(executor):
        paths = iter(pdfs)
        pending = deque()
        for _ in range(workers * 2):
            pdf = next(paths, None)
            if pdf is not None:
                pending.append(executor.submit(hash_pdf, pdf))
        batch = []
        while pending:
            pdf = pending.popleft().result()
            following = next(paths, None)
            if following is not None:
                pending.append(executor.submit(hash_pdf, following))
            batch.append(pdf)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    with report, ThreadPoolExecutor(max_workers=workers) as executor:
        writer = csv.DictWriter(report, fieldnames=CSV_FIELDS)
        writer.writeheader()
        report.flush()
        os.fsync(report.fileno())
        completed = 0
        for batch in hashed_batches(executor):
            matches = lookup(pdf.sha256 for pdf in batch if not pdf.error)
            for pdf in batch:
                current, proposed = pdf.path, pdf.path
                error, doi = pdf.error, ""
                status = "error" if error else "not_found"
                if not error:
                    try:
                        record = matches.get(pdf.sha256)
                        if record is not None:
                            doi = normalize_doi(record["doi"])
                            proposed = current.with_name(doi_filename(doi))
                            status = "already_named" if proposed == current else "would_rename"
                            if proposed != current:
                                key = str(proposed).casefold()
                                if os.path.lexists(proposed) or key in reserved:
                                    status = "conflict"
                                    error = "Destination already exists or is reserved by another PDF"
                                else:
                                    reserved.add(key)
                                    if rename:
                                        if file_signature(current) != pdf.signature:
                                            raise RuntimeError("File changed since hashing; left in place")
                                        move_without_overwrite(current, proposed, pdf.sha256, hash_file)
                                        current, status = proposed, "renamed"
                    except FileExistsError:
                        status, error = "conflict", "Destination appeared during processing; source retained"
                    except (KeyError, TypeError, OSError, ValueError, RuntimeError) as exc:
                        status, error = "error", str(exc)
                row = dict.fromkeys(CSV_FIELDS, "")
                row.update({
                    "sha256": pdf.sha256, "doi": doi, "directory": str(current.parent),
                    "original_filename": pdf.path.name, "filename": current.name,
                    "original_path": str(pdf.path), "path": str(current),
                    "proposed_path": str(proposed), "status": status,
                    "match_status": "error" if pdf.error else ("matched" if doi else "not_found"),
                    "error": error,
                })
                writer.writerow(row)
                report.flush()
                os.fsync(report.fileno())
                counts[status] = counts.get(status, 0) + 1
                completed += 1
                if progress:
                    progress("Hashing and renaming PDFs", completed, len(pdfs))
    return report_path, counts
