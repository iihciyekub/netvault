"""Cheap DOI-first preflight for ordinary uploads.

The preflight deliberately does not read or hash the entire PDF. A file that
cannot be identified cheaply falls through to the existing full resolver.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape
from pathlib import Path
import re
import subprocess
import unicodedata
from urllib.parse import quote

import requests

from netvault.doi import (
    extract_document_info_evidence,
    find_dois_in_text,
    find_filename_doi_from_name,
)


@dataclass(frozen=True)
class FastIdentity:
    doi: str
    source: str
    text: str = ""


def first_two_pages(path: Path, *, strict: bool = False) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "2", "-enc", "UTF-8", str(path), "-"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if strict:
            raise ValueError(f"Could not extract PDF pages 1-2: {exc}") from exc
        return ""
    if result.returncode != 0:
        if strict:
            raise ValueError("pdftotext could not read PDF pages 1-2")
        return ""
    return result.stdout


def _article_text(text: str) -> str:
    # DOI and title occurrences in a bibliography do not identify this article.
    return re.split(
        r"(?im)^\s*(?:references|bibliography|works cited)\s*$",
        text, maxsplit=1,
    )[0][:20000]


def probe_pdf(
    path: Path, *, allow_filename: bool = True, strict: bool = False,
) -> FastIdentity | None:
    """Read Document Info first, then PDF pages 1-2 with pdftotext.

    Regular upload allows filename fallback and lets the legacy resolver handle
    ambiguity. Offline tidy uses strict page parsing and never trusts filenames.
    """
    embedded = extract_document_info_evidence(path)
    if embedded is not None:
        if embedded.status == "conflict":
            raise ValueError(embedded.reason or "Conflicting PDF Document Info DOIs")
        if embedded.doi:
            return FastIdentity(embedded.doi, "pdf-metadata")

    text = first_two_pages(path, strict=strict)
    if not text.strip():
        return None
    article = _article_text(text)
    dois = set(find_dois_in_text(article))
    first_page_dois = set(find_dois_in_text(_article_text(text.split("\f", 1)[0])))
    if len(dois) == 1:
        return FastIdentity(next(iter(dois)), "pdf-content", text)
    if len(first_page_dois) == 1:
        return FastIdentity(next(iter(first_page_dois)), "pdf-content", text)
    if dois:
        if strict:
            raise ValueError("Ambiguous DOI candidates on PDF pages 1-2")
        return None  # never guess among bibliography/citation DOIs
    if allow_filename:
        filename = find_filename_doi_from_name(path.name)
        if filename is not None:
            return FastIdentity(filename.doi, "filename", text)
    return None


def _plain_ascii_letters(value: str) -> str:
    # CSL titles may contain inline markup and PDF text may contain ligatures.
    clean = re.sub(r"<[^>]*>", "", unescape(value))
    clean = unicodedata.normalize("NFKD", clean).casefold()
    return re.sub(r"[^a-z]", "", clean)


def title_matches(title: str | None, pdf_text: str) -> bool:
    if not isinstance(title, str):
        return False
    normalized_title = _plain_ascii_letters(title)
    if len(normalized_title) < 20:
        return False
    return normalized_title in _plain_ascii_letters(_article_text(pdf_text))


def confirms_existing(identity: FastIdentity, server_pdf: dict) -> bool:
    if identity.source == "pdf-metadata":
        return True
    text = identity.text
    first_page = _article_text(text.split("\f", 1)[0])
    return (
        identity.doi in set(find_dois_in_text(first_page))
        and title_matches(server_pdf.get("title"), first_page)
    )


def fetch_csl_title(doi: str) -> str | None:
    """Lookup only the needed title; the server keeps final authority."""
    headers = {
        "Accept": "application/vnd.citationstyles.csl+json",
        "User-Agent": "NetVault CLI DOI upload preflight (https://iiaide.com/nv)",
    }
    try:
        response = requests.get(
            f"https://doi.org/{quote(doi, safe='/')}",
            headers=headers, timeout=(3.05, 10),
        )
        if response.ok:
            try:
                title = response.json().get("title")
                if isinstance(title, str) and title.strip():
                    return title
            except (ValueError, TypeError, AttributeError):
                pass
        # A malformed CSL response should not disable Crossref fallback.
        response = requests.get(
            f"https://api.crossref.org/works/{quote(doi, safe='')}",
            headers={"User-Agent": headers["User-Agent"]}, timeout=(3.05, 10),
        )
        if response.ok:
            titles = response.json()["message"].get("title") or []
            if titles and isinstance(titles[0], str):
                return titles[0]
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


def check_new_titles(identities: dict[Path, FastIdentity]) -> dict[Path, bool]:
    """Fetch each unique DOI once; cap parallel provider requests at three."""
    if not identities:
        return {}
    dois = sorted({identity.doi for identity in identities.values()})
    with ThreadPoolExecutor(max_workers=min(3, len(dois))) as pool:
        titles = dict(zip(dois, pool.map(fetch_csl_title, dois)))
    return {
        path: title_matches(titles.get(identity.doi), identity.text or first_two_pages(path))
        for path, identity in identities.items()
    }
