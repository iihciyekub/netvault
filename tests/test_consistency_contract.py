"""Cross-entry regressions for identity assertions, aliases and recovery."""

import hashlib
import importlib
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from netvault.cli import user
from netvault.cli.http import ApiError
from netvault.cli.cache import atomic_json, save_cache, load_cache
from test_api import client as client, login, upload, DOI, PDF_BYTES, seed_verified_alias


@pytest.mark.parametrize("assertion", ["option", "cache", "index"])
def test_known_hash_doi_conflict_is_reported_before_skip(tmp_path, monkeypatch, assertion):
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identity.json")
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "load_credentials", lambda: {"server_url": "https://audit.invalid"})
    monkeypatch.setattr(
        user, "load_upload_index_settings", lambda: (True, ("pdf-download-index.json",))
    )
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(PDF_BYTES)
    sha = hashlib.sha256(PDF_BYTES).hexdigest()
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *a, **k: {sha: {"doi": DOI}})
    monkeypatch.setattr(
        user, "upload_pdf", lambda *a, **k: pytest.fail("Known conflicting identity was sent")
    )
    options = []
    if assertion == "option":
        # Manual DOI-first preflight queries the asserted DOI directly; a
        # known DOI is skipped without a full-file SHA comparison.
        monkeypatch.setattr(user, "get_existing_pdfs_by_doi", lambda values: {
            "10.1234/wrong": {"doi": "10.1234/wrong", "title": "User confirmed"}
        })
        options = ["--doi", "10.1234/wrong"]
    elif assertion == "cache":
        user.save_identity_cache({sha: user.manual_identity("10.1234/wrong")})
    else:
        from test_cli_helpers import write_download_index

        index = tmp_path / "index.json"
        write_download_index(index, pdf, "10.1234/wrong")
        options = ["--index-file", str(index)]
    report = tmp_path / "report.json"
    result = CliRunner().invoke(user.app, ["upload", str(pdf), "--report", str(report), *options])
    if assertion == "option":
        assert result.exit_code == 0, result.output
        row = json.loads(report.read_text())["files"][0]
        assert row["status"] == "existing" and row["doi"] == "10.1234/wrong"
    else:
        assert result.exit_code == 1, result.output
        row = json.loads(report.read_text())["files"][0]
        assert row["status"] == "failed" and "already linked" in row["error"]


@pytest.mark.parametrize("status", [401, 429, 503])
def test_bulk_failures_do_not_expand_into_serial_requests(monkeypatch, status):
    def fail(*a, **k):
        raise ApiError(status, "temporary or authentication error")

    monkeypatch.setattr(user, "api_post", fail)
    monkeypatch.setattr(
        user, "get_existing_pdf_by_sha256", lambda *a: pytest.fail("Serial fallback")
    )
    monkeypatch.setattr(user, "get_existing_pdf_by_doi", lambda *a: pytest.fail("Serial fallback"))
    with pytest.raises(ApiError):
        user.get_existing_pdfs_by_sha256(["a" * 64])
    with pytest.raises(ApiError):
        user.get_existing_pdfs_by_doi([DOI])


def test_legacy_bulk_endpoint_still_has_bounded_compatibility(monkeypatch):
    def fail(*a, **k):
        raise ApiError(404, "endpoint unavailable")

    monkeypatch.setattr(user, "api_post", fail)
    monkeypatch.setattr(user, "get_existing_pdf_by_sha256", lambda sha: {"doi": DOI})
    assert user.get_existing_pdfs_by_sha256(["a" * 64]) == {"a" * 64: {"doi": DOI}}


def test_web_api_alias_lookup_and_search_agree(client):
    headers = login(client, "admin", "admin-pass")
    assert upload(client, headers).status_code == 200
    alias = seed_verified_alias(client, headers)
    client.get("/web/login")
    csrf = client.cookies["netvault_csrf"]
    assert (
        client.post(
            "/web/login", data={"username": "admin", "password": "admin-pass", "csrf_token": csrf}
        ).status_code
        == 200
    )
    payload = {"sha256": [alias.upper()]}
    api = client.post("/pdfs/exists", headers=headers, json=payload)
    web = client.post(
        "/web/pdfs/exists", headers={"X-CSRF-Token": client.cookies["netvault_csrf"]}, json=payload
    )
    assert web.status_code == 200 and api.json() == web.json()
    assert api.json()["existing"][alias]["doi"] == DOI
    assert client.get("/pdfs/" + alias.upper(), headers=headers).status_code == 200
    for q in [alias, alias[:12]]:
        found = client.get("/pdfs/search", headers=headers, params={"q": q}).json()
        assert [p["doi"] for p in found] == [DOI]
        assert DOI in client.get("/web/pdfs", params={"q": q}).text


def test_web_api_account_validation_preserves_password_spaces(client):
    headers = login(client, "admin", "admin-pass")
    client.get("/web/login")
    csrf = client.cookies["netvault_csrf"]
    client.post(
        "/web/login", data={"username": "admin", "password": "admin-pass", "csrf_token": csrf}
    )
    password = "  useful-password  "
    response = client.post(
        "/web/admin/users/create",
        data={
            "username": "spaces",
            "password": password,
            "role": "user",
            "csrf_token": client.cookies["netvault_csrf"],
        },
    )
    assert "Created spaces" in response.text
    assert (
        client.post("/auth/login", json={"username": "spaces", "password": password}).status_code
        == 200
    )
    assert (
        client.post(
            "/auth/login", json={"username": "spaces", "password": password.strip()}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/admin/users", headers=headers, json={"username": "用户", "password": password}
        ).status_code
        == 422
    )
    rejected = client.post(
        "/web/admin/users/create",
        data={
            "username": "用户",
            "password": password,
            "role": "user",
            "csrf_token": client.cookies["netvault_csrf"],
        },
    )
    assert "ASCII" in rejected.text


def test_atomic_cache_merges_without_losing_other_commands(tmp_path):
    path = tmp_path / "cache.json"
    old = {"a": {"updated_at": "2026-01-01T00:00:00+00:00", "doi": "old"}}
    save_cache(path, "identities", 1, old)
    save_cache(
        path,
        "identities",
        1,
        {
            "a": {"updated_at": "2026-02-01T00:00:00+00:00", "doi": "new"},
            "b": {"updated_at": "2026-02-01T00:00:00+00:00"},
        },
    )
    save_cache(path, "identities", 1, old)
    stored = load_cache(path, "identities", 1)
    assert stored["a"]["doi"] == "new" and "b" in stored
    save_cache(path, "identities", 1, {}, removed=("a",))
    assert set(load_cache(path, "identities", 1)) == {"b"}
    assert not list(tmp_path.glob("*.tmp"))
    atomic_json(path, {"version": 1, "identities": {}})
    assert load_cache(path, "identities", 1) == {}


def test_alias_without_object_is_not_reported_as_corrupt(client):
    headers = login(client, "admin", "admin-pass")
    upload(client, headers)
    seed_verified_alias(client, headers)
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/audit-pdf-identity.py")
    )
    report = namespace["audit"](0, 1)
    assert report["aliases_without_retained_object"] == 1
    assert report["unreadable_aliases"] == 0


def test_generated_contract_and_candidate_priority_agree():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, str(root / "scripts/sync-identity-contract.py"), "--check"], check=True
    )
    from netvault import doi as cli

    server = importlib.import_module("netvault_server.server.doi")
    for module in (cli, server):
        choices = [
            module.DoiCandidate(DOI, "filename", score=80),
            module.DoiCandidate(DOI, "pdf-content", score=80),
        ]
        assert module.choose_source(choices, DOI) == "pdf-content"
