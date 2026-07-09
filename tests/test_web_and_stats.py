import hashlib
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PDF_BYTES = b"%PDF-1.4\nDOI: 10.1234/web.test\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("NETVAULT_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("NETVAULT_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("NETVAULT_SECRET_KEY", "test-secret")
    monkeypatch.setenv("NETVAULT_BOOTSTRAP_ADMIN", "admin")
    monkeypatch.setenv("NETVAULT_BOOTSTRAP_ADMIN_PASSWORD", "admin-pass")

    for module_name in list(sys.modules):
        if module_name.startswith("netvault_server.server"):
            del sys.modules[module_name]

    main = importlib.import_module("netvault_server.server.main")
    crossref = importlib.import_module("netvault_server.server.crossref")
    main_helpers = importlib.import_module("netvault_server.server.main_helpers")

    def fake_crossref_metadata(doi: str):
        return crossref.CrossrefMetadata(
            status="ok",
            title=f"Title for {doi}",
            authors="Grace Hopper",
            container_title="Web Journal",
            publisher="NetVault Press",
            published_year=2026,
            resource_url=f"https://doi.org/{doi}",
        )

    monkeypatch.setattr(main_helpers, "fetch_crossref_metadata", fake_crossref_metadata)
    with TestClient(main.app) as test_client:
        yield test_client


def login_headers(client: TestClient) -> dict[str, str]:
    response = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def web_login(client: TestClient) -> None:
    get_login = client.get("/web/login")
    csrf = get_login.cookies["netvault_csrf"]
    response = client.post(
        "/web/login",
        data={"username": "admin", "password": "admin-pass", "csrf_token": csrf},
    )
    assert response.status_code == 200
    assert response.history
    assert response.history[0].status_code == 303


def test_stats_api_requires_auth_and_groups_data(client: TestClient) -> None:
    assert client.get("/stats/summary").status_code == 401
    headers = login_headers(client)
    uploaded = client.post(
        "/pdfs/upload",
        headers=headers,
        files={"file": ("web.pdf", PDF_BYTES, "application/pdf")},
    )
    assert uploaded.status_code == 200

    summary = client.get("/stats/summary", headers=headers)
    assert summary.status_code == 200
    assert summary.json()["active_pdfs"] == 1
    by_year = client.get("/stats/by-year", headers=headers)
    assert by_year.json() == [{"year": 2026, "count": 1}]
    by_journal = client.get("/stats/by-journal", headers=headers)
    assert by_journal.json()[0] == {"journal": "Web Journal", "count": 1}
    journal_year = client.get("/stats/by-journal-year", headers=headers)
    assert journal_year.status_code == 200
    assert journal_year.json()["max_count"] == 1
    assert journal_year.json()["rows"][0]["cells"][0] == {"year": 2026, "count": 1, "level": 4}


def test_web_login_dashboard_upload_download_and_csrf(client: TestClient) -> None:
    login_page = client.get("/web/login")
    csrf = login_page.cookies["netvault_csrf"]
    bad_upload = client.post(
        "/web/upload",
        files={"files": ("web.pdf", PDF_BYTES, "application/pdf")},
    )
    assert bad_upload.status_code == 403

    response = client.post(
        "/web/login",
        data={"username": "admin", "password": "admin-pass", "csrf_token": csrf},
    )
    assert response.status_code == 200
    dashboard = client.get("/web")
    assert dashboard.status_code == 200
    assert "No journal-year data." in dashboard.text
    assert "By Year" not in dashboard.text
    assert "Top Journals" not in dashboard.text

    csrf = client.cookies["netvault_csrf"]
    upload = client.post(
        "/web/upload",
        data={"csrf_token": csrf},
        files={"files": ("web.pdf", PDF_BYTES, "application/pdf")},
    )
    assert upload.status_code == 200
    assert "10.1234/web.test" in upload.text
    assert "Drop PDF files" in upload.text
    dashboard = client.get("/web")
    assert dashboard.status_code == 200
    assert "journal-heatmap" in dashboard.text
    assert "heat-cell level-4" in dashboard.text

    pdfs_without_query = client.get("/web/pdfs")
    assert pdfs_without_query.status_code == 200
    assert "Search by DOI or metadata." in pdfs_without_query.text
    assert "10.1234/web.test" not in pdfs_without_query.text

    pdfs_with_query = client.get("/web/pdfs", params={"q": "10.1234/web.test"})
    assert pdfs_with_query.status_code == 200
    assert "10.1234/web.test" in pdfs_with_query.text

    lookup = client.post(
        "/web/download",
        data={"csrf_token": client.cookies["netvault_csrf"], "doi_text": "https://doi.org/10.1234/web.test"},
    )
    assert lookup.status_code == 200
    assert "Download" in lookup.text

    downloaded = client.get("/web/pdfs/download", params={"doi": "10.1234/web.test"})
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == hashlib.sha256(PDF_BYTES).hexdigest()


def test_root_package_is_cli_only() -> None:
    import tomllib

    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(pyproject["project"]["dependencies"])
    scripts = pyproject["project"]["scripts"]
    assert "fastapi" not in dependencies
    assert "sqlalchemy" not in dependencies
    assert "psycopg" not in dependencies
    assert "netvault-admin" not in scripts
