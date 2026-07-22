from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from netvault_server.server.config import get_settings

_local = threading.local()


@dataclass(frozen=True)
class CrossrefMetadata:
    status: str
    canonical_doi: str | None = None
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


def _session() -> requests.Session:
    session = getattr(_local, "session", None)
    if session is None:
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        session.mount("https://", adapter)
        _local.session = session
    return session


def fetch_crossref_metadata(doi: str) -> CrossrefMetadata:
    settings = get_settings()
    url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
    params = {"mailto": settings.crossref_mailto} if settings.crossref_mailto else None
    headers = {"User-Agent": settings.crossref_user_agent}

    fetched_at = datetime.now(timezone.utc)
    try:
        response = _session().get(url, params=params, headers=headers, timeout=(3.05, 10))
    except requests.RequestException:
        return CrossrefMetadata(status="unavailable", fetched_at=fetched_at)

    if response.status_code == 404:
        return CrossrefMetadata(status="not_found", fetched_at=fetched_at)
    if not response.ok:
        return CrossrefMetadata(status="unavailable", fetched_at=fetched_at)

    try:
        message = response.json()["message"]
    except (KeyError, TypeError, ValueError):
        return CrossrefMetadata(status="unavailable", fetched_at=fetched_at)

    return CrossrefMetadata(
        status="ok",
        canonical_doi=message.get("DOI"),
        title=_first(message.get("title")),
        authors=_authors(message),
        container_title=_first(message.get("container-title")),
        publisher=message.get("publisher"),
        published_year=_published_year(message),
        resource_url=message.get("URL"),
        fetched_at=fetched_at,
    )
