"""Local, offline PDF Document Info inspection and incremental updates.

Document Info is the only authoritative embedded DOI source. XMP is never
parsed for DOI identity or modified; automatic writes compare its raw stream
only to verify preservation.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
from pathlib import Path
import re
import stat
import tempfile

from pypdf import PdfReader, PdfWriter

from netvault.doi import document_info_candidates, normalize_doi
from netvault.identity_shared import DOI_RE
from netvault.cli.cache import cache_lock


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


def _verified_doi_receipt(verification: dict) -> tuple[dict, str, str]:
    """Accept only independent Crossref/content verification of these exact bytes."""
    if not isinstance(verification, dict):
        raise ValueError("An independent DOI verification receipt is required.")
    receipt = dict(verification)
    required = {
        "policy_version": 1,
        "decision": "verified",
        "metadata_status": "ok",
        "metadata_provider": "crossref",
    }
    for key, expected in required.items():
        if receipt.get(key) != expected or (
            key == "policy_version" and type(receipt.get(key)) is not int
        ):
            raise ValueError(f"DOI verification receipt has invalid {key}.")
    for key in ("writeback_allowed", "printed_doi_confirmed", "author_match", "year_match"):
        if receipt.get(key) is not True:
            raise ValueError(f"DOI verification receipt must confirm {key}.")
    score = receipt.get("title_match_score")
    if (isinstance(score, bool) or not isinstance(score, (int, float))
            or not math.isfinite(score) or not 0.98 <= score <= 1):
        raise ValueError("DOI verification receipt does not confirm the PDF title.")
    # These optional diagnostics must never contradict the mandatory evidence.
    for key in ("source", "evidence_source", "verification_source", "status"):
        value = str(receipt.get(key, "")).casefold()
        if any(term in value for term in (
            "embedded", "csv", "pending", "metadata", "trusted", "manual", "cache", "filename"
        )):
            raise ValueError("Trusted or pending DOI identification cannot authorize PDF writeback.")
    if receipt.get("trusted_embedded") or receipt.get("csv_import"):
        raise ValueError("Trusted DOI identification cannot authorize PDF writeback.")
    doi = receipt.get("doi")
    canonical = receipt.get("canonical_doi")
    if not isinstance(doi, str) or not isinstance(canonical, str):
        raise ValueError("DOI verification receipt is missing its canonical DOI.")
    normalized = normalize_doi(doi)
    if doi != normalized or canonical != normalized:
        raise ValueError("DOI verification receipt contains a different canonical DOI.")
    digest = receipt.get("input_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ValueError("DOI verification receipt is missing the complete input SHA-256.")
    return receipt, normalized, digest.lower()


def _file_identity(details: os.stat_result) -> tuple:
    return (
        details.st_dev, details.st_ino, details.st_size,
        details.st_mtime_ns, details.st_ctime_ns, details.st_mode, details.st_nlink,
    )


def _automatic_pdf_path(path: Path) -> Path:
    # Resolving first would hide a symlink and could silently replace its target.
    source = Path(os.path.abspath(path.expanduser()))
    if source.suffix.casefold() != ".pdf":
        raise ValueError(f"PDF file not found: {path}")
    for component in (source, *source.parents):
        if component.is_symlink():
            raise ValueError("Automatic PDF writeback does not follow symbolic links.")
    return source


def _pdf_snapshot(source: Path) -> tuple[bytes, os.stat_result, str]:
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("Automatic PDF writeback requires a regular file with one hard link.")
        data = handle.read()
        after = os.fstat(handle.fileno())
    if (_file_identity(before) != _file_identity(after)
            or _file_identity(before) != _file_identity(source.lstat())):
        raise ValueError("PDF changed while its verification snapshot was being read.")
    return data, before, hashlib.sha256(data).hexdigest()


def _assert_pdf_unchanged(source: Path, original: os.stat_result, digest: str) -> None:
    _automatic_pdf_path(source)
    _, current, current_digest = _pdf_snapshot(source)
    if _file_identity(current) != _file_identity(original) or current_digest != digest:
        raise ValueError("PDF changed after DOI verification; the original was not replaced.")


def _strict_pdf_reader(snapshot: bytes) -> PdfReader:
    reader = PdfReader(io.BytesIO(snapshot), strict=True)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDFs are excluded from automatic DOI writeback.")
    root = reader.trailer["/Root"]
    if "/Perms" in root:
        raise ValueError("Signed or certified PDFs are excluded from automatic DOI writeback.")
    form = root.get("/AcroForm")
    if form is not None:
        form = form.get_object()
        visited = set()

        def inspect_field(reference, inherited_type=None):
            field = reference.get_object()
            if id(field) in visited:
                return
            visited.add(id(field))
            kind = field.get("/FT", inherited_type)
            if kind == "/Sig" or "/ByteRange" in field:
                raise ValueError("Signature fields are excluded from automatic DOI writeback.")
            for child in field.get("/Kids", []):
                inspect_field(child, kind)

        if int(form.get("/SigFlags", 0)):
            raise ValueError("Signature fields are excluded from automatic DOI writeback.")
        for field in form.get("/Fields", []):
            inspect_field(field)
    # Force strict parsing of the page tree and Info before any filesystem mutation.
    if not list(reader.pages):
        raise ValueError("A PDF without pages is excluded from automatic DOI writeback.")
    dict(reader.metadata or {})
    return reader


def _safe_pdf_reader(snapshot: bytes) -> PdfReader:
    try:
        return _strict_pdf_reader(snapshot)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Damaged or unsupported PDFs are excluded from automatic DOI writeback.") from exc


def _automatic_info_dois(reader: PdfReader) -> set[str]:
    for key, value in (reader.metadata or {}).items():
        if str(key).casefold() != "/doi":
            continue
        if not isinstance(value, str):
            raise ValueError("The PDF /DOI field is non-text; automatic writeback is not allowed.")
        if value.strip():
            try:
                normalize_doi(value)
            except ValueError as exc:
                raise ValueError("The PDF /DOI field is nonempty but invalid or ambiguous.") from exc
    dois = {
        candidate.doi for candidate in document_info_candidates(reader)
        if "/(issn)" not in candidate.doi.casefold()
    }
    if len(dois) > 1:
        raise ValueError("Conflicting Document Info DOI values require manual review.")
    return dois


def check_pdf_writeback_safety(path: Path) -> None:
    """Preflight automatic writeback; the committing writer always checks again."""
    source = _automatic_pdf_path(path)
    snapshot, _, _ = _pdf_snapshot(source)
    _automatic_info_dois(_safe_pdf_reader(snapshot))


def _set_pdf_mode(descriptor: int, path: Path, mode: int) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, mode)
    else:
        path.chmod(mode)


def _sync_pdf_directory(directory: Path) -> None:
    # Windows supports file fsync and atomic replace, but not this POSIX
    # directory-fsync operation. Do not prevent all Windows writebacks.
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verified_pdf_backup(source: Path, snapshot: bytes, digest: str, mode: int) -> Path:
    backup = source.with_name(f"{source.name}.nv-doi-{digest}.bak")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(backup, flags, 0o600)
    except FileExistsError:
        # Never clobber a backup, follow a backup symlink, or reuse partial data.
        descriptor = os.open(backup, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            details = os.fstat(handle.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise ValueError("The existing DOI backup is not a regular file with one hard link.")
            if (hashlib.sha256(handle.read()).hexdigest() != digest
                    or _file_identity(details) != _file_identity(os.fstat(handle.fileno()))
                    or _file_identity(details) != _file_identity(backup.lstat())):
                raise ValueError("The existing DOI backup does not match the original PDF.")
            os.fsync(handle.fileno())
        _sync_pdf_directory(source.parent)
        return backup
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(snapshot)
            handle.flush()
            _set_pdf_mode(handle.fileno(), backup, mode)
            os.fsync(handle.fileno())
        _sync_pdf_directory(source.parent)
        return backup
    except BaseException:
        backup.unlink(missing_ok=True)
        raise


def write_verified_pdf_doi(path: Path, verification: dict) -> dict:
    """Safely add a missing Info DOI after independent verification of exact bytes.

    The receipt is accepted only from the server verification workflow. This is
    intentionally stricter than the manual writer: it never overwrites DOI
    evidence, follows links, decrypts, or invalidates a PDF signature.
    """
    receipt, doi, expected_digest = _verified_doi_receipt(verification)
    source = _automatic_pdf_path(path)
    with cache_lock(source.with_name(f".{source.name}.nv-doi")):
        snapshot, original, original_digest = _pdf_snapshot(source)
        if original_digest != expected_digest:
            raise ValueError("PDF bytes do not match the independently verified input SHA-256.")
        reader = _safe_pdf_reader(snapshot)
        existing = _automatic_info_dois(reader)
        if existing and existing != {doi}:
            raise ValueError("The PDF already has a different DOI; automatic writeback is not allowed.")
        result = {
            "changed": False,
            "doi": doi,
            "original_sha256": original_digest,
            "sha256": original_digest,
            "backup": None,
            "file": str(source),
            "verification": receipt,
        }
        if existing:
            _assert_pdf_unchanged(source, original, original_digest)
            return result

        writer = PdfWriter(io.BytesIO(snapshot), incremental=True)
        writer.add_metadata({"/DOI": doi})
        descriptor, name = tempfile.mkstemp(prefix=f".{source.name}.nv-doi-", dir=source.parent)
        temporary = Path(name)
        backup = None
        committed = False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                writer.write(handle)
                handle.flush()
                _set_pdf_mode(handle.fileno(), temporary, stat.S_IMODE(original.st_mode))
                os.fsync(handle.fileno())
            updated = temporary.read_bytes()
            if len(updated) <= len(snapshot) or not updated.startswith(snapshot):
                raise ValueError("Automatic DOI writeback did not preserve the original PDF bytes.")
            after = _safe_pdf_reader(updated)
            if _automatic_info_dois(after) != {doi} or len(after.pages) != len(reader.pages):
                raise ValueError("Automatic DOI writeback failed DOI or page integrity verification.")
            if after.metadata.get("/DOI") != doi:
                raise ValueError("Automatic DOI writeback failed to write its complete DOI.")
            for key, value in (reader.metadata or {}).items():
                if str(key) != "/DOI" and after.metadata.get(key) != value:
                    raise ValueError("Automatic DOI writeback changed unrelated Document Info.")
            old_xmp = reader.trailer["/Root"].get("/Metadata")
            new_xmp = after.trailer["/Root"].get("/Metadata")
            if ((old_xmp is None) != (new_xmp is None)
                    or (old_xmp is not None and
                        old_xmp.get_object().get_data() != new_xmp.get_object().get_data())):
                raise ValueError("Automatic DOI writeback changed XMP metadata.")
            _assert_pdf_unchanged(source, original, original_digest)
            backup = _verified_pdf_backup(
                source, snapshot, original_digest, stat.S_IMODE(original.st_mode)
            )
            _assert_pdf_unchanged(source, original, original_digest)
            result.update(
                changed=True, sha256=hashlib.sha256(updated).hexdigest(), backup=str(backup)
            )
            # The backup and temporary contents are durable before committing.
            os.replace(temporary, source)
            committed = True
            try:
                _sync_pdf_directory(source.parent)
            except OSError as exc:
                # Replacement has committed. Report its true state instead of
                # raising an error that misleadingly suggests it was unchanged.
                result["durability_warning"] = f"Directory sync after replacement failed: {exc}"
            return result
        except Exception as exc:
            # A verified backup can survive a later source-change/replace
            # failure. Preserve its location for the per-file failure report.
            if backup is not None:
                exc.pdf_writeback_backup = str(backup)
            raise
        finally:
            if not committed:
                temporary.unlink(missing_ok=True)
