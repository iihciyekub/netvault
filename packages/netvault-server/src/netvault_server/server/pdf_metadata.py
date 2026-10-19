"""Incremental DOI stamp for incoming valid PDFs, before content-addressed storage.

Never rewrite an already-published object. Caller passes the upload's staged
temporary path and computes its final content hash after this operation.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject

from netvault_server.server.doi import document_info_candidates, normalize_doi


def stamp_uploaded_doi(path: Path, doi: str, *, trusted: bool = False) -> bool:
    """Write canonical /DOI; return whether bytes changed.

    Legacy non-renderable test fixtures are left to the upload's existing
    validation rules. For a readable PDF, failure is reported, never ignored.
    Signed/encrypted PDFs are not modified.
    """
    canonical = normalize_doi(doi)
    try:
        reader = PdfReader(str(path), strict=False)
    except Exception:
        # Preserve compatibility with the server's historical %PDF-/%%EOF
        # intake policy, which accepts some non-renderable legacy fixtures.
        return False
    if reader.is_encrypted:
        raise ValueError("Cannot write a DOI into an encrypted PDF")
    try:
        if not reader.pages:
            return False
        root = reader.trailer["/Root"]
    except Exception:
        return False
    # A canonical DOI is already stored; never rewrite the bytes just to restamp it.
    # This no-op is safe even when the PDF contains signature or append-only flags.
    info = reader.metadata or {}
    old_dois = {value.doi for value in document_info_candidates(reader)}
    if old_dois - {canonical} and not trusted:
        raise ValueError("PDF Document Info DOI conflicts with verified article DOI")
    aliases = [str(key) for key in info if str(key).casefold() == "/doi" and str(key) != "/DOI"]
    if (old_dois == {canonical} and info.get("/DOI") == canonical and not aliases):
        return False

    if "/Perms" in root:
        raise ValueError("Cannot modify a signed or certified PDF")
    form = root.get("/AcroForm")
    if form is not None:
        form = form.get_object()
        if int(form.get("/SigFlags", 0)):
            raise ValueError("Cannot modify a signed PDF")
        if any(field.get_object().get("/FT") == "/Sig"
               for field in form.get("/Fields", [])):
            raise ValueError("Cannot modify a signed PDF")

    updates = {"/DOI": canonical}
    if trusted:
        # The explicit user/CSV mapping is authoritative. Clear contradictory
        # DOI strings from common bibliographic metadata before verification.
        from netvault_server.server.doi import DOI_RE
        for key in ("/Subject", "/Keywords"):
            value = info.get(key)
            if isinstance(value, str):
                updated = DOI_RE.sub(canonical, value)
                if updated != value:
                    updates[key] = updated
    writer = PdfWriter(str(path), incremental=True)
    writer.add_metadata(updates)
    for alias in aliases:
        writer._info.get_object().pop(NameObject(alias), None)

    fd, name = tempfile.mkstemp(prefix="upload-doi-", suffix=".tmp", dir=path.parent)
    output = Path(name)
    try:
        with os.fdopen(fd, "wb") as destination:
            writer.write(destination)
            destination.flush()
            os.fsync(destination.fileno())
        check = PdfReader(str(output), strict=False)
        written_dois = {value.doi for value in document_info_candidates(check)}
        if written_dois != {canonical} or (check.metadata or {}).get("/DOI") != canonical:
            raise ValueError("PDF DOI metadata verification failed")
        os.replace(output, path)
        return True
    finally:
        output.unlink(missing_ok=True)
