"""Simple trusted file-to-DOI mappings for nv upload --csv.

Matching is path-based; a modified or recompressed PDF must not lose its DOI.
The older richer cli-downloads.csv index still handles renamed legacy files
as a fallback, without imposing SHA/size requirements on simple mappings.
"""
from __future__ import annotations

import csv
from pathlib import Path, PurePosixPath

from netvault.doi import normalize_doi


class TrustedDoiCsv:
    def __init__(self, path: Path):
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("DOI CSV exceeds 64 MiB")
        self.entries: dict[str, str] = {}
        self.has_legacy_columns = False
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = {str(value).strip().lower() for value in (reader.fieldnames or [])}
            if "doi" not in columns or not ({"file", "filename"} & columns):
                raise ValueError("DOI CSV requires file,doi columns")
            self.has_legacy_columns = bool(columns & {"bytes", "size", "sha256"})
            for number, raw in enumerate(reader, start=2):
                if number > 100001:
                    raise ValueError("DOI CSV exceeds 100,000 rows")
                row = {str(k).strip().lower(): str(v or "").strip()
                       for k, v in raw.items() if k is not None}
                if row.get("status") and row["status"].lower() != "downloaded":
                    continue
                name = (row.get("file") or row.get("filename") or "").replace("\\", "/")
                entry = PurePosixPath(name)
                if (not name or entry.is_absolute() or ".." in entry.parts
                        or entry.suffix.lower() != ".pdf"):
                    raise ValueError(f"Invalid DOI CSV filename at row {number}: {name}")
                doi = normalize_doi(row.get("doi", ""))
                key = str(entry)
                if key in self.entries and self.entries[key] != doi:
                    raise ValueError(f"Conflicting DOI CSV entries for {key}")
                self.entries[key] = doi

    def resolve(self, path: Path, roots: list[Path], all_paths: list[Path]) -> str | None:
        candidates = set()
        for root in roots:
            try:
                relative = path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                continue
            if relative in self.entries:
                candidates.add(self.entries[relative])
        if path.name in self.entries:
            same_name = sum(other.name == path.name for other in all_paths)
            if same_name == 1:
                candidates.add(self.entries[path.name])
            elif not candidates:
                raise ValueError(f"Ambiguous filename {path.name} in DOI CSV; use relative paths")
            # A precise relative-path mapping wins over ambiguous basenames.
        if len(candidates) > 1:
            raise ValueError(f"Conflicting DOI CSV paths for {path}")
        return next(iter(candidates)) if candidates else None
