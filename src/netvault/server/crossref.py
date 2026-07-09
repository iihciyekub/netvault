from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

from netvault.server.config import get_settings


@dataclass(frozen=True)
class CrossrefMetadata:
    status: str
    title: str | None = None
    authors: str | None = None
    container_title: str | None = None
    publisher: str | None = None
    published_year: int | None = None
    resource_url: str | None = None
    fetched_at: datetime | None = None


def _first(values: list[Any] | None) -> Any | None:
    if not values:
        return None
    return values[0]


def _published_year(message: dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        date_parts = message.get(key, {}).get("date-parts")
        first_part = _first(date_parts)
        if first_part and isinstance(first_part, list) and first_part:
            year = first_part[0]
            return int(year) if isinstance(year, int) else None
    return None


def _authors(message: dict[str, Any]) -> str | None:
    authors = []
    for author in message.get("author") or []:
        given = author.get("given")
        family = author.get("family")
        name = " ".join(part for part in (given, family) if part)
        if name:
            authors.append(name)
    return "; ".join(authors) if authors else None


def fetch_crossref_metadata(doi: str) -> CrossrefMetadata:
    settings = get_settings()
    url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
    params = {"mailto": settings.crossref_mailto} if settings.crossref_mailto else None
    headers = {"User-Agent": settings.crossref_user_agent}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
    except requests.RequestException:
        return CrossrefMetadata(status="unavailable")

    if response.status_code == 404:
        return CrossrefMetadata(status="not_found")
    if not response.ok:
        return CrossrefMetadata(status="unavailable")

    try:
        message = response.json()["message"]
    except (KeyError, TypeError, ValueError):
        return CrossrefMetadata(status="unavailable")

    return CrossrefMetadata(
        status="ok",
        title=_first(message.get("title")),
        authors=_authors(message),
        container_title=_first(message.get("container-title")),
        publisher=message.get("publisher"),
        published_year=_published_year(message),
        resource_url=message.get("URL"),
        fetched_at=datetime.now(timezone.utc),
    )
