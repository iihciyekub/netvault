import hashlib
import importlib
import io
import sys
import zipfile
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
    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    with database.SessionLocal() as db:
        admin = db.query(models.User).filter_by(username="admin").one()
        db.add(
            models.Pdf(
                doi="10.1234/unknown.test",
                doi_source="manual",
                sha256="0" * 64,
                original_name="unknown.pdf",
                title="Unknown venue",
                authors="[]",
                container_title="(unknown)",
                publisher=None,
                published_year=2026,
                crossref_status="ok",
                size=12,
                storage_path="objects/00/unknown.pdf",
                uploaded_by_id=admin.id,
            )
        )
        db.add(
            models.Pdf(
                doi="10.1234/amp.test",
                doi_source="manual",
                sha256="1" * 64,
                original_name="amp.pdf",
                title="Encoded venue",
                authors="[]",
                container_title="Fish &amp; Chips Journal",
                publisher=None,
                published_year=2026,
                crossref_status="ok",
                size=12,
                storage_path="objects/11/amp.pdf",
                uploaded_by_id=admin.id,
            )
        )
        db.add(
            models.Pdf(
                doi="10.1234/utd.test",
                doi_source="manual",
                sha256="2" * 64,
                original_name="utd.pdf",
                title="UTD venue",
                authors="[]",
                container_title="The Journal of Finance",
                publisher=None,
                published_year=2024,
                crossref_status="ok",
                size=100,
                storage_path="objects/22/utd.pdf",
                uploaded_by_id=admin.id,
            )
        )
        db.add(
            models.Pdf(
                doi="10.1234/abs1.test",
                doi_source="manual",
                sha256="3" * 64,
                original_name="abs1.pdf",
                title="ABS one venue",
                authors="[]",
                container_title="International Journal of Intercultural Relations",
                publisher=None,
                published_year=2025,
                crossref_status="ok",
                size=200,
                storage_path="objects/33/abs1.pdf",
                uploaded_by_id=admin.id,
            )
        )
        db.add(
            models.Pdf(
                doi="10.1234/nonmatch.test",
                doi_source="manual",
                sha256="4" * 64,
                original_name="nonmatch.pdf",
                title="Non matching venue",
                authors="[]",
                container_title="Neighborhood PDF Review",
                publisher=None,
                published_year=2024,
                crossref_status="ok",
                size=300,
                storage_path="objects/44/nonmatch.pdf",
                uploaded_by_id=admin.id,
            )
        )
        db.commit()
    by_journal = client.get("/stats/by-journal", headers=headers)
    assert {"journal": "Web Journal", "count": 1} in by_journal.json()
    assert {"journal": "Fish & Chips Journal", "count": 1} in by_journal.json()
    journal_year = client.get("/stats/by-journal-year", headers=headers)
    assert journal_year.status_code == 200
    assert journal_year.json()["max_count"] == 1
    assert journal_year.json()["years"] == sorted(journal_year.json()["years"], reverse=True)
    assert "Fish & Chips Journal" in {row["journal"] for row in journal_year.json()["rows"]}
    fish_row = next(row for row in journal_year.json()["rows"] if row["journal"] == "Fish & Chips Journal")
    assert next(cell for cell in fish_row["cells"] if cell["year"] == 2026) == {"year": 2026, "count": 1, "level": 4}

    utd_summary = client.get("/stats/summary", params={"filter": "utd24"}, headers=headers)
    assert utd_summary.status_code == 200
    assert utd_summary.json()["active_pdfs"] == 1
    assert utd_summary.json()["total_size"] == 100
    utd_heatmap = client.get("/stats/by-journal-year", params={"filter": "utd24"}, headers=headers)
    assert [row["journal"] for row in utd_heatmap.json()["rows"]] == ["The Journal of Finance"]

    abs4star_summary = client.get("/stats/summary", params={"filter": "4*"}, headers=headers)
    assert abs4star_summary.status_code == 200
    assert abs4star_summary.json()["active_pdfs"] == 1
    abs1_summary = client.get("/stats/summary", params={"filter": "abs1"}, headers=headers)
    assert abs1_summary.status_code == 200
    assert abs1_summary.json()["active_pdfs"] == 1
    assert abs1_summary.json()["total_size"] == 200


def test_dashboard_stats_cache_can_be_invalidated(client: TestClient) -> None:
    headers = login_headers(client)
    uploaded = client.post(
        "/pdfs/upload",
        headers=headers,
        files={"file": ("web.pdf", PDF_BYTES, "application/pdf")},
    )
    assert uploaded.status_code == 200

    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    stats = importlib.import_module("netvault_server.server.stats")
    with database.SessionLocal() as db:
        cached = stats.get_dashboard_stats(db)
        assert cached["summary"]["active_pdfs"] == 1
        admin = db.query(models.User).filter_by(username="admin").one()
        db.add(
            models.Pdf(
                doi="10.1234/cache.test",
                doi_source="manual",
                sha256="2" * 64,
                original_name="cache.pdf",
                title="Cache test",
                authors="[]",
                container_title="Cache Journal",
                publisher=None,
                published_year=2025,
                crossref_status="ok",
                size=12,
                storage_path="objects/22/cache.pdf",
                uploaded_by_id=admin.id,
            )
        )
        db.commit()
        assert stats.get_dashboard_stats(db)["summary"]["active_pdfs"] == 1
        stats.invalidate_stats_cache()
        assert stats.get_dashboard_stats(db)["summary"]["active_pdfs"] == 2


def test_dashboard_can_include_a_pinned_journal_outside_top_twenty(client: TestClient) -> None:
    web_login(client)
    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    stats = importlib.import_module("netvault_server.server.stats")
    with database.SessionLocal() as db:
        admin = db.query(models.User).filter_by(username="admin").one()
        for index in range(21):
            journal = f"Journal {index:02d}"
            for paper in range(21 - index):
                db.add(
                    models.Pdf(
                        doi=f"10.7777/{index}.{paper}",
                        doi_source="manual",
                        sha256=f"{index * 100 + paper + 1:064x}",
                        original_name=f"{index}-{paper}.pdf",
                        title=f"Paper {index}-{paper}",
                        authors="Test Author",
                        container_title=journal,
                        publisher="Test",
                        published_year=2026,
                        crossref_status="ok",
                        size=10,
                        storage_path=f"objects/aa/{index}-{paper}.pdf",
                        uploaded_by_id=admin.id,
                    )
                )
        db.commit()
    stats.invalidate_stats_cache()

    normal = client.get("/web")
    pinned = client.get("/web", params={"pin": "Journal 20"})
    assert 'data-journal-name="Journal 20"' not in normal.text
    assert 'data-journal-name="Journal 20"' in pinned.text
    assert '<option value="Journal 20"></option>' in normal.text


def test_web_login_dashboard_upload_download_and_csrf(client: TestClient) -> None:
    login_page = client.get("/web/login")
    assert '<h1 class="sr-only">Log in to NetVault</h1>' in login_page.text
    assert "vendor/fontawesome/css/fontawesome.min.css" in login_page.text
    assert "vendor/fontawesome/css/solid.min.css" in login_page.text
    assert "app.js" in login_page.text
    assert "clipboard.js" not in login_page.text
    assert "upload.js" not in login_page.text
    assert "heatmap-tooltip.js" not in login_page.text
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
    assert '<h1 class="sr-only">NetVault dashboard</h1>' in dashboard.text
    assert "No journal-year data." in dashboard.text
    assert "UTD24" in dashboard.text
    assert "ABS 4*" in dashboard.text
    assert "summary-count" in dashboard.text
    assert ">0</span>" in dashboard.text
    assert "0 PDFs" not in dashboard.text
    assert "1974-" not in dashboard.text
    assert "Current filter summary" in dashboard.text
    assert "<span>Users</span>" not in dashboard.text
    assert "Admin" in dashboard.text
    assert "Info" in dashboard.text
    assert "fa-chart-column" in dashboard.text
    assert "fa-magnifying-glass" in dashboard.text
    assert "fa-cloud-arrow-up" in dashboard.text
    assert "fa-cloud-arrow-down" in dashboard.text
    assert "fa-quote-left" in dashboard.text
    assert "fa-box-archive" not in dashboard.text
    assert "data-utility-toggle" in dashboard.text
    assert 'id="utility-nav"' in dashboard.text
    assert '<h2 id="journal-year-title" class="sr-only">Journal by Year</h2>' in dashboard.text
    assert "Publication density across matching journals." not in dashboard.text
    assert "By Year" not in dashboard.text
    assert "Top Journals" not in dashboard.text
    assert "Upload PDFs" not in dashboard.text
    assert "Download by DOI" not in dashboard.text

    csrf = client.cookies["netvault_csrf"]
    upload_page = client.get("/web/upload")
    assert upload_page.status_code == 200
    assert '<h1 class="sr-only">Upload PDFs</h1>' in upload_page.text
    assert "raw.githubusercontent.com/iihciyekub/netvault/main/scripts/install.sh" not in upload_page.text
    assert "data-upload-form" in upload_page.text
    assert "data-precheck-url=\"/web/pdfs/exists\"" in upload_page.text
    assert "upload-progress" in upload_page.text
    download_page = client.get("/web/download")
    assert download_page.status_code == 200
    assert '<h1 class="sr-only">Download PDFs</h1>' in download_page.text
    assert "nv download --file ./dois.txt --to ./downloads" not in download_page.text
    assert "app.js" in download_page.text
    assert "clipboard.js" not in download_page.text
    cli_page = client.get("/web/cli")
    assert cli_page.status_code == 200
    assert '<h1 class="sr-only">NetVault command-line interface</h1>' in cli_page.text
    assert "Install / Update" in cli_page.text
    assert "nv update" in cli_page.text
    assert "nv login https://iiaide.com/nv --username polyu" in cli_page.text
    assert "--password" not in cli_page.text
    assert "nv download --file ./dois.txt --to ./downloads" in cli_page.text
    assert "nv upload ./papers" in cli_page.text
    assert "data-copy" in cli_page.text
    assert 'class="command-timeline"' in cli_page.text
    assert cli_page.text.count('class="timeline-node"') == 4
    assert "Step 01" in cli_page.text
    assert "Step 04" in cli_page.text
    assert "fa-cloud-arrow-down" in cli_page.text
    assert "data-copy-label" in cli_page.text
    assert "syntax-command" in cli_page.text
    assert "syntax-option" in cli_page.text
    assert "syntax-url" in cli_page.text
    assert "syntax-path" in cli_page.text
    info_page = client.get("/web/info")
    assert info_page.status_code == 200
    assert '<h1 class="sr-only">About NetVault</h1>' in info_page.text
    assert "Version" in info_page.text
    assert "0.7.1" in info_page.text
    assert "github.com/iihciyekub/netvault" in info_page.text
    assert 'class="author-email"' in info_page.text
    assert "<span>yongjian.li</span><span>@</span><span>polyu.edu.hk</span>" in info_page.text
    assert "mailto:" not in info_page.text
    assert "Usage Declaration" in info_page.text
    assert "must be deleted within 24 hours after use" in info_page.text
    assert "Bulk redistribution" in info_page.text
    assert "Acknowledgement" in info_page.text
    assert "The Hong Kong Polytechnic University (PolyU)" in info_page.text
    assert "School of Fashion and Textiles (SFT)" in info_page.text
    assert "Professor Fan" in info_page.text
    assert "Professor Di Fan" not in info_page.text

    upload = client.post(
        "/web/upload",
        data={"csrf_token": csrf},
        files={"files": ("web.pdf", PDF_BYTES, "application/pdf")},
    )
    assert upload.status_code == 200
    assert "10.1234/web.test" in upload.text
    assert "Drop PDF files" in upload.text
    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    stats = importlib.import_module("netvault_server.server.stats")
    with database.SessionLocal() as db:
        admin = db.query(models.User).filter_by(username="admin").one()
        db.add(
            models.Pdf(
                doi="10.1234/zero-cell.test",
                doi_source="manual",
                sha256="3" * 64,
                original_name="zero-cell.pdf",
                title="Zero cell test",
                authors="[]",
                container_title="Another Journal",
                publisher=None,
                published_year=2025,
                crossref_status="ok",
                size=12,
                storage_path="objects/33/zero-cell.pdf",
                uploaded_by_id=admin.id,
            )
        )
        db.commit()
    stats.invalidate_stats_cache()
    dashboard = client.get("/web")
    assert dashboard.status_code == 200
    assert "journal-heatmap" in dashboard.text
    assert 'data-journal-filter' in dashboard.text
    assert 'data-journal-pin-panel' in dashboard.text
    assert 'data-journal-pin-input' in dashboard.text
    assert 'data-journal-pin-add' in dashboard.text
    assert 'data-journal-pin-clear' in dashboard.text
    assert 'data-journal-pin-list' not in dashboard.text
    assert 'placeholder="Pin a journal name..."' in dashboard.text
    assert 'id="journal-pin-options"' in dashboard.text
    assert 'placeholder="Filter journal names..."' in dashboard.text
    assert 'data-journal-name="Web Journal"' in dashboard.text
    assert 'data-journal-name="Another Journal"' in dashboard.text
    assert "data-journal-row=" in dashboard.text
    assert "No journals match the current filters." in dashboard.text
    assert "data-tip=" in dashboard.text
    assert 'data-tip="2026 · 1 PDF"' in dashboard.text
    assert 'data-tip="Another Journal · 2026' not in dashboard.text
    assert "aria-hidden=\"true\"" in dashboard.text
    assert "0 PDFs" not in dashboard.text
    assert "heatmap-tooltip.js" not in dashboard.text
    assert "heat-cell level-4" in dashboard.text
    assert '<button\n              type="button"\n              class="heat-cell level-4"' in dashboard.text

    pdfs_without_query = client.get("/web/pdfs")
    assert pdfs_without_query.status_code == 200
    assert '<h1 class="sr-only">Search PDFs</h1>' in pdfs_without_query.text
    assert "Search by DOI or Crossref metadata." in pdfs_without_query.text
    assert "10.1234/web.test" not in pdfs_without_query.text

    pdfs_with_query = client.get("/web/pdfs", params={"q": "10.1234/web.test"})
    assert pdfs_with_query.status_code == 200
    assert "10.1234/web.test" in pdfs_with_query.text
    assert "paper-result" in pdfs_with_query.text
    assert "Grace Hopper" in pdfs_with_query.text
    assert "Web Journal" in pdfs_with_query.text
    assert "NetVault Press" in pdfs_with_query.text
    assert "crossref-badge is-verified" in pdfs_with_query.text
    assert "fa-certificate" in pdfs_with_query.text
    assert "fa-check" in pdfs_with_query.text
    assert "Metadata retrieved from Crossref" in pdfs_with_query.text
    assert "https://doi.org/10.1234/web.test" in pdfs_with_query.text
    assert "/web/pdfs/download?pdf_id=" in pdfs_with_query.text
    assert "data-no-pjax" in pdfs_with_query.text

    publisher_search = client.get("/web/pdfs", params={"q": "NetVault Press"})
    assert publisher_search.status_code == 200
    assert "10.1234/web.test" in publisher_search.text

    font = client.get("/static/vendor/fontawesome/webfonts/fa-solid-900.woff2")
    assert font.status_code == 200
    assert font.headers["cache-control"] == "public, max-age=31536000, immutable"

    lookup = client.post(
        "/web/download",
        data={"csrf_token": client.cookies["netvault_csrf"], "doi_text": "https://doi.org/10.1234/web.test"},
    )
    assert lookup.status_code == 200
    assert "Download" in lookup.text
    assert "Download All ZIP" in lookup.text
    assert "results-header" in lookup.text
    assert "data-native-submit" in lookup.text
    assert "/web/pdfs/download?pdf_id=" in lookup.text
    assert "data-no-pjax" in lookup.text

    downloaded = client.get("/web/pdfs/download", params={"doi": "10.1234/web.test"})
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == hashlib.sha256(PDF_BYTES).hexdigest()
    pdf_id = int(lookup.text.split("/web/pdfs/download?pdf_id=", 1)[1].split("\"", 1)[0])
    downloaded_by_id = client.get("/web/pdfs/download", params={"pdf_id": pdf_id})
    assert downloaded_by_id.status_code == 200
    assert hashlib.sha256(downloaded_by_id.content).hexdigest() == hashlib.sha256(PDF_BYTES).hexdigest()
    exists = client.post(
        "/web/pdfs/exists",
        headers={"x-csrf-token": client.cookies["netvault_csrf"]},
        json={"sha256": [hashlib.sha256(PDF_BYTES).hexdigest()]},
    )
    assert exists.status_code == 200
    assert hashlib.sha256(PDF_BYTES).hexdigest() in exists.json()["existing"]
    zipped = client.post(
        "/web/pdfs/download-all",
        data={"csrf_token": client.cookies["netvault_csrf"], "pdf_ids": [str(pdf_id)]},
    )
    assert zipped.status_code == 200
    assert zipped.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        names = archive.namelist()
        assert names == ["10.1234_web.test.pdf", "netvault-manifest.tsv"]
        assert hashlib.sha256(archive.read(names[0])).hexdigest() == hashlib.sha256(PDF_BYTES).hexdigest()
        assert b"10.1234/web.test" in archive.read("netvault-manifest.tsv")


def test_web_admin_can_manage_users_and_is_admin_only(client: TestClient) -> None:
    web_login(client)
    admin_page = client.get("/web/admin")
    assert admin_page.status_code == 200
    assert "Create User" in admin_page.text
    assert "Reset Password" in admin_page.text

    csrf = client.cookies["netvault_csrf"]
    created = client.post(
        "/web/admin/users/create",
        data={"username": "carol", "password": "carol-pass", "role": "user", "csrf_token": csrf},
    )
    assert created.status_code == 200
    assert "Created carol (user)." in created.text
    assert "carol" in created.text

    old_login = client.post("/auth/login", json={"username": "carol", "password": "carol-pass"})
    assert old_login.status_code == 200

    reset = client.post(
        "/web/admin/users/reset-password",
        data={"username": "carol", "password": "new-carol-pass", "csrf_token": client.cookies["netvault_csrf"]},
    )
    assert reset.status_code == 200
    assert "Updated password for carol." in reset.text
    assert client.post("/auth/login", json={"username": "carol", "password": "carol-pass"}).status_code == 401
    assert client.post("/auth/login", json={"username": "carol", "password": "new-carol-pass"}).status_code == 200

    deactivated = client.post(
        "/web/admin/users/set-active",
        data={"username": "carol", "active": "false", "csrf_token": client.cookies["netvault_csrf"]},
    )
    assert deactivated.status_code == 200
    assert "Deactivated carol." in deactivated.text
    assert client.post("/auth/login", json={"username": "carol", "password": "new-carol-pass"}).status_code == 401

    activated = client.post(
        "/web/admin/users/set-active",
        data={"username": "carol", "active": "true", "csrf_token": client.cookies["netvault_csrf"]},
    )
    assert activated.status_code == 200
    assert "Activated carol." in activated.text

    client.cookies.clear()
    login_page = client.get("/web/login")
    csrf = login_page.cookies["netvault_csrf"]
    login = client.post(
        "/web/login",
        data={"username": "carol", "password": "new-carol-pass", "csrf_token": csrf},
    )
    assert login.status_code == 200
    user_home = client.get("/web")
    assert "Admin" not in user_home.text
    assert client.get("/web/admin").status_code == 403


def test_search_pagination_shows_crossref_metadata_cards(client: TestClient) -> None:
    web_login(client)
    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    with database.SessionLocal() as db:
        admin = db.query(models.User).filter_by(username="admin").one()
        for index in range(51):
            db.add(
                models.Pdf(
                    doi=f"10.5555/paged.{index}",
                    doi_source="manual",
                    sha256=f"{index + 1000:064x}",
                    original_name=f"paged-{index}.pdf",
                    title=f"Paged paper {index}",
                    authors="Ada Lovelace; Alan Turing",
                    container_title="Pagination Journal",
                    publisher="Paged Publisher",
                    published_year=2026,
                    crossref_status="ok",
                    crossref_url=f"https://doi.org/10.5555/paged.{index}",
                    size=100 + index,
                    storage_path=f"objects/ff/paged-{index}.pdf",
                    uploaded_by_id=admin.id,
                )
            )
        db.commit()

    first = client.get("/web/pdfs", params={"q": "Paged Publisher"})
    second = client.get("/web/pdfs", params={"q": "Paged Publisher", "page": 2})
    third = client.get("/web/pdfs", params={"q": "Paged Publisher", "page": 3})

    assert first.status_code == 200
    assert first.text.count('class="paper-result"') == 25
    assert "Page 1 of 3" in first.text
    assert "page=2" in first.text
    assert "fa-chevron-right" in first.text
    assert "Ada Lovelace; Alan Turing" in first.text
    assert "crossref-badge is-verified" in first.text
    assert second.status_code == 200
    assert second.text.count('class="paper-result"') == 25
    assert "Page 2 of 3" in second.text
    assert "page=1" in second.text
    assert "fa-chevron-left" in second.text
    assert third.status_code == 200
    assert third.text.count('class="paper-result"') == 1
    assert "Page 3 of 3" in third.text


def test_static_assets_are_long_cached(client: TestClient) -> None:
    for path in (
        "/static/styles.css",
        "/static/app.js",
        "/static/vendor/fontawesome/css/fontawesome.min.css",
        "/static/vendor/fontawesome/css/solid.min.css",
        "/static/vendor/fontawesome/webfonts/fa-solid-900.woff2",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_root_package_is_cli_only() -> None:
    import tomllib

    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(pyproject["project"]["dependencies"])
    scripts = pyproject["project"]["scripts"]
    assert "fastapi" not in dependencies
    assert "sqlalchemy" not in dependencies
    assert "psycopg" not in dependencies
    assert "netvault-admin" not in scripts
