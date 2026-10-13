"""Bounded hash-only identification, with per-directory collection and auditing."""

from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
import csv
import os
from pathlib import Path
from time import monotonic

from netvault.cli.identify import (
    CSV_FIELDS, LocalPdf, available_destination, doi_filename, file_signature,
    move_without_overwrite, open_report, scan_for_collections,
)
from netvault.doi import normalize_doi


def fast_identify_pdfs(
    directory: Path, *, rename: bool, duplicates_to: Path | None, csv_path: Path | None,
    hash_file: Callable[[Path], str], lookup: Callable[[Iterable[str]], dict[str, dict]],
    collect_unidentified: bool = True, workers: int = 4, batch_size: int = 1, progress=None,
) -> tuple[Path, dict[str, int]]:
    """Overlap bounded hashing with matching, moves and durable CSV writes.

    No PDF pages are parsed. Index misses remain in place; only full-vault misses
    are unidentified. Identical extra copies are collected across the entire scan.
    """
    if not 1 <= workers <= 32 or batch_size < 1:
        raise ValueError("Workers must be between 1 and 32 and batch size must be positive")
    directory = directory.expanduser().resolve()
    duplicates_to = duplicates_to.expanduser().resolve() if duplicates_to else None
    pdfs = scan_for_collections(
        directory, duplicates_to, collect_by_parent=True,
        collect_unidentified=collect_unidentified,
    )
    requested = csv_path.expanduser().absolute() if csv_path is not None else None
    report_path, report = open_report(directory, requested)
    counts: dict[str, int] = {}
    reserved: set[str] = set()
    keepers: dict[str, Path] = {}
    known_records: dict[str, dict | None] = {}
    owners: dict[str, tuple[str, str, Path]] = {}

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

    def destination_owner(path: Path) -> tuple[str, str, Path] | None:
        owner = owners.get(str(path).casefold())
        if owner is not None:
            return owner
        if path.is_symlink() or not path.is_file():
            return None
        sha256 = hash_file(path)
        if sha256 not in known_records:
            known_records[sha256] = lookup([sha256]).get(sha256)
        record = known_records[sha256]
        if record is None:
            return None
        return normalize_doi(record["doi"]), sha256, path

    with report, ThreadPoolExecutor(max_workers=workers) as executor:
        writer = csv.DictWriter(report, fieldnames=CSV_FIELDS)
        writer.writeheader()
        report.flush()
        os.fsync(report.fileno())
        last_sync = monotonic()
        completed = 0
        for batch in hashed_batches(executor):
            hashes = {pdf.sha256 for pdf in batch if not pdf.error}
            missing = hashes - known_records.keys()
            matches = lookup(sorted(missing)) if missing else {}
            known_records.update({sha: matches.get(sha) for sha in missing})
            for pdf in batch:
                current, proposed = pdf.path, pdf.path
                error, doi = pdf.error, ""
                duplicate_of = conflict_with = conflict_sha = ""
                reference: tuple[Path, str] | None = None
                status = "error" if error else ("not_found" if collect_unidentified else "not_in_index")
                match_status = status
                if not error:
                    try:
                        record = known_records[pdf.sha256]
                        if record is not None:
                            match_status = "error"
                            doi = normalize_doi(record["doi"])
                            doi_filename(doi)
                        match_status = "matched" if doi else status
                        duplicate_directory = duplicates_to or pdf.path.parent / "duplicates"
                        keeper = keepers.get(pdf.sha256)
                        if keeper is not None and keeper != current:
                            duplicate_of = str(keeper)
                            reference = keeper, pdf.sha256
                            proposed = available_destination(duplicate_directory, pdf, reserved)
                            status = "would_move_duplicate"
                        elif doi:
                            proposed = current.with_name(doi_filename(doi))
                            status = "already_named" if proposed == current else "would_rename"
                            if proposed != current and (os.path.lexists(proposed)
                                                        or str(proposed).casefold() in reserved):
                                owner = destination_owner(proposed)
                                if owner is not None and owner[0] == doi:
                                    reference = owner[2], owner[1]
                                    if owner[1] == pdf.sha256:
                                        duplicate_of = str(owner[2])
                                        keepers[pdf.sha256] = owner[2]
                                        status = "would_move_duplicate"
                                    else:
                                        conflict_with, conflict_sha = str(owner[2]), owner[1]
                                        status = "would_move_doi_conflict"
                                    proposed = available_destination(duplicate_directory, pdf, reserved)
                                else:
                                    status = "conflict"
                                    error = "Destination already exists or is reserved by another PDF"
                        elif collect_unidentified:
                            proposed = available_destination(pdf.path.parent / "unidentifys", pdf, reserved)
                            status = "would_move_unidentified"

                        if not error and proposed != current:
                            key = str(proposed).casefold()
                            if os.path.lexists(proposed) or key in reserved:
                                status = "conflict"
                                error = "Destination already exists or is reserved by another PDF"
                            else:
                                reserved.add(key)
                                if rename:
                                    if file_signature(current) != pdf.signature:
                                        raise RuntimeError("File changed since hashing; left in place")
                                    if reference is not None:
                                        path, sha = reference
                                        if path.is_symlink() or hash_file(path) != sha:
                                            raise RuntimeError("Retained reference changed; source left in place")
                                    move_without_overwrite(current, proposed, pdf.sha256, hash_file)
                                    current = proposed
                                    status = {
                                        "would_rename": "renamed",
                                        "would_move_duplicate": "moved_duplicate",
                                        "would_move_doi_conflict": "moved_doi_conflict",
                                        "would_move_unidentified": "moved_unidentified",
                                    }[status]
                        if not error:
                            if not duplicate_of:
                                keepers.setdefault(pdf.sha256, current)
                            if status in {"would_rename", "renamed", "already_named"}:
                                owners[str(proposed).casefold()] = doi, pdf.sha256, current
                    except FileExistsError:
                        status, error = "conflict", "Destination appeared during processing; source retained"
                    except (KeyError, TypeError, OSError, ValueError, RuntimeError) as exc:
                        status, error = "error", str(exc)
                        if not doi:
                            match_status = "error"
                row = dict.fromkeys(CSV_FIELDS, "")
                row.update({
                    "sha256": pdf.sha256, "doi": doi, "directory": str(current.parent),
                    "original_filename": pdf.path.name, "filename": current.name,
                    "original_path": str(pdf.path), "path": str(current),
                    "proposed_path": str(proposed), "status": status,
                    "match_status": match_status, "duplicate_of": duplicate_of,
                    "conflict_with": conflict_with, "conflict_with_sha256": conflict_sha,
                    "error": error,
                })
                writer.writerow(row)
                if rename or monotonic() - last_sync >= 1:
                    report.flush()
                    os.fsync(report.fileno())
                    last_sync = monotonic()
                counts[status] = counts.get(status, 0) + 1
                completed += 1
                if progress:
                    progress("Hashing and identifying PDFs", completed, len(pdfs))
        report.flush()
        os.fsync(report.fileno())
    return report_path, counts
