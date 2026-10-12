"""Resolve journal identities using reviewed aliases and validated ISSNs.

Name formatting alone never strips qualifiers, years, punctuation, or words.
Only reviewed aliases or matching serial identifiers establish equivalence.
"""

import json
import re
import unicodedata
from html import unescape

from sqlalchemy import select


def journal_display_name(value: str | None) -> str | None:
    if not value:
        return None
    name = value
    for _ in range(4):
        decoded = unescape(name)
        if decoded == name:
            break
        name = decoded
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name)).strip().upper() or None


def normalize_issn(value: str | None) -> str | None:
    digits = re.sub(r"[\s-]", "", value or "").upper()
    if not re.fullmatch(r"\d{7}[\dX]", digits):
        return None
    check = 10 if digits[-1] == "X" else int(digits[-1])
    if (sum(int(v) * weight for v, weight in zip(digits[:7], range(8, 1, -1))) + check) % 11:
        return None
    return digits[:4] + "-" + digits[4:]


def serial_identifiers(values) -> tuple[str, ...]:
    return tuple(sorted({issn for value in (values or ()) if (issn := normalize_issn(value))}))


def canonical_journal(connection, name: str | None, issns=(), doi=None) -> str | None:
    from netvault_server.server.models import JournalAlias, JournalDoiOverride, JournalIssn

    display = journal_display_name(name)
    identifiers = serial_identifiers(issns)
    if identifiers:
        matches = set(connection.scalars(
            select(JournalIssn.canonical_name).where(JournalIssn.issn.in_(identifiers))
        ))
        if len(matches) == 1:
            return matches.pop()
        # Conflicting or unregistered identifiers must never inherit a reviewed
        # identity just because their title happens to match an alias.
        return display
    if doi:
        override = connection.scalar(select(JournalDoiOverride.canonical_name).where(
            JournalDoiOverride.doi == doi
        ))
        if override:
            return override
    if display:
        alias = connection.scalar(
            select(JournalAlias.canonical_name).where(JournalAlias.alias_name == display)
        )
        return alias or display
    return None


def canonicalize_pdf(connection, pdf) -> None:
    from netvault_server.server.journal_filters import normalize_journal_name

    source = pdf.container_title
    if source is not None and (pdf.journal_raw_title is None or source != journal_display_name(source)):
        pdf.journal_raw_title = source
    try:
        identifiers = json.loads(pdf.journal_issns_json or "[]")
    except (TypeError, ValueError):
        identifiers = []
    pdf.container_title = canonical_journal(connection, source, identifiers, pdf.doi)
    pdf.journal_key = normalize_journal_name(pdf.container_title) or None
