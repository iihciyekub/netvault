"""Identify local PDFs by server hashes and keep an audit of file operations."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import csv
import errno
import os
from pathlib import Path
import re
import shutil

from netvault.doi import normalize_doi


CSV_FIELDS = (
    "sha256", "doi", "directory", "original_filename", "filename",
    "original_path", "path", "proposed_path", "status", "match_status",
    "duplicate_of", "error",
)


@dataclass
class LocalPdf:
    path: Path
    sha256: str = ""
    signature: tuple[int, int, int, int] | None = None
    doi: str = ""
    error: str = ""


def file_signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def doi_filename(doi: str) -> str:
    # Keep the DOI readable, while avoiding path separators and Windows restrictions.
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", normalize_doi(doi)).rstrip(" .")
    filename = f"{stem}.pdf"
    if len(filename.encode("utf-8")) > 255:
        raise ValueError("DOI filename exceeds 255 UTF-8 bytes")
    return filename


def scan_pdfs(directory: Path, duplicates_to: Path | None) -> list[LocalPdf]:
    pdfs = []

    def scan_error(error: OSError) -> None:
        raise error

    for root, directories, filenames in os.walk(directory, onerror=scan_error):
        parent = Path(root)
        directories[:] = sorted(
            name for name in directories
            if not (parent / name).is_symlink()
            and (duplicates_to is None or (parent / name).resolve() != duplicates_to)
        )
        for name in sorted(filenames):
            if Path(name).suffix.lower() == ".pdf":
                pdfs.append(LocalPdf(parent / name))
    return sorted(pdfs, key=lambda pdf: str(pdf.path))


def move_without_overwrite(source: Path, destination: Path, sha256: str,
                           hash_file: Callable[[Path], str]) -> None:
    """Use exclusive creation, including when moving onto another filesystem."""
    if source.is_symlink() or hash_file(source) != sha256:
        raise RuntimeError("File changed since hashing; left in place")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError as exc:
        if exc.errno not in {errno.EXDEV, errno.EPERM, errno.ENOTSUP}:
            raise
        # 'xb' cannot replace even a dangling symlink or a concurrent destination.
        with destination.open("xb") as output:
            try:
                with source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
                if hash_file(destination) != sha256 or hash_file(source) != sha256:
                    raise RuntimeError("File changed while copying; source retained")
                shutil.copystat(source, destination)
            except BaseException:
                destination.unlink()
                raise
    try:
        source.unlink()
    except OSError:
        # The source still exists: roll back the newly created destination.
        destination.unlink()
        raise


def open_report(directory: Path, requested: Path | None):
    if requested is not None:
        if requested.suffix.lower() != ".csv":
            raise ValueError("--csv must name a .csv file")
        return requested, requested.open("x", encoding="utf-8-sig", newline="")
    index = 1
    while True:
        suffix = "" if index == 1 else f"-{index}"
        path = directory / f"netvault-index{suffix}.csv"
        try:
            return path, path.open("x", encoding="utf-8-sig", newline="")
        except FileExistsError:
            index += 1


def identify_pdfs(
    directory: Path,
    *,
    rename: bool,
    duplicates_to: Path | None,
    csv_path: Path | None,
    hash_file: Callable[[Path], str],
    lookup: Callable[[Iterable[str]], dict[str, dict]],
    progress: Callable[[str, int, int], None] | None = None,
) -> tuple[Path, dict[str, int]]:
    directory = directory.expanduser().resolve()
    duplicates_to = duplicates_to.expanduser().resolve() if duplicates_to else None
    csv_path = csv_path.expanduser().absolute() if csv_path else None
    if duplicates_to is not None:
        if directory == duplicates_to or directory.is_relative_to(duplicates_to):
            raise ValueError("Duplicate destination must not be the scan directory or its ancestor")
        if duplicates_to.exists() and not duplicates_to.is_dir():
            raise ValueError("Duplicate destination must be a directory")
    pdfs = scan_pdfs(directory, duplicates_to)
    for index, pdf in enumerate(pdfs, 1):
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
        if progress:
            progress("Hashing PDFs", index, len(pdfs))

    # Finish all network requests before touching PDFs. A network failure is not 'not found'.
    existing = lookup(pdf.sha256 for pdf in pdfs if not pdf.error)
    groups: dict[str, list[LocalPdf]] = {}
    for pdf in pdfs:
        if pdf.error:
            continue
        record = existing.get(pdf.sha256)
        if record is not None:
            try:
                pdf.doi = normalize_doi(record["doi"])
                doi_filename(pdf.doi)
            except (KeyError, TypeError, ValueError) as exc:
                pdf.error = f"Invalid DOI from server: {exc}"
                continue
        groups.setdefault(pdf.sha256, []).append(pdf)

    keepers = {}
    for sha256, group in groups.items():
        # Prefer an already correctly named copy, otherwise use sorted absolute paths.
        keepers[sha256] = next(
            (pdf for pdf in group if pdf.doi and pdf.path.name == doi_filename(pdf.doi)),
            group[0],
        )

    report_path, report = open_report(directory, csv_path)
    counts: dict[str, int] = {}
    actual_paths = {id(pdf): pdf.path for pdf in pdfs}
    reserved: set[str] = set()
    # Process keepers first so duplicate_of can contain their final paths.
    ordered = sorted(pdfs, key=lambda pdf: keepers.get(pdf.sha256) is not pdf)
    with report:
        writer = csv.DictWriter(report, fieldnames=CSV_FIELDS)
        writer.writeheader()
        report.flush()
        os.fsync(report.fileno())
        for index, pdf in enumerate(ordered, 1):
            keeper = keepers.get(pdf.sha256)
            duplicate = keeper is not None and keeper is not pdf
            current = pdf.path
            proposed = current
            duplicate_of = str(actual_paths[id(keeper)]) if duplicate else ""
            error = pdf.error
            status = "error" if error else "not_found"
            if not error:
                try:
                    if duplicate and duplicates_to is not None:
                        # Use original basenames; add hash/number only for destination conflicts.
                        proposed = duplicates_to / current.name
                        counter = 1
                        while (os.path.lexists(proposed)
                               or str(proposed).casefold() in reserved):
                            ending = f"__{pdf.sha256[:8]}" + (f"-{counter}" if counter > 1 else "")
                            budget = 255 - len(f"{ending}.pdf".encode("utf-8"))
                            stem = current.stem.encode("utf-8")[:budget].decode(
                                "utf-8", errors="ignore",
                            )
                            proposed = duplicates_to / f"{stem}{ending}.pdf"
                            counter += 1
                        status = "would_move_duplicate"
                    elif pdf.doi:
                        proposed = current.with_name(doi_filename(pdf.doi))
                        status = "already_named" if proposed == current else "would_rename"

                    if proposed != current:
                        if (os.path.lexists(proposed)
                                or str(proposed).casefold() in reserved):
                            status = "conflict"
                            error = "Destination already exists or is reserved by another PDF"
                        else:
                            reserved.add(str(proposed).casefold())
                            if rename:
                                if file_signature(current) != pdf.signature:
                                    raise RuntimeError("File changed since hashing; left in place")
                                if duplicate and duplicates_to is not None:
                                    keeper_path = actual_paths[id(keeper)]
                                    if (keeper_path.is_symlink()
                                            or file_signature(keeper_path) != keeper.signature):
                                        raise RuntimeError("Retained copy changed; duplicate left in place")
                                move_without_overwrite(current, proposed, pdf.sha256, hash_file)
                                current = proposed
                                actual_paths[id(pdf)] = current
                                pdf.signature = file_signature(current)
                                status = "moved_duplicate" if duplicate and duplicates_to else "renamed"
                except FileExistsError:
                    status, error = "conflict", "Destination appeared during processing; source retained"
                except (OSError, ValueError, RuntimeError) as exc:
                    status, error = "error", str(exc)
            writer.writerow({
                "sha256": pdf.sha256, "doi": pdf.doi,
                "directory": str(current.parent), "original_filename": pdf.path.name,
                "filename": current.name, "original_path": str(pdf.path),
                "path": str(current), "proposed_path": str(proposed), "status": status,
                "match_status": "error" if pdf.error else ("matched" if pdf.doi else "not_found"),
                "duplicate_of": duplicate_of, "error": error,
            })
            report.flush()
            os.fsync(report.fileno())
            counts[status] = counts.get(status, 0) + 1
            if progress:
                progress("Processing PDFs", index, len(ordered))
    return report_path, counts
