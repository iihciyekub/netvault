import hashlib
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


DOI = "10.1234/netvault.test"
PDF_BYTES = b"%PDF-1.4\nDOI: 10.1234/netvault.test\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
OTHER_PDF_BYTES = b"%PDF-1.4\nDOI: 10.1234/other.test\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


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
            authors="Ada Lovelace; Alan Turing",
            container_title="NetVault Journal",
            publisher="NetVault Press",
            published_year=2026,
            resource_url=f"https://doi.org/{doi}",
        )

    monkeypatch.setattr(main_helpers, "fetch_crossref_metadata", fake_crossref_metadata)
    with TestClient(main.app) as test_client:
        yield test_client


def login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def upload(
    client: TestClient,
    headers: dict[str, str],
    name: str = "paper.pdf",
    content: bytes = PDF_BYTES,
    doi: str | None = None,
):
    return client.post(
        "/pdfs/upload",
        headers=headers,
        data={"doi": doi} if doi else None,
        files={"file": (name, content, "application/pdf")},
    )


def test_admin_can_create_user_and_user_can_login(client: TestClient) -> None:
    admin_headers = login(client, "admin", "admin-pass")
    response = client.post(
        "/admin/users",
        headers=admin_headers,
        json={"username": "alice", "password": "alice-pass", "role": "user"},
    )
    assert response.status_code == 200
    assert response.json()["username"] == "alice"

    user_headers = login(client, "alice", "alice-pass")
    me = client.get("/me", headers=user_headers)
    assert me.status_code == 200
    assert me.json()["role"] == "user"


def test_pdf_upload_list_search_download_and_dedup(client: TestClient, tmp_path: Path) -> None:
    admin_headers = login(client, "admin", "admin-pass")
    first = upload(client, admin_headers, "paper.pdf")
    assert first.status_code == 200
    assert first.json()["deduplicated"] is False
    assert first.json()["pdf"]["doi"] == DOI
    assert first.json()["pdf"]["doi_source"] == "pdf-content"
    assert first.json()["pdf"]["title"] == f"Title for {DOI}"
    assert first.json()["pdf"]["published_year"] == 2026
    assert first.json()["pdf"]["crossref_status"] == "ok"
    pdf_id = first.json()["pdf"]["id"]

    second = upload(client, admin_headers, "paper-copy.pdf")
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["pdf"]["id"] == pdf_id

    listed = client.get("/pdfs", headers=admin_headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    searched = client.get("/pdfs/search", headers=admin_headers, params={"q": "paper"})
    assert searched.status_code == 200
    assert searched.json()[0]["sha256"] == hashlib.sha256(PDF_BYTES).hexdigest()

    detail = client.get("/pdfs/by-doi", headers=admin_headers, params={"doi": DOI})
    assert detail.status_code == 200
    assert detail.json()["id"] == pdf_id
    assert "pdf-content" in detail.json()["doi_evidence"]

    downloaded = client.get("/pdfs/by-doi/download", headers=admin_headers, params={"doi": DOI})
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == hashlib.sha256(PDF_BYTES).hexdigest()

    stored_objects = list((tmp_path / "storage" / "objects").rglob("*.pdf"))
    assert len(stored_objects) == 1


def test_rejects_missing_doi_and_conflicting_doi(client: TestClient) -> None:
    admin_headers = login(client, "admin", "admin-pass")

    missing = upload(
        client,
        admin_headers,
        name="missing.pdf",
        content=b"%PDF-1.4\nno doi here\n%%EOF\n",
    )
    assert missing.status_code == 400

    first = upload(client, admin_headers, doi="https://doi.org/10.5555/manual")
    assert first.status_code == 200
    assert first.json()["pdf"]["doi"] == "10.5555/manual"

    conflict = upload(
        client,
        admin_headers,
        name="other.pdf",
        content=OTHER_PDF_BYTES,
        doi="10.5555/manual",
    )
    assert conflict.status_code == 409


def test_upload_uses_filename_doi_when_pdf_contains_reference_noise(client: TestClient) -> None:
    admin_headers = login(client, "admin", "admin-pass")
    noisy_pdf = (
        b"%PDF-1.4\n"
        b"References\n"
        b"https://doi.org/10.9999/reference.noise\n"
        b"%%EOF\n"
    )

    uploaded = upload(
        client,
        admin_headers,
        name="10.1016_j.chb.2015.03.041.pdf",
        content=noisy_pdf,
    )

    assert uploaded.status_code == 200
    assert uploaded.json()["pdf"]["doi"] == "10.1016/j.chb.2015.03.041"
    assert uploaded.json()["pdf"]["doi_source"] == "filename"


def test_rejects_unauthenticated_non_pdf_and_non_admin_delete(client: TestClient) -> None:
    assert client.get("/pdfs").status_code == 401

    admin_headers = login(client, "admin", "admin-pass")
    create = client.post(
        "/admin/users",
        headers=admin_headers,
        json={"username": "bob", "password": "bob-pass", "role": "user"},
    )
    assert create.status_code == 200
    bob_headers = login(client, "bob", "bob-pass")

    bad_upload = client.post(
        "/pdfs/upload",
        headers=bob_headers,
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert bad_upload.status_code == 400

    uploaded = upload(client, bob_headers)
    assert uploaded.status_code == 200
    pdf_id = uploaded.json()["pdf"]["id"]

    forbidden = client.delete(f"/admin/pdfs/{pdf_id}", headers=bob_headers)
    assert forbidden.status_code == 403

    deleted = client.delete(f"/admin/pdfs/{pdf_id}", headers=admin_headers)
    assert deleted.status_code == 200

    listed = client.get("/pdfs", headers=bob_headers)
    assert listed.status_code == 200
    assert listed.json() == []
