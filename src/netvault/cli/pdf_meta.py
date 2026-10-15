"""Local, offline PDF Document Info inspection and incremental updates.

Document Info is the only authoritative embedded metadata source. XMP is
deliberately excluded from reads, writes, and identity decisions.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import tempfile

from pypdf import PdfReader, PdfWriter

from netvault.doi import document_info_candidates, normalize_doi
from netvault.identity_shared import DOI_RE


def _open_reader(path: Path) -> PdfReader:
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise ValueError("Encrypted PDFs cannot be inspected or modified without decryption.")
    return reader


def read_pdf_metadata(path: Path) -> dict:
    """Read PDF Document Info without extracting page text or reading XMP."""
    reader = _open_reader(path)
    info = {
        str(key): str(value)
        for key, value in (reader.metadata or {}).items()
        if value is not None
    }
    candidates = [
        candidate
        for candidate in document_info_candidates(reader)
        if "/(issn)" not in candidate.doi.casefold()
    ]
    dois = sorted({candidate.doi for candidate in candidates})
    return {
        "file": str(path),
        "status": "conflict" if len(dois) > 1 else "ok" if dois else "no-doi",
        "doi": dois[0] if len(dois) == 1 else None,
        "dois": dois,
        "document_info": info,
        "doi_sources": [
            {"doi": item.doi, "location": item.detail}
            for item in candidates
        ],
    }


def _replace_document_info_dois(reader: PdfReader, doi: str) -> dict[str, str]:
    """Replace conflicting DOI values in Document Info text fields only on request."""
    changes = {}
    for key, value in (reader.metadata or {}).items():
        if not isinstance(value, str) or re.search(r"(?:journal|issue|volume).*doi", str(key), re.I):
            continue
        if str(key).casefold() == "/doi":
            if value != doi:
                changes[str(key)] = doi
            continue

        def replace_match(match: re.Match[str]) -> str:
            try:
                prior = normalize_doi(match.group(1))
            except ValueError:
                return match.group(0)
            return doi if prior != doi else match.group(0)

        updated = DOI_RE.sub(replace_match, value)
        if updated != value:
            changes[str(key)] = updated
    return changes


def write_pdf_metadata(
    path: Path,
    doi: str,
    *,
    output: Path | None = None,
    replace: bool = False,
    title: str | None = None,
    author: str | None = None,
    subject: str | None = None,
    keywords: str | None = None,
) -> dict:
    """Write only Document Info /DOI and optional Info fields.

    Uses a pypdf incremental update in a temporary file, verifies Document
    Info, then atomically replaces the chosen local output. XMP is untouched.
    """
    source = path.expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() != ".pdf":
        raise ValueError(f"PDF file not found: {path}")
    target = output.expanduser().resolve() if output is not None else source
    if target != source and target.exists():
        raise FileExistsError(f"Output already exists: {target}")

    normalized = normalize_doi(doi)
    before = read_pdf_metadata(source)
    if before["dois"] and set(before["dois"]) != {normalized} and not replace:
        raise ValueError(
            "The PDF Document Info has a different embedded DOI. "
            "Pass --replace to update conflicting Document Info DOI values."
        )
    optional = {
        key: value
        for key, value in {
            "/Title": title,
            "/Author": author,
            "/Subject": subject,
            "/Keywords": keywords,
        }.items()
        if value is not None
    }
    if (target == source and before["document_info"].get("/DOI") == normalized
            and not optional and before["status"] == "ok"):
        return {**before, "changed": False}

    reader = _open_reader(source)
    writer = PdfWriter(source, incremental=True)
    updates = _replace_document_info_dois(reader, normalized) if replace else {}
    # Explicit field values take precedence over automatic DOI correction.
    updates.update(optional)
    updates["/DOI"] = normalized
    writer.add_metadata(updates)

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.pdf-meta-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            writer.write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(stat.S_IMODE(source.stat().st_mode))
        after = read_pdf_metadata(temporary)
        if after["status"] != "ok" or after["doi"] != normalized:
            raise ValueError(
                "The rewritten PDF Document Info has conflicting DOI values; "
                "original file was not changed."
            )
        os.replace(temporary, target)
        return {**after, "file": str(target), "changed": True}
    finally:
        temporary.unlink(missing_ok=True)
