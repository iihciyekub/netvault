"""Explicit CSV assertions support image PDFs without weakening automatic uploads."""
import csv
import importlib
import io
import json

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DecodedStreamObject, DictionaryObject, NameObject, NumberObject,
)
from typer.testing import CliRunner

from netvault.cli import upload_csv, user
from netvault.cli.upload_index import DownloadIndexResolver
from netvault.doi import DoiEvidence
import test_api
from test_api import login, upload

client = test_api.client
DOI = "10.1234/scanned"


def scanned_pdf(pixel=b"\x00\x00\x00"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    image = DecodedStreamObject()
    image.set_data(pixel)
    image.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(1),
        NameObject("/Height"): NumberObject(1),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({
            NameObject("/Scan"): writer._add_object(image),
        }),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"q 100 0 0 100 0 0 cm /Scan Do Q")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    content = output.getvalue()
    assert PdfReader(io.BytesIO(content)).pages[0].extract_text() == ""
    return content


def forbid_pdf_identity(helpers, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Explicit CSV must not parse or verify PDF identity")
    for name in ("extract_doi_evidence", "extract_pdf_text", "extract_pdf_title",
                 "verify_automatic_doi", "title_match_score"):
        monkeypatch.setattr(helpers, name, forbidden)


@pytest.mark.parametrize("metadata_status", ["ok", "not_found", "unavailable", "skipped"])
def test_server_csv_scan_accepts_without_pdf_identity_or_metadata_gate(
    client, monkeypatch, metadata_status,
):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    crossref = importlib.import_module("netvault_server.server.crossref")
    forbid_pdf_identity(helpers, monkeypatch)
    calls = []
    def fetch(doi):
        calls.append(doi)
        assert metadata_status != "skipped"
        return crossref.CrossrefMetadata(status=metadata_status, title="Different registry title")
    monkeypatch.setattr(helpers, "fetch_doi_metadata", fetch)
    response = upload(client, login(client, "admin", "admin-pass"), content=scanned_pdf(),
                      doi=DOI, doi_source="csv-trusted", no_crossref=metadata_status == "skipped")
    assert response.status_code == 200, response.text
    pdf = response.json()["pdf"]
    assert pdf["doi"] == DOI
    assert pdf["doi_source"] == "csv-trusted"
    assert pdf["crossref_status"] == metadata_status
    assert calls == ([] if metadata_status == "skipped" else [DOI])
    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    with database.SessionLocal() as db:
        evidence = json.loads(db.query(models.Pdf).one().doi_evidence)
        assert evidence["verification"][0]["pdf_content_verified"] is False


def test_automatic_csv_scan_still_requires_title_verification(client, monkeypatch):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    monkeypatch.setattr(helpers, "extract_pdf_text", lambda path: "")
    monkeypatch.setattr(helpers, "extract_pdf_title", lambda path: None)
    response = upload(client, login(client, "admin", "admin-pass"), content=scanned_pdf(),
                      doi=DOI, doi_source="download-record")
    assert response.status_code == 422, response.text


def test_csv_aliases_deduplicate_but_never_reassign_existing_hash(client, monkeypatch):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    forbid_pdf_identity(helpers, monkeypatch)
    headers = login(client, "admin", "admin-pass")
    original, other = scanned_pdf(), scanned_pdf(b"\xff\xff\xff")
    first = upload(client, headers, content=original, doi=DOI, doi_source="csv-trusted")
    assert first.status_code == 200, first.text
    alias = upload(client, headers, content=other, doi=DOI, doi_source="csv-trusted")
    assert alias.status_code == 200, alias.text
    assert alias.json()["deduplicated"] is True
    for content in (original, other):
        conflict = upload(client, headers, content=content, doi="10.1234/wrong",
                          doi_source="csv-trusted")
        assert conflict.status_code == 409, conflict.text
    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    with database.SessionLocal() as db:
        assert db.query(models.Pdf).count() == 1
        assert db.query(models.PdfFileAlias).one().source == "csv-trusted"


@pytest.mark.parametrize("available", [True, False])
def test_csv_force_skips_pdf_identity_but_requires_metadata(client, monkeypatch, available):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    crossref = importlib.import_module("netvault_server.server.crossref")
    forbid_pdf_identity(helpers, monkeypatch)
    headers = login(client, "admin", "admin-pass")
    first = upload(client, headers, content=scanned_pdf(), doi=DOI, doi_source="csv-trusted")
    assert first.status_code == 200, first.text
    monkeypatch.setattr(helpers, "fetch_doi_metadata", lambda doi:
                        crossref.CrossrefMetadata(status="ok" if available else "unavailable"))
    result = upload(client, headers, content=scanned_pdf(b"\xff\x00\x00"), doi=DOI,
                    doi_source="csv-trusted", force=True)
    assert result.status_code == (200 if available else 503), result.text
    if available:
        assert result.json()["replaced"] is True
    else:
        assert client.get("/pdfs", headers=headers).json()[0]["sha256"] == first.json()["pdf"]["sha256"]


@pytest.mark.parametrize("content,doi", [
    (b"not a pdf", DOI), (b"%PDF-1.4\nincomplete", DOI),
    (b"", DOI), (b"%PDF-1.4\n%%EOF", "invalid"),
])
def test_trusted_csv_still_rejects_invalid_files_and_dois(client, content, doi):
    response = upload(client, login(client, "admin", "admin-pass"), content=content,
                      doi=doi, doi_source="csv-trusted")
    assert response.status_code == 400, response.text


@pytest.fixture()
def cli_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (True, ("pdf-download-index.json",)))
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *a, **k: {})
    monkeypatch.setattr(user, "get_existing_pdfs_by_doi", lambda *a, **k: {})
    calls = []
    def send(path, **kwargs):
        calls.append(kwargs)
        return {"pdf": {"doi": kwargs["doi"], "original_name": path.name}, "deduplicated": False}
    monkeypatch.setattr(user, "upload_pdf", send)
    def forbidden(*a, **k):
        pytest.fail("Explicit CSV must never fall back to PDF text")
    monkeypatch.setattr(user, "extract_doi_evidence", forbidden)
    monkeypatch.setattr(upload_csv, "first_page_text", forbidden)
    return tmp_path, calls


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "status", "doi", "bytes", "sha256", "title"])
        writer.writeheader()
        writer.writerows(rows)


def csv_row(pdf, **updates):
    row = dict(file=pdf.name, status="downloaded", doi=DOI, bytes=pdf.stat().st_size,
               sha256="", title="")
    return row | updates


def test_cli_explicit_csv_trust_never_persists_and_does_not_skip_hash_conflicts(
    cli_setup, monkeypatch,
):
    root, calls = cli_setup
    pdf = root / "scan.pdf"
    pdf.write_bytes(scanned_pdf())
    csv_path = root / "cli-downloads.csv"
    write_csv(csv_path, [csv_row(pdf)])
    digest = user.file_sha256(pdf)
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *a, **k:
                        {digest: {"doi": "10.1234/previous", "original_name": pdf.name}})
    result = CliRunner().invoke(user.app, ["upload", str(pdf), "--csv", str(csv_path)])
    assert result.exit_code == 0, result.output
    assert calls[0]["doi_source"] == "csv-trusted"  # Must reach the server's conflict check.
    assert not user.IDENTITY_CACHE_PATH.exists()
    assert user.cached_identity({digest: user.download_index_identity(
        DownloadIndexResolver((), explicit_csv=csv_path).resolve(pdf, digest, pdf.stat().st_size)
    )}, digest) is None
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *a, **k: {})
    calls.clear()
    automatic = CliRunner().invoke(user.app, ["upload", str(pdf)])
    assert automatic.exit_code == 0, automatic.output
    assert calls[0]["doi_source"] == "download-record"


@pytest.mark.parametrize("mode", ["unmatched", "title-only", "wrong-size", "ambiguous", "wrong-hash"])
def test_cli_explicit_csv_unmatched_never_uses_cached_or_pdf_identity(cli_setup, mode):
    root, calls = cli_setup
    pdf = root / "scan.pdf"
    pdf.write_bytes(scanned_pdf())
    row = csv_row(pdf)
    if mode in {"unmatched", "title-only"}:
        row["file"] = "original.pdf"
        row["title"] = "An Article Title Long Enough for Automatic Matching"
    elif mode == "wrong-size":
        row["bytes"] += 1
    elif mode == "wrong-hash":
        row["sha256"] = "0" * 64
    rows = [row]
    if mode == "ambiguous":
        rows.append(row | {"doi": "10.1234/other"})
    csv_path = root / "cli-downloads.csv"
    write_csv(csv_path, rows)
    digest = user.file_sha256(pdf)
    user.save_identity_cache({digest: user.identity_from_evidence(
        DoiEvidence("ok", DOI, "pdf-content", [])
    )})
    result = CliRunner().invoke(user.app, ["upload", str(pdf), "--csv", str(csv_path)])
    assert result.exit_code == 1, result.output
    assert "PDF fallback is disabled" in " ".join(result.output.split())
    assert not calls
    report = json.loads((root / "nv-upload-report.json").read_text())
    assert report["summary"]["failed"] == 1
    assert report["summary"]["pending"] == 0


def test_explicit_csv_hash_ambiguity_cannot_fall_back_to_original_name(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(scanned_pdf())
    digest = user.file_sha256(pdf)
    csv_path = tmp_path / "cli-downloads.csv"
    write_csv(csv_path, [csv_row(pdf, sha256=digest),
                         csv_row(pdf, file="other.pdf", sha256=digest, doi="10.1234/other")])
    assert DownloadIndexResolver((), explicit_csv=csv_path).resolve(
        pdf, digest, pdf.stat().st_size,
    ) is None


def test_identical_files_cannot_have_different_csv_dois(cli_setup):
    root, calls = cli_setup
    docs = root / "papers"
    docs.mkdir()
    first, second = docs / "first.pdf", docs / "second.pdf"
    first.write_bytes(scanned_pdf())
    second.write_bytes(first.read_bytes())
    csv_path = root / "cli-downloads.csv"
    write_csv(csv_path, [csv_row(first), csv_row(second, doi="10.1234/different")])
    result = CliRunner().invoke(user.app, ["upload", str(docs), "--csv", str(csv_path)])
    assert result.exit_code == 1, result.output
    assert "different DOIs to identical PDFs" in result.output
    assert not calls
    report = json.loads((root / "nv-upload-report.json").read_text())
    assert report["summary"]["failed"] == 2
    assert report["summary"]["local-duplicate"] == 0


def test_explicit_csv_preview_does_not_read_pdf_for_title_only_match(cli_setup):
    root, calls = cli_setup
    pdf = root / "scan.pdf"
    pdf.write_bytes(scanned_pdf())
    csv_path = root / "cli-downloads.csv"
    write_csv(csv_path, [csv_row(pdf, file="original.pdf",
                               title="An Article Title Long Enough for Automatic Matching")])
    result = CliRunner().invoke(user.app, ["upload", str(pdf), "--csv", str(csv_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "PDF fallback is disabled" in " ".join(result.output.split())
    assert not calls
    assert not user.HASH_CACHE_PATH.exists()
    assert not user.IDENTITY_CACHE_PATH.exists()
    assert not (root / "nv-upload-report.json").exists()
