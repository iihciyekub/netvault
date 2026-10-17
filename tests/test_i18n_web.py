"""NetVault trilingual UI contract and dynamic browser translation checks."""
from __future__ import annotations

import importlib
from pathlib import Path
import re
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "packages/netvault-server/src/netvault_server/server/static"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NETVAULT_DATABASE_URL", f"sqlite:///{tmp_path / 'netvault-lang.db'}")
    monkeypatch.setenv("NETVAULT_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("NETVAULT_SECRET_KEY", "localization-fixture-key")
    monkeypatch.setenv("NETVAULT_BASE_PATH", "")
    monkeypatch.setenv("NETVAULT_BOOTSTRAP_ADMIN", "admin")
    monkeypatch.setenv("NETVAULT_BOOTSTRAP_ADMIN_PASSWORD", "admin-pass")
    for module in list(sys.modules):
        if module.startswith("netvault_server.server"):
            del sys.modules[module]
    from netvault_server.server.main import app
    with TestClient(app) as conn:
        yield conn


def sign_in(client: TestClient):
    csrf = client.get("/web/login").cookies["netvault_csrf"]
    response = client.post("/web/login", data={
        "username": "admin", "password": "admin-pass", "csrf_token": csrf,
    })
    assert response.status_code == 200


def test_english_server_html_and_login_language_access(client: TestClient):
    login = client.get("/web/login")
    assert login.status_code == 200
    assert '<html lang="en">' in login.text
    assert 'data-language-toggle' in login.text
    assert 'id="login-language-menu"' in login.text
    assert 'data-language-option="en"' in login.text
    assert 'data-language-option="zh-CN"' in login.text
    assert 'data-language-option="zh-TW"' in login.text
    assert login.text.count("data-language-option=") == 3
    assert login.text.index('data-theme-toggle') < login.text.index('data-language-toggle')
    assert re.search(r"/static/i18n.js\?v=[^\"&]+&amp;h=[a-f0-9]{12}",login.text)
    assert "Username" in login.text, "Non-JS English fallback remains readable"
    assert "i18n-catalog.tsv" not in login.text


def test_all_main_views_keep_lang_control_after_journal_navigation(client: TestClient):
    sign_in(client)
    for route in ("/web", "/web/pdfs", "/web/upload", "/web/cli", "/web/info",
                  "/web/admin", "/web/admin/maintenance", "/web/admin/jobs",
                  "/web/admin/users"):
        response = client.get(route)
        assert response.status_code == 200, route
        html = response.text
        assert "data-language-toggle" in html, route
        assert html.count("data-language-option=") == 3
        assert html.index('data-theme-toggle') < html.index('data-language-toggle') < html.index(
            'data-tooltip="Help"'
        ), route
        assert '<i class="fa-solid fa-language fa-fw"' in html
        assert 'aria-haspopup="menu" aria-expanded="false"' in html
        assert '<script src="/static/i18n.js?' in html
        assert re.search(r'/static/styles.css\?v=[^\"]+&amp;h=[a-f0-9]{12}',html)
        assert re.search(r'/static/app.js\?v=[^\"]+&amp;h=[a-f0-9]{12}',html)
    admin = client.get("/web/admin/maintenance")
    assert "Start Full Library Optimization + DOI" in admin.text
    assert "i18n.js" in admin.text


def test_locale_assets_are_self_contained_and_correctly_cached(client: TestClient):
    import hashlib
    for name in ("i18n.js", "styles.css", "app.js"):
        response = client.get("/static/" + name)
        assert response.status_code == 200
        assert response.content == (STATIC / name).read_bytes()
    login = client.get("/web/login")
    assert hashlib.sha256((STATIC / "i18n.js").read_bytes()).hexdigest()[:12] in login.text
    assert hashlib.sha256((STATIC / "styles.css").read_bytes()).hexdigest()[:12] in login.text
    assert hashlib.sha256((STATIC / "app.js").read_bytes()).hexdigest()[:12] in login.text


def test_generated_catalog_has_no_duplicate_keys_and_covers_critical_pages():
    spec = importlib.util.spec_from_file_location("build_i18n", ROOT / "scripts/build_i18n.py")
    assert spec and spec.loader
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    assert builder.generate() == (STATIC / "i18n.js").read_text(encoding="utf-8")
    catalog = {}
    for line in (STATIC / "i18n-catalog.tsv").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, simplified, traditional = line.split("\t")
        assert key not in catalog, key
        assert simplified and traditional
        catalog[key] = (simplified, traditional)
    assert len(catalog) >= 400
    for text in (
        "Home", "Search", "Help", "Upload", "Journal list", "Search PDFs",
        "Download selected ZIP", "Platform Overview", "Usage Policy",
        "Full Library Optimization + DOI Writeback", "Document Health Audit",
        "Start Full Library Optimization + DOI", "Task History & Review",
        "Read Document Info offline, then append a DOI to its custom",
    ):
        assert text in catalog
    assert catalog["Document & Database Maintenance"] == ("文献与数据库维护", "文獻與資料庫維護")


def test_trilingual_browser_state_and_pjax_updates():
    node = __import__("shutil").which("node")
    if not node:
        pytest.skip("Node.js is required for language switch browser-state checks")
    result = subprocess.run(
        [node, str(ROOT / "tests/check_i18n_ui.cjs")],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_i18n_assets_do_not_make_api_or_host_backup_requests():
    script = (STATIC / "i18n-runtime.js").read_text()
    assert "fetch(" not in script
    assert "XMLHttpRequest" not in script
    assert "document.cookie" in script
    assert "location.reload()" in script
    assert 'netvault:language:v1' in script
    assert "window.NetVaultI18n" in script
    app = (STATIC / "app.js").read_text()
    assert "window.NetVaultI18n?.translate(currentMain)" in app
    styles = (STATIC / "styles.css").read_text()
    assert ".language-menu[hidden]" in styles
    assert ".language-switcher" in styles
