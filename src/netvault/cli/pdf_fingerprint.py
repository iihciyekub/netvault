"""Conservative page fingerprints for PDFs that differ in download metadata."""

import hashlib
from pathlib import Path
import re

from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject, BooleanObject, ByteStringObject, ContentStream, DictionaryObject,
    IndirectObject, NullObject, StreamObject,
)


WILEY_NOTICE = re.compile(
    r"\s*\d{8},\s*\d{4},\s*\d+,\s*Downloaded from "
    r"https://onlinelibrary\.wiley\.com/doi/\S+ by [^\r\n]+, "
    r"Wiley Online Library on \[\d{2}/\d{2}/\d{4}\]\. See the Terms and Conditions "
    r"\(https://onlinelibrary\.wiley\.com/terms-and-conditions\) on Wiley Online Library "
    r"for rules of use; OA articles are governed by the applicable Creative Commons License\s*"
)
TEXT_OPERATORS = {
    b"BT", b"ET", b"Tf", b"Tm", b"Td", b"TD", b"T*", b"Tj", b"TJ", b"'", b'"',
    b"Tc", b"Tw", b"Tz", b"TL", b"Ts", b"Tr", b"q", b"Q", b"cm",
    b"g", b"G", b"rg", b"RG", b"k", b"K",
}


def is_wiley_download_notice(stream: StreamObject, reader: PdfReader) -> bool:
    # Only a standalone text Form can be ignored. Images, drawing commands, and
    # arbitrary text mentioning Wiley remain part of the content fingerprint.
    if stream.get("/Subtype") != "/Form":
        return False
    if b"Downloaded from https://onlinelibrary.wiley.com/doi/" not in stream.get_data():
        return False
    text = []
    for operands, operator in ContentStream(stream, reader).operations:
        if operator not in TEXT_OPERATORS:
            return False
        if operator in {b"Tj", b"'", b'"'}:
            text.append(str(operands[-1]))
        elif operator == b"TJ":
            text.extend(str(value) for value in operands[0] if isinstance(value, str))
    return WILEY_NOTICE.fullmatch("".join(text)) is not None


def pdf_content_fingerprint(path: Path) -> str | None:
    """Hash page objects/resources, ignoring metadata and verified Wiley notices.

    Object numbers and stream compression do not affect the result. Fonts,
    images, page order/geometry, annotations, and optional-content settings do.
    Unsupported or unreadable PDFs simply receive no content fingerprint.
    """
    try:
        with path.open("rb") as source:
            reader = PdfReader(source)
            if reader.is_encrypted or not reader.pages:
                return None
            root = reader.trailer["/Root"]
            names = root.get("/Names")
            if isinstance(names, IndirectObject):
                names = names.get_object()
            if root.get("/AcroForm") or (names and names.get("/EmbeddedFiles")):
                return None
            memo: dict[int, bytes] = {}
            active: set[int] = set()

            def digest(value) -> bytes:
                if isinstance(value, IndirectObject):
                    value = value.get_object()
                if isinstance(value, (DictionaryObject, ArrayObject)):
                    identity = id(value)
                    if identity in active:
                        raise ValueError("Cyclic page resources cannot be fingerprinted safely")
                    if identity in memo:
                        return memo[identity]
                    active.add(identity)
                    hasher = hashlib.sha256()
                    if isinstance(value, StreamObject):
                        if is_wiley_download_notice(value, reader):
                            result = hashlib.sha256(b"verified-wiley-download-notice-v1").digest()
                            memo[identity] = result
                            active.remove(identity)
                            return result
                        hasher.update(b"stream\0")
                        hasher.update(hashlib.sha256(value.get_data()).digest())
                    if isinstance(value, DictionaryObject):
                        hasher.update(b"dictionary\0")
                        ignored = {"/Metadata"}
                        if value.get("/Type") == "/Page":
                            ignored.add("/Parent")
                        if isinstance(value, StreamObject):
                            ignored.update({"/Length", "/Filter", "/DecodeParms"})
                        for key in sorted(value):
                            if key not in ignored:
                                hasher.update(digest(key))
                                hasher.update(digest(value.raw_get(key)))
                    else:
                        hasher.update(b"array\0")
                        for item in value:
                            hasher.update(digest(item))
                    result = hasher.digest()
                    active.remove(identity)
                    memo[identity] = result
                    return result
                if isinstance(value, NullObject):
                    payload = b"null"
                elif isinstance(value, BooleanObject):
                    payload = b"bool:" + str(value.value).encode()
                elif isinstance(value, ByteStringObject):
                    payload = b"bytes:" + bytes(value)
                else:
                    payload = type(value).__name__.encode() + b":" + str(value).encode("utf-8")
                return hashlib.sha256(payload).digest()

            hasher = hashlib.sha256(b"netvault-pdf-page-content-v1\0")
            for page in reader.pages:
                # Avoid treating empty or malformed blank PDFs as duplicate literature.
                contents = page.get_contents()
                if contents is None or not contents.get_data().strip():
                    return None
                hasher.update(digest(page))
            if root.get("/OCProperties"):
                hasher.update(digest(root["/OCProperties"]))
            return hasher.hexdigest()
    except Exception:
        # Content comparison is optional; byte hashes and server lookup remain usable.
        return None
