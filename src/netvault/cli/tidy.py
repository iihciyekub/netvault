"""Offline, per-directory PDF housekeeping. No server, no Crossref, no OCR."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from netvault.cli.identify import (
    LocalPdf, available_destination, doi_filename, file_signature,
    move_without_overwrite, scan_for_collections,
)
from netvault.cli.pdf_fingerprint import pdf_content_fingerprint
from netvault.cli.history import History
from netvault.cli.upload_fast import FastIdentity


@dataclass
class TidyPdf:
    path: Path
    signature: tuple[int, int, int, int] | None = None
    doi: str = ""
    source: str = ""
    action: str = "keep"
    keeper: Path | None = None
    sha256: str = ""
    content_sha256: str = ""
    error: str = ""


def _keeper_rank(item: TidyPdf) -> tuple[bool, bool, str]:
    # A dedicated /DOI beats a text-only assertion; never rank by file size
    # because aggressive compression can discard important image quality.
    try:
        well_named = bool(item.doi and item.path.name == doi_filename(item.doi))
    except ValueError:
        well_named = False
    return item.source != "pdf-metadata", not well_named, str(item.path)


def _record_hash(item: TidyPdf, hash_file: Callable[[Path], str]) -> str:
    if not item.sha256:
        if item.signature is None or file_signature(item.path) != item.signature:
            raise RuntimeError("PDF changed during scan")
        item.sha256 = hash_file(item.path)
        if file_signature(item.path) != item.signature:
            raise RuntimeError("PDF changed while hashing")
    return item.sha256


def _group_confirmed_duplicates(
    entries: list[TidyPdf], hash_file: Callable[[Path], str],
    *, compare_page_content: bool,
) -> None:
    if len(entries) < 2:
        return
    try:
        hashes: dict[str, list[TidyPdf]] = defaultdict(list)
        for item in entries:
            hashes[_record_hash(item, hash_file)].append(item)
        fingerprints: dict[str, str | None] = {}
        if compare_page_content and len(hashes) > 1:
            for sha, matching in hashes.items():
                representative = matching[0]
                fingerprint = pdf_content_fingerprint(representative.path)
                if file_signature(representative.path) != representative.signature:
                    raise RuntimeError("PDF changed during page comparison")
                fingerprints[sha] = fingerprint
        equivalent: dict[tuple[str, str], list[TidyPdf]] = defaultdict(list)
        for sha, matching in hashes.items():
            fp = fingerprints.get(sha)
            for item in matching:
                item.content_sha256 = fp or ""
                # A page fingerprint is never used across different DOI groups.
                key = ("pages", fp) if fp else ("bytes", sha)
                equivalent[key].append(item)
    except (OSError, ValueError, RuntimeError) as exc:
        # Never delete a subset of a DOI group after a failed comparison.
        for item in entries:
            item.action, item.error = "error", str(exc)
        return

    for duplicates in equivalent.values():
        keeper = min(duplicates, key=_keeper_rank)
        for item in duplicates:
            if item is not keeper:
                item.action, item.keeper = "duplicate", keeper.path
    # Two non-equivalent files with the same DOI may be different editions or
    # corrupted assertions. Do not conflate DOI identity with byte equality.
    if compare_page_content and len(equivalent) > 1:
        for item in entries:
            if item.action == "keep":
                item.action = "conflict"
                item.error = "Same DOI, differing or unverified PDF page content"


def plan_tidy(
    directory: Path,
    scan_dois: Callable[[list[Path]], list[tuple[FastIdentity | None, str | None]]],
    hash_file: Callable[[Path], str],
) -> list[TidyPdf]:
    """Build a read-only plan with one isolated namespace per PDF parent."""
    source = scan_for_collections(
        directory.expanduser().resolve(), None, collect_by_parent=True,
    )
    rows = [TidyPdf(item.path) for item in source]
    for row in rows:
        try:
            if row.path.is_symlink() or not row.path.is_file():
                raise ValueError("Symlink or non-regular PDF is not modified")
            row.signature = file_signature(row.path)
        except (OSError, ValueError) as exc:
            row.action, row.error = "error", str(exc)

    candidates = [row for row in rows if row.signature is not None]
    identities = scan_dois([row.path for row in candidates])
    if len(identities) != len(candidates):
        raise RuntimeError("DOI scan returned an incomplete result")
    for row, (identity, error) in zip(candidates, identities):
        try:
            if file_signature(row.path) != row.signature:
                raise RuntimeError("PDF changed during DOI extraction")
            if error:
                raise ValueError(error)
            if identity is None:
                row.action = "unidentified"
            else:
                if identity.source == "filename":
                    raise ValueError("Filename-only DOI evidence is not trusted by tidy")
                row.doi, row.source = identity.doi, identity.source
        except (OSError, ValueError, RuntimeError) as exc:
            row.action, row.error = "error", str(exc)

    # Per-directory DOI namespaces: a/ and b/ are never compared.
    by_doi: dict[tuple[Path, str], list[TidyPdf]] = defaultdict(list)
    unidentified_by_size: dict[tuple[Path, int], list[TidyPdf]] = defaultdict(list)
    for row in rows:
        if row.doi and row.action == "keep":
            by_doi[(row.path.parent, row.doi)].append(row)
        elif row.action == "unidentified" and row.signature is not None:
            unidentified_by_size[(row.path.parent, row.signature[2])].append(row)
    for group in by_doi.values():
        _group_confirmed_duplicates(group, hash_file, compare_page_content=True)
    for group in unidentified_by_size.values():
        if len(group) > 1:
            _group_confirmed_duplicates(group, hash_file, compare_page_content=False)
    return rows


def _unchanged(item: TidyPdf, hash_file: Callable[[Path], str]) -> None:
    if (item.signature is None or item.path.is_symlink()
            or file_signature(item.path) != item.signature):
        raise RuntimeError("File changed or became a symlink; retained")
    if item.sha256 and hash_file(item.path) != item.sha256:
        raise RuntimeError("PDF bytes changed; retained")


def apply_tidy(
    directory: Path, rows: list[TidyPdf], hash_file: Callable[[Path], str],
) -> tuple[Path, dict[str, int]]:
    """Remove proven duplicates then collect DOI-less files; journal actual changes."""
    journal = History(directory, "tidy")
    counts: dict[str, int] = defaultdict(int)
    reserved: set[str] = set()
    known = {row.path: row for row in rows}
    # Remove first: an unidentified keeper may move later, but not before its
    # duplicate was verified against the original path.
    ordered = sorted(rows, key=lambda row: (row.action != "duplicate", str(row.path)))
    for row in ordered:
        outcome = row.action
        try:
            if row.action == "duplicate":
                keeper = known.get(row.keeper)
                if keeper is None or keeper.action not in {"keep", "conflict", "unidentified"}:
                    raise RuntimeError("Cannot prove a surviving keeper")
                _unchanged(row, hash_file)
                _unchanged(keeper, hash_file)
                row.path.unlink()
                journal.write(
                    "delete", file=row.path, target=keeper.path,
                    doi=row.doi, reason="confirmed_duplicate",
                )
                outcome = "removed_duplicate"
            elif row.action == "unidentified":
                _unchanged(row, hash_file)
                destination_dir = row.path.parent / "unidentifys"
                if destination_dir.is_symlink():
                    raise RuntimeError("Unidentified destination is a symlink")
                if destination_dir.exists() and not destination_dir.is_dir():
                    raise RuntimeError("Unidentified destination is not a directory")
                sha = _record_hash(row, hash_file)
                destination = available_destination(
                    destination_dir, LocalPdf(row.path, sha256=sha), reserved,
                )
                reserved.add(str(destination).casefold())
                move_without_overwrite(row.path, destination, sha, hash_file)
                journal.write(
                    "move", file=row.path, target=destination,
                    reason="no_doi",
                )
                outcome = "moved_unidentified"
        except (OSError, ValueError, RuntimeError) as exc:
            outcome = "error"
            journal.write(
                "error", file=row.path, doi=row.doi, reason=str(exc),
                result="failed",
            )
        counts[outcome] += 1
    journal.write(
        "summary",
        result="partial" if counts["error"] else "ok",
        counts=dict(counts), scanned=len(rows),
    )
    return journal.path, dict(counts)


def preview_counts(rows: list[TidyPdf]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row.action] += 1
    return dict(counts)
