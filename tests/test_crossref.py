from types import SimpleNamespace

import pytest


def test_metadata_requests_share_three_polite_slots(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    from netvault_server.server import crossref

    monkeypatch.setattr(crossref, "get_settings", lambda: SimpleNamespace(crossref_mailto="test@example.edu"))
    monkeypatch.setattr(crossref, "_next_request", 0.0)
    barrier, lock = Barrier(3, timeout=5), Lock()
    active = peak = 0

    def request(_index):
        nonlocal active, peak
        with crossref._request_slot():
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait()
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(request, range(6)))
    assert peak == 3
    assert crossref._active_requests == 0


def test_public_metadata_requests_are_serialized(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from netvault_server.server import crossref

    monkeypatch.setattr(crossref, "get_settings", lambda: SimpleNamespace(crossref_mailto=None))
    monkeypatch.setattr(crossref, "_next_request", 0.0)
    first, second, release = Event(), Event(), Event()

    def request(index):
        with crossref._request_slot():
            (first if index == 0 else second).set()
            if index == 0:
                assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(request, 0)
        try:
            assert first.wait(5)
            b = pool.submit(request, 1)
            assert not second.wait(0.3)
        finally:
            release.set()
        a.result(timeout=5)
        b.result(timeout=5)
    assert second.is_set()


def test_crossref_session_retries_transient_failures(monkeypatch) -> None:
    from netvault_server.server import crossref

    crossref._local.session = None
    session = crossref._session()
    retry = session.get_adapter("https://").max_retries
    assert retry.total == 3
    assert 429 in retry.status_forcelist
    assert 503 in retry.status_forcelist
    assert retry.respect_retry_after_header is True

    class FakeResponse:
        status_code = 404
        ok = False

    class FakeSession:
        def get(self, url, **kwargs):
            assert url.endswith("10.1234%2Fmissing")
            assert kwargs["params"] == {"mailto": "research@example.edu"}
            assert kwargs["timeout"] == (3.05, 10)
            return FakeResponse()

    monkeypatch.setattr(
        crossref,
        "get_settings",
        lambda: SimpleNamespace(
            crossref_mailto="research@example.edu",
            crossref_user_agent="NetVault test",
        ),
    )
    monkeypatch.setattr(crossref, "_session", lambda: FakeSession())
    metadata = crossref.fetch_crossref_metadata("10.1234/missing")
    assert metadata.status == "not_found"
    assert metadata.fetched_at is not None


def test_crossref_returns_the_registry_canonical_doi(monkeypatch) -> None:
    from netvault_server.server import crossref

    class FakeResponse:
        status_code = 200
        ok = True

        @staticmethod
        def json():
            return {
                "message": {
                    "DOI": "10.25300/MISQ/2025/18946",
                    "title": ["AI-Augmented Content Validation"],
                }
            }

    class FakeSession:
        @staticmethod
        def get(*args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        crossref,
        "get_settings",
        lambda: SimpleNamespace(crossref_mailto=None, crossref_user_agent="NetVault test"),
    )
    monkeypatch.setattr(crossref, "_session", lambda: FakeSession())

    metadata = crossref.fetch_crossref_metadata("10.25300/misq/2025/18946")

    assert metadata.status == "ok"
    assert metadata.canonical_doi == "10.25300/MISQ/2025/18946"


def test_doi_metadata_falls_back_when_crossref_has_no_record(monkeypatch) -> None:
    from netvault_server.server import crossref

    monkeypatch.setattr(
        crossref,
        "fetch_crossref_metadata",
        lambda _doi: crossref.CrossrefMetadata(status="not_found"),
    )
    monkeypatch.setattr(
        crossref,
        "fetch_doi_org_metadata",
        lambda doi: crossref.CrossrefMetadata(
            status="ok",
            canonical_doi=doi,
            title="Repository Object",
            provider="doi.org",
        ),
    )

    metadata = crossref.fetch_doi_metadata("10.9999/example")
    assert metadata.status == "ok"
    assert metadata.provider == "doi.org"
    assert metadata.title == "Repository Object"


def test_crossref_title_search_preserves_verification_fields(monkeypatch):
    from netvault_server.server import crossref

    class FakeSession:
        def get(self, url, **kwargs):
            assert url == 'https://api.crossref.org/works'
            assert kwargs['params']['query.title'] == 'Article title'
            assert kwargs['params']['rows'] == 5
            return SimpleNamespace(ok=True, json=lambda: {'message': {'items': [{
                'DOI': '10.1234/article', 'title': ['Article title'], 'type': 'journal-article',
                'container-title': ['Journal'], 'author': [{'family': 'Corbin'}],
                'published': {'date-parts': [[1990]]},
            }]}})

    monkeypatch.setattr(crossref, '_session', lambda: FakeSession())
    result = crossref.search_crossref_metadata('Article title')[0]
    assert result.canonical_doi == '10.1234/article'
    assert result.work_type == 'journal-article'
    assert result.container_title == 'Journal'
    assert result.authors == 'Corbin'
    assert result.published_year == 1990


@pytest.mark.parametrize("registry_status,resolver_status,expected", [
    ("not_found", "not_found", "semantic-scholar"),
    ("not_found", "ok", "doi.org"),
    ("not_found", "unavailable", "doi.org"),
    ("unavailable", "not_found", "crossref"),
    ("ok", "not_found", "crossref"),
])
def test_secondary_index_is_used_only_after_two_explicit_missing_records(
    monkeypatch, registry_status, resolver_status, expected,
):
    from netvault_server.server import crossref
    calls = []
    monkeypatch.setattr(crossref, "fetch_crossref_metadata", lambda doi: crossref.CrossrefMetadata(
        status=registry_status, provider="crossref"))
    monkeypatch.setattr(crossref, "fetch_doi_org_metadata", lambda doi: crossref.CrossrefMetadata(
        status=resolver_status, provider="doi.org"))

    def secondary(doi):
        calls.append(doi)
        return crossref.CrossrefMetadata(status="ok", provider="semantic-scholar")

    monkeypatch.setattr(crossref, "fetch_semantic_scholar_metadata", secondary)
    assert crossref.fetch_doi_metadata("10.1287/mnsc.2018.3045").provider == expected
    assert bool(calls) == (expected == "semantic-scholar")


@pytest.mark.parametrize("returned_doi,expected", [
    ("10.1287/MNSC.2018.3045", "ok"), ("10.1287/mnsc.2018.other", "not_found"),
])
def test_secondary_metadata_requires_the_same_doi(monkeypatch, returned_doi, expected):
    from netvault_server.server import crossref
    monkeypatch.setattr(crossref, "_limited_get", lambda url, **kwargs: SimpleNamespace(
        status_code=200, ok=True, json=lambda: {
            "externalIds": {"DOI": returned_doi}, "title": "Corporate Investment Efficiency",
            "authors": [{"name": "Yiwei Dou"}], "year": 2019, "venue": "Management Science",
            "url": "https://www.semanticscholar.org/paper/example", "publicationTypes": ["JournalArticle"],
        },
    ))
    metadata = crossref.fetch_semantic_scholar_metadata("10.1287/mnsc.2018.3045")
    assert metadata.status == expected
    assert metadata.provider == "semantic-scholar"
    if expected == "ok":
        assert metadata.canonical_doi == "10.1287/mnsc.2018.3045"
        assert metadata.authors == "Yiwei Dou"
        assert metadata.published_year == 2019
        assert metadata.work_type == "journal-article"


@pytest.mark.parametrize("status,expected", [(404, "not_found"), (429, "unavailable"), (503, "unavailable")])
def test_secondary_metadata_preserves_provider_failures(monkeypatch, status, expected):
    from netvault_server.server import crossref
    monkeypatch.setattr(crossref, "_limited_get", lambda *args, **kwargs: SimpleNamespace(status_code=status, ok=False))
    assert crossref.fetch_semantic_scholar_metadata("10.1287/example").status == expected
