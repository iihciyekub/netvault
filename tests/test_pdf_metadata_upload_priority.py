"""Document Info identity remains authoritative across CLI and server uploads."""
import asyncio
import csv
import importlib
import io
import json

import pytest
from pypdf import PdfWriter
from typer.testing import CliRunner

from netvault.cli import user
import test_api
from test_api import login, upload


client = test_api.client
DOI = "10.1234/embedded.article"
OTHER = "10.1234/other.article"


def pdf_bytes(*, conflict=False):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/DOI": DOI, "/Title": "A scanned article"})
    if conflict:
        writer.add_metadata({"/Subject": f"DOI: {OTHER}"})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture
def cli_upload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "load_credentials", lambda: {"server_url": "https://vault.example/nv"})
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (True, ()))
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *a, **k: {})
    calls = []

    def do_upload(path, **kwargs):
        calls.append(kwargs)
        return {"deduplicated": False,
                "pdf": {"doi": kwargs["doi"], "original_name": path.name}}

    monkeypatch.setattr(user, "upload_pdf", do_upload)
    monkeypatch.setattr(user, "extract_doi_evidence", lambda *a, **k: pytest.fail("PDF body scanned"))
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(pdf_bytes())
    return pdf, calls


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("hint", ["doi", "cache", "index"])
def test_cli_document_info_precedes_explicit_cached_and_index_hints(
    cli_upload, monkeypatch, hint, dry_run,
):
    pdf, calls = cli_upload
    options = ["--dry-run"] if dry_run else []
    if hint == "doi":
        options += ["--doi", OTHER]
    elif hint == "cache":
        user.save_identity_cache({user.file_sha256(pdf): user.manual_identity(OTHER)})
    else:
        monkeypatch.setattr(user.DownloadIndexResolver, "resolve",
                            lambda *a, **k: pytest.fail("Index took priority over Document Info"))
    result = CliRunner().invoke(user.app, ["upload", str(pdf), *options])
    assert result.exit_code == 0, result.output
    if dry_run:
        assert DOI in result.output
        assert "[pdf-metadata]" in result.output
        assert not calls
    else:
        assert len(calls) == 1
        assert calls[0]["doi"] == DOI
        assert calls[0]["doi_source"] == "pdf-metadata"


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_info_conflict_never_guesses_or_uploads(cli_upload, monkeypatch, dry_run):
    pdf, calls = cli_upload
    pdf.write_bytes(pdf_bytes(conflict=True))
    monkeypatch.setattr(user.DownloadIndexResolver, "resolve",
                        lambda *a, **k: pytest.fail("Conflicting Info fell back to index"))
    options = ["--dry-run"] if dry_run else []
    result = CliRunner().invoke(user.app, ["upload", str(pdf), "--doi", OTHER, *options])
    assert not calls
    if dry_run:
        assert "unresolved" in result.output
        assert "[needs-review]" in result.output
    else:
        assert result.exit_code == 1, result.output
        rows = json.loads((pdf.parent / "nv-upload-report.json").read_text())["files"]
        assert rows[0]["status"] == "needs-review"
        assert "Multiple Document Info DOI values" in rows[0]["error"]


def test_explicit_csv_remains_an_explicit_trust_exception(cli_upload, monkeypatch):
    pdf, calls = cli_upload
    csv_path = pdf.parent / "cli-downloads.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "doi", "bytes", "status"])
        writer.writeheader()
        writer.writerow({"file": pdf.name, "doi": OTHER,
                         "bytes": pdf.stat().st_size, "status": "downloaded"})
    monkeypatch.setattr(user, "extract_document_info_evidence",
                        lambda *a, **k: pytest.fail("Explicit CSV must bypass Info resolution"))
    result = CliRunner().invoke(user.app, ["upload", str(pdf), "--csv", str(csv_path)])
    assert result.exit_code == 0, result.output
    assert calls[0]["doi"] == OTHER
    assert calls[0]["doi_source"] == "csv-trusted"


@pytest.mark.parametrize("doi_source", [None, "filename", "download-record", "download-index", "user", "explicit"])
@pytest.mark.parametrize("metadata_status", ["ok", "not_found", "unavailable"])
def test_server_info_doi_keeps_exact_identity_when_registry_succeeds_or_fails(
    client, monkeypatch, doi_source, metadata_status,
):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    crossref = importlib.import_module("netvault_server.server.crossref")
    calls = []

    def fetch(doi):
        calls.append(doi)
        return crossref.CrossrefMetadata(
            status=metadata_status, canonical_doi=doi,
            title="Registry title differs from the scan" if metadata_status == "ok" else None,
        )

    monkeypatch.setattr(helpers, "fetch_doi_metadata", fetch)
    for name in ("extract_pdf_text", "extract_pdf_title", "search_crossref_metadata", "title_match_score"):
        monkeypatch.setattr(helpers, name, lambda *a, **k: pytest.fail("Info DOI fell back to PDF/title guessing"))
    response = upload(client, login(client, "admin", "admin-pass"),
                      content=pdf_bytes(), name="10.1234_other.article.pdf",
                      doi=OTHER if doi_source else None, doi_source=doi_source)
    assert response.status_code == 200, response.text
    pdf = response.json()["pdf"]
    assert pdf["doi"] == DOI
    assert pdf["doi_source"] == "pdf-metadata"
    assert pdf["crossref_status"] == metadata_status
    assert calls == [DOI]


@pytest.mark.parametrize("no_crossref", [False, True])
def test_server_info_conflict_rejects_even_with_explicit_doi(client, monkeypatch, no_crossref):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    for name in ("fetch_doi_metadata", "extract_pdf_text", "extract_pdf_title", "search_crossref_metadata"):
        monkeypatch.setattr(helpers, name, lambda *a, **k: pytest.fail("Conflicting Info was guessed"))
    response = upload(client, login(client, "admin", "admin-pass"),
                      content=pdf_bytes(conflict=True), doi=OTHER,
                      doi_source="explicit", no_crossref=no_crossref)
    assert response.status_code == 422, response.text
    assert "Multiple Document Info DOI values" in response.json()["detail"]


def test_server_does_not_bind_registry_metadata_for_another_doi(client, monkeypatch):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    crossref = importlib.import_module("netvault_server.server.crossref")
    monkeypatch.setattr(helpers, "fetch_doi_metadata", lambda doi: crossref.CrossrefMetadata(
        status="ok", canonical_doi=OTHER, title="An unrelated article"))
    response = upload(client, login(client, "admin", "admin-pass"), content=pdf_bytes())
    assert response.status_code == 200, response.text
    pdf = response.json()["pdf"]
    assert pdf["doi"] == DOI
    assert pdf["title"] is None
    assert pdf["crossref_status"] == "unavailable"


def test_verifier_returns_document_info_conflict_without_network_or_page_parsing(monkeypatch):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    doi = importlib.import_module("netvault_server.server.doi")
    evidence = doi.DoiEvidence("conflict", None, None, [
        doi.DoiCandidate(DOI, "pdf-metadata", "document-info:/DOI", 96),
        doi.DoiCandidate(OTHER, "pdf-metadata", "document-info:/Subject", 96),
    ])
    for name in ("fetch_doi_metadata", "extract_pdf_text", "extract_pdf_title", "search_crossref_metadata"):
        monkeypatch.setattr(helpers, name, lambda *a, **k: pytest.fail("Info conflict was guessed"))
    result, metadata, attempts = asyncio.run(helpers.verify_automatic_doi(evidence, "unused.pdf"))
    assert result.status == "conflict"
    assert result.doi is None
    assert metadata.status == "skipped"
    assert attempts == []


@pytest.mark.parametrize("source", [None, "explicit", "user", "download-index"])
def test_server_existing_hash_obeys_info_instead_of_conflicting_client_hint(client, source):
    headers = login(client, "admin", "admin-pass")
    first = upload(client, headers, content=pdf_bytes())
    assert first.status_code == 200, first.text
    again = upload(client, headers, content=pdf_bytes(), doi=OTHER, doi_source=source)
    assert again.status_code == 200, again.text
    assert again.json()["deduplicated"] is True
    assert again.json()["pdf"]["doi"] == DOI


@pytest.mark.parametrize("source", [None, "explicit", "csv-trusted"])
def test_server_existing_hash_never_reassigns_historical_doi(client, source):
    headers = login(client, "admin", "admin-pass")
    first = upload(client, headers, content=pdf_bytes(), doi=OTHER, doi_source="csv-trusted")
    assert first.status_code == 200, first.text
    again = upload(client, headers, content=pdf_bytes(),
                   doi=DOI if source else None, doi_source=source)
    assert again.status_code == 409, again.text
    assert f"already linked to DOI {OTHER}" in again.json()["detail"]
    stored = client.get(f"/pdfs/{first.json()['pdf']['id']}", headers=headers)
    assert stored.json()["doi"] == OTHER


def test_server_existing_conflicting_info_rejected_except_explicit_csv(client):
    headers = login(client, "admin", "admin-pass")
    content = pdf_bytes(conflict=True)
    first = upload(client, headers, content=content, doi=OTHER, doi_source="csv-trusted")
    assert first.status_code == 200, first.text
    again = upload(client, headers, content=content)
    assert again.status_code == 422, again.text
    assert "Multiple Document Info DOI values" in again.json()["detail"]
    trusted = upload(client, headers, content=content, doi=OTHER, doi_source="csv-trusted")
    assert trusted.status_code == 200, trusted.text
    assert trusted.json()["pdf"]["doi"] == OTHER
    assert trusted.json()["deduplicated"] is True


@pytest.mark.parametrize("metadata_status", ["not_found", "unavailable"])
def test_csv_force_for_new_doi_requires_successful_metadata(client, monkeypatch, metadata_status):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    crossref = importlib.import_module("netvault_server.server.crossref")
    calls = []

    def fetch(doi):
        calls.append(doi)
        return crossref.CrossrefMetadata(status=metadata_status)

    monkeypatch.setattr(helpers, "fetch_doi_metadata", fetch)
    headers = login(client, "admin", "admin-pass")
    response = upload(client, headers, content=pdf_bytes(),
                      doi=OTHER, doi_source="csv-trusted", force=True)
    assert response.status_code == 503, response.text
    assert "requires externally verified DOI metadata" in response.json()["detail"]
    assert calls == [OTHER]
    assert client.get("/pdfs", headers=headers).json() == []
