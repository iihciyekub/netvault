from types import SimpleNamespace


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
