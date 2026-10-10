from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
import threading
from contextlib import contextmanager
from time import monotonic

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from netvault_server.server.config import get_settings
from netvault_server.server.doi import normalize_doi

_local = threading.local()
_request_condition = threading.Condition()
_active_requests = 0
_next_request = 0.0


@contextmanager
def _request_slot(*, search: bool = False, secondary: bool = False):
    """Share metadata concurrency/rate limits across upload and web threads in this server process.

    Public requests use one slot; configured polite requests use three.
    Title searches use the stricter list-endpoint rate. The HTTP adapter keeps
    its existing Retry-After handling while a retry holds its concurrency slot.
    """
    global _active_requests, _next_request
    polite = bool(get_settings().crossref_mailto)
    limit = 1 if secondary else (3 if polite else 1)
    rate = 1 if secondary else ((3 if polite else 1) if search else (10 if polite else 5))
    with _request_condition:
        while True:
            delay = _next_request - monotonic()
            if _active_requests < limit and delay <= 0:
                _active_requests += 1
                _next_request = monotonic() + 1 / rate
                break
            _request_condition.wait(timeout=max(delay, 0.001) if _active_requests < limit else None)
    try:
        yield
    finally:
        with _request_condition:
            _active_requests -= 1
            _request_condition.notify_all()


def _limited_get(url: str, **kwargs):
    with _request_slot(search=url.endswith("/works"), secondary=url.startswith("https://api.semanticscholar.org/")):
        return _session().get(url, **kwargs)


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
    provider: str = "crossref"
    work_type: str | None = None


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
        response = _limited_get(url, params=params, headers=headers, timeout=(3.05, 10))
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
        provider="crossref",
        work_type=message.get("type"),
    )


def _csl_authors(message: dict[str, Any]) -> str | None:
    authors = []
    for author in message.get("author") or []:
        given = author.get("given")
        family = author.get("family")
        literal = author.get("literal")
        name = " ".join(part for part in (given, family) if part) or literal
        if name:
            authors.append(name)
    return "; ".join(authors) if authors else None


def _csl_year(message: dict[str, Any]) -> int | None:
    for key in ("issued", "published", "created"):
        date_parts = message.get(key, {}).get("date-parts")
        first = _first(date_parts)
        if first and isinstance(first, list) and first:
            year = first[0]
            return int(year) if isinstance(year, int) else None
    return None


def fetch_doi_org_metadata(doi: str) -> CrossrefMetadata:
    settings = get_settings()
    fetched_at = datetime.now(timezone.utc)
    url = f"https://doi.org/{quote(doi, safe='/')}"
    headers = {
        "Accept": "application/vnd.citationstyles.csl+json",
        "User-Agent": settings.crossref_user_agent,
    }
    try:
        response = _limited_get(url, headers=headers, timeout=(3.05, 10), allow_redirects=True)
    except requests.RequestException:
        return CrossrefMetadata(status="unavailable", fetched_at=fetched_at, provider="doi.org")

    if response.status_code == 404:
        return CrossrefMetadata(status="not_found", fetched_at=fetched_at, provider="doi.org")
    if not response.ok:
        return CrossrefMetadata(status="unavailable", fetched_at=fetched_at, provider="doi.org")

    try:
        message = response.json()
    except ValueError:
        return CrossrefMetadata(status="unavailable", fetched_at=fetched_at, provider="doi.org")

    title = message.get("title")
    if isinstance(title, list):
        title = _first(title)
    container_title = message.get("container-title")
    if isinstance(container_title, list):
        container_title = _first(container_title)

    return CrossrefMetadata(
        status="ok",
        canonical_doi=message.get("DOI") or doi,
        title=title if isinstance(title, str) else None,
        authors=_csl_authors(message),
        container_title=container_title if isinstance(container_title, str) else None,
        publisher=message.get("publisher") if isinstance(message.get("publisher"), str) else None,
        published_year=_csl_year(message),
        resource_url=message.get("URL") if isinstance(message.get("URL"), str) else f"https://doi.org/{doi}",
        fetched_at=fetched_at,
        provider="doi.org",
        work_type=message.get("type"),
    )


def fetch_semantic_scholar_metadata(doi: str) -> CrossrefMetadata:
    """Recover a DOI-indexed paper only after both registry providers report 404."""
    fetched_at = datetime.now(timezone.utc)
    missing = CrossrefMetadata(status="not_found", fetched_at=fetched_at, provider="semantic-scholar")
    unavailable = CrossrefMetadata(status="unavailable", fetched_at=fetched_at, provider="semantic-scholar")
    try:
        response = _limited_get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi, safe='')}",
            params={"fields": "title,authors,venue,year,externalIds,url,publicationTypes"},
            headers={"User-Agent": get_settings().crossref_user_agent}, timeout=(3.05, 10),
        )
        if response.status_code == 404:
            return missing
        if not response.ok:
            return unavailable
        message = response.json()
        # Never substitute a related paper or infer a DOI from a search result.
        canonical = normalize_doi(message["externalIds"]["DOI"])
        if canonical != normalize_doi(doi):
            return missing
        title = message.get("title")
        authors = "; ".join(a["name"] for a in message.get("authors", []) if isinstance(a.get("name"), str))
        year = message.get("year")
        if not isinstance(title, str) or not title.strip() or not authors or type(year) is not int:
            return missing
    except requests.RequestException:
        return unavailable
    except (KeyError, TypeError, ValueError, AttributeError):
        return unavailable
    return CrossrefMetadata(
        status="ok", canonical_doi=canonical, title=title, authors=authors,
        container_title=message.get("venue") or None, published_year=year,
        resource_url=message.get("url"), fetched_at=fetched_at, provider="semantic-scholar",
        work_type="journal-article" if "JournalArticle" in (message.get("publicationTypes") or []) else None,
    )


def fetch_doi_metadata(doi: str) -> CrossrefMetadata:
    metadata = fetch_crossref_metadata(doi)
    if metadata.status != "not_found":
        return metadata
    fallback = fetch_doi_org_metadata(doi)
    if fallback.status != "not_found":
        return fallback
    return fetch_semantic_scholar_metadata(doi)


def search_crossref_metadata(title: str) -> list[CrossrefMetadata]:
    settings = get_settings()
    params = {"query.title": title[:500], "rows": 5}
    if settings.crossref_mailto:
        params["mailto"] = settings.crossref_mailto
    fetched_at = datetime.now(timezone.utc)
    try:
        response = _limited_get(
            "https://api.crossref.org/works", params=params,
            headers={"User-Agent": settings.crossref_user_agent}, timeout=(3.05, 10),
        )
        if not response.ok:
            return [CrossrefMetadata(status="unavailable", fetched_at=fetched_at)]
        items = response.json()["message"]["items"]
        return [CrossrefMetadata(
            status="ok", canonical_doi=item.get("DOI"), title=_first(item.get("title")),
            authors=_authors(item), container_title=_first(item.get("container-title")),
            publisher=item.get("publisher"), published_year=_published_year(item),
            resource_url=item.get("URL"), fetched_at=fetched_at, work_type=item.get("type"),
        ) for item in items]
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return [CrossrefMetadata(status="unavailable", fetched_at=fetched_at)]
