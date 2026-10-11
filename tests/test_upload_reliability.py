import csv
import importlib
import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from netvault.cli import http, user
from netvault.cli.upload_index import DownloadIndexResolver
from netvault.doi import DoiEvidence
import test_api
from test_api import login, upload

client = test_api.client


def test_primary_page_doi_survives_many_raw_reference_candidates(tmp_path, monkeypatch):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    doi = importlib.import_module("netvault_server.server.doi")
    primary = "10.25300/misq/2024/17307"
    refs = [doi.DoiCandidate(f"10.1000/cited.{n}", "pdf-content", "raw-explicit", 88)
            for n in range(30)]
    evidence = doi.DoiEvidence("conflict", None, None,
                              [*refs, doi.DoiCandidate(primary, "pdf-content", "pdftotext-page-1", 88)])
    ordered = helpers._ordered_automatic_candidates(evidence, refs[0].doi, "pdf-content")
    assert ordered[0].doi == primary
    assert len(ordered) == 12


def test_cited_title_cannot_create_wrong_alias(client, monkeypatch):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    crossref = importlib.import_module("netvault_server.server.crossref")
    cited = "10.1234/cited"
    monkeypatch.setattr(helpers, "extract_pdf_title", lambda path: None)
    monkeypatch.setattr(helpers, "fetch_doi_metadata", lambda doi: crossref.CrossrefMetadata(
        status="ok", canonical_doi=doi, title="A Long Primary Title for the Cited Article",
        work_type="journal-article",
    ))
    monkeypatch.setattr(helpers, "extract_pdf_text", lambda path:
                        "A Long Primary Title for the Cited Article\nAbstract\nArticle body")
    headers = login(client, "admin", "admin-pass")
    seed = upload(client, headers, content=f"%PDF-1.4\nDOI: {cited}\n%%EOF".encode())
    assert seed.status_code == 200, seed.text
    monkeypatch.setattr(helpers, "extract_pdf_text", lambda path:
                        "The Actual Different Primary Title\nAbstract\nOwn text\f"
                        "References\nA Long Primary Title for the Cited Article")
    response = upload(client, headers, name="different.pdf",
                      content=f"%PDF-1.4\nDifferent file\nDOI: {cited}\n%%EOF".encode())
    assert response.status_code == 422, response.text
    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    with database.SessionLocal() as db:
        assert db.query(models.Pdf).count() == 1
        assert db.query(models.PdfFileAlias).count() == 0


def test_automatic_doi_requires_readable_heading(client, monkeypatch):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    from netvault_server.server.article_title import heading_match_score
    monkeypatch.setattr(helpers, "title_match_score", heading_match_score)
    monkeypatch.setattr(helpers, "extract_pdf_text", lambda path: "")
    response = upload(client, login(client, "admin", "admin-pass"))
    assert response.status_code == 422
    assert client.get("/pdfs", headers=login(client, "admin", "admin-pass")).json() == []


def test_csv_hint_is_size_bound_and_still_verified_by_server(tmp_path, monkeypatch):
    pdf = tmp_path / "article.pdf"
    pdf.write_bytes(b"%PDF-1.4\narticle without embedded DOI\n%%EOF")
    csv_path = tmp_path / "cli-downloads.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "status", "doi", "bytes"])
        writer.writeheader()
        writer.writerow(dict(file=pdf.name, status="downloaded", doi="10.25300/MISQ/2024/17307",
                             bytes=pdf.stat().st_size))
    resolver = DownloadIndexResolver(("pdf-download-index.json",))
    match = resolver.resolve(pdf, user.file_sha256(pdf), pdf.stat().st_size)
    assert match.source == "download-record"
    assert match.record.doi == "10.25300/misq/2024/17307"
    assert resolver.resolve(pdf, user.file_sha256(pdf), pdf.stat().st_size + 1) is None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (True, ("pdf-download-index.json",)))
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *a, **k: {})
    def exists(dois):
        assert not list(dois), "legacy CSV must not cause an unverified DOI-only skip"
        return {}
    monkeypatch.setattr(user, "get_existing_pdfs_by_doi", exists)
    calls = []
    def send(path, **kwargs):
        calls.append(kwargs)
        return {"pdf": {"doi": kwargs["doi"], "original_name": path.name}, "deduplicated": False}
    monkeypatch.setattr(user, "upload_pdf", send)
    result = CliRunner().invoke(user.app, ["upload", str(pdf)])
    assert result.exit_code == 0, result.output
    assert calls[0]["doi_source"] == "download-record"


def test_report_separates_new_existing_failed_and_local_duplicates_and_resumes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (False, ()))
    for name in ("new", "failed", "existing"):
        (tmp_path / f"{name}.pdf").write_bytes(f"%PDF-1.4\n{name}\n%%EOF".encode())
    (tmp_path / "new-copy.pdf").write_bytes((tmp_path / "new.pdf").read_bytes())
    existing_hash = user.file_sha256(tmp_path / "existing.pdf")
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *a, **k:
                        {existing_hash: {"doi": "10.1234/existing", "original_name": "existing.pdf"}})
    monkeypatch.setattr(user, "get_existing_pdfs_by_doi", lambda *a, **k: {})
    monkeypatch.setattr(user, "extract_doi_evidence", lambda path:
                        DoiEvidence("ok", "10.1234/article", "pdf-content", []))
    def send(path, **kwargs):
        if path.stem == "failed":
            raise RuntimeError("422: title does not match")
        return {"pdf": {"doi": kwargs["doi"], "original_name": path.name}, "deduplicated": False}
    monkeypatch.setattr(user, "upload_pdf", send)
    report = tmp_path / "result.json"
    result = CliRunner().invoke(user.app, ["upload", str(tmp_path), "--report", str(report)])
    assert result.exit_code == 1, result.output
    saved = json.loads(report.read_text())
    assert saved["finished"]
    assert saved["summary"] == dict(pending=0, uploaded=1, existing=1, replaced=0, failed=1,
                                    **{"local-duplicate": 1}, found_paths=4, processed=4)
    assert "only uploaded is new" in result.output
    calls = []
    def recovered(path, **kwargs):
        calls.append(path.name)
        return {"pdf": {"doi": kwargs["doi"], "original_name": path.name}, "deduplicated": False}
    monkeypatch.setattr(user, "upload_pdf", recovered)
    retry = CliRunner().invoke(user.app, ["upload", str(tmp_path), "--report", str(report), "--retry-failed"])
    assert retry.exit_code == 0, retry.output
    assert calls == ["failed.pdf"]
    assert json.loads(report.read_text())["summary"]["uploaded"] == 1


def test_http_retry_reopens_stream_and_does_not_retry_validation_errors(tmp_path, monkeypatch):
    pdf = tmp_path / "article.pdf"
    content = b"%PDF-1.4\nactual PDF body\n%%EOF"
    pdf.write_bytes(content)
    monkeypatch.setattr(http, "auth_headers", lambda: {})
    monkeypatch.setattr(http, "server_url", lambda: "https://example.test")
    monkeypatch.setattr(http, "sleep", lambda seconds: None)
    bodies = []
    def post(url, **kwargs):
        assert kwargs["headers"]["Idempotency-Key"] == "hash"
        bodies.append(kwargs["data"].read())
        code = 503 if len(bodies) == 1 else 200
        return SimpleNamespace(ok=code == 200, status_code=code, headers={}, text="",
                               json=lambda: {"detail": "temporary", "deduplicated": False})
    monkeypatch.setattr(http, "http_session", lambda: SimpleNamespace(post=post))
    assert http.upload_pdf(pdf, sha256="hash")["deduplicated"] is False
    assert len(bodies) == 2 and all(content in body for body in bodies)
    calls = []
    def invalid(*args, **kwargs):
        calls.append(1)
        raise http.ApiError(422, "wrong article")
    monkeypatch.setattr(http, "_upload_pdf_once", invalid)
    with pytest.raises(http.ApiError):
        http.upload_pdf(pdf, sha256="hash")
    assert len(calls) == 1


def _write_download_csv(path, rows, fieldnames=None):
    fieldnames = fieldnames or ["file", "status", "doi", "bytes", "sha256", "title"]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_csv_sha256_matches_renamed_pdf_and_never_uses_filename_only(tmp_path):
    pdf = tmp_path / "renamed.pdf"
    pdf.write_bytes(b"%PDF-1.4\nA file whose name changed\n%%EOF")
    sha = user.file_sha256(pdf)
    csv_path = tmp_path / "cli-downloads.csv"
    _write_download_csv(csv_path, [{
        "file": "original.pdf", "status": "downloaded", "doi": "10.1234/renamed",
        "bytes": pdf.stat().st_size, "sha256": sha, "title": "",
    }])
    match = DownloadIndexResolver(("pdf-download-index.json",)).resolve(
        pdf, sha, pdf.stat().st_size
    )
    assert match is not None
    assert match.record.doi == "10.1234/renamed"
    assert match.renamed
    assert match.record.validation_method == "download-record-sha256"
    assert match.source == "download-record"
    assert DownloadIndexResolver(("pdf-download-index.json",)).resolve(
        pdf, "0" * 64, pdf.stat().st_size
    ) is None


def test_csv_duplicate_hash_with_conflicting_dois_is_rejected(tmp_path):
    pdf = tmp_path / "renamed.pdf"
    pdf.write_bytes(b"%PDF-1.4\nsame bytes\n%%EOF")
    sha = user.file_sha256(pdf)
    _write_download_csv(tmp_path / "cli-downloads.csv", [
        {"file": f"old{i}.pdf", "status": "downloaded", "doi": doi,
         "bytes": pdf.stat().st_size, "sha256": sha, "title": ""}
        for i, doi in enumerate(("10.1234/one", "10.1234/two"))
    ])
    warnings = []
    resolver = DownloadIndexResolver(("pdf-download-index.json",),
                                     warning_callback=warnings.append)
    assert resolver.resolve(pdf, sha, pdf.stat().st_size) is None
    assert any("ambiguous" in message for message in warnings)


def test_csv_title_matches_renamed_modified_pdf_only_on_unique_front_page(tmp_path, monkeypatch):
    from netvault.cli import upload_csv
    pdf = tmp_path / "new-name.pdf"
    pdf.write_bytes(b"%PDF-1.4\nmodified bytes\n%%EOF")
    csv_path = tmp_path / "cli-downloads.csv"
    title = "The Impact of Automation on Scientific Literature Collections"
    _write_download_csv(csv_path, [
        {"file": "old-name.pdf", "status": "downloaded",
         "doi": "10.1234/automation", "bytes": 900, "sha256": "", "title": title}
    ])
    monkeypatch.setattr(upload_csv, "first_page_text", lambda _:
                        "Authors\n" + title + "\nAbstract\nAn independent study.")
    resolver = DownloadIndexResolver(("pdf-download-index.json",))
    match = resolver.resolve(pdf, user.file_sha256(pdf), pdf.stat().st_size)
    assert match.record.doi == "10.1234/automation"
    assert match.record.validation_method == "download-record-title"
    assert match.source == "download-record"  # Server validates the title.
    assert match.renamed
    monkeypatch.setattr(upload_csv, "first_page_text", lambda _:
                        "Abstract\nA different and unrelated study\nReferences\n" + title)
    assert resolver.resolve(pdf, user.file_sha256(pdf), pdf.stat().st_size) is None


def test_csv_explicit_other_directory_dry_run_never_uploads_or_writes(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    pdf = docs / "renamed.pdf"
    pdf.write_bytes(b"%PDF-1.4\nRenamed PDF\n%%EOF")
    csv_path = tmp_path / "cli-downloads.csv"
    _write_download_csv(csv_path, [{
        "file": "original.pdf", "status": "downloaded", "doi": "10.1234/explicit",
        "bytes": pdf.stat().st_size, "sha256": user.file_sha256(pdf), "title": "",
    }])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user, "ensure_logged_in", lambda: pytest.fail("must not log in"))
    monkeypatch.setattr(user, "upload_pdf", lambda *a, **k: pytest.fail("must not upload"))
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *a, **k:
                        pytest.fail("must not contact server"))
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (True, ("pdf-download-index.json",)))
    result = CliRunner().invoke(user.app, [
        "upload", str(docs), "--csv", str(csv_path), "--dry-run"
    ])
    assert result.exit_code == 0, result.output
    assert "10.1234/explicit" in result.output
    assert "renamed" in result.output
    assert not (tmp_path / "nv-upload-report.json").exists()
    assert not (tmp_path / "hash-cache.json").exists()
    assert not (tmp_path / "identity-cache.json").exists()



def test_csv_at_upload_root_matches_nested_renamed_pdf(tmp_path):
    parent = tmp_path / "papers"
    nested = parent / "journal" / "2024"
    nested.mkdir(parents=True)
    pdf = nested / "renamed.pdf"
    pdf.write_bytes(b"%PDF-1.4\nRenamed paper\n%%EOF")
    digest = user.file_sha256(pdf)
    _write_download_csv(parent / "cli-downloads.csv", [{
        "file": "original.pdf", "status": "downloaded", "doi": "10.1234/nested",
        "bytes": pdf.stat().st_size, "sha256": digest, "title": "",
    }])
    resolver = DownloadIndexResolver(("pdf-download-index.json",), csv_roots=(parent,))
    match = resolver.resolve(pdf, digest, pdf.stat().st_size)
    assert match is not None and match.renamed
    assert match.record.doi == "10.1234/nested"
    assert DownloadIndexResolver(("pdf-download-index.json",)).resolve(
        pdf, digest, pdf.stat().st_size
    ) is None



def test_upload_dry_run_auto_finds_csv_in_parent_folder(tmp_path, monkeypatch):
    parent = tmp_path / "papers"
    nested = parent / "journal"
    nested.mkdir(parents=True)
    pdf = nested / "newname.pdf"
    pdf.write_bytes(b"%PDF-1.4\nA renamed document\n%%EOF")
    _write_download_csv(parent / "cli-downloads.csv", [{
        "file": "oldname.pdf", "status": "downloaded", "doi": "10.1234/auto-parent",
        "bytes": pdf.stat().st_size, "sha256": user.file_sha256(pdf), "title": "",
    }])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user, "load_upload_index_settings",
                        lambda: (True, ("pdf-download-index.json",)))
    monkeypatch.setattr(user, "ensure_logged_in", lambda: pytest.fail("dry run logged in"))
    monkeypatch.setattr(user, "upload_pdf", lambda *a, **k: pytest.fail("dry run uploaded"))
    result = CliRunner().invoke(user.app, ["upload", str(parent), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "10.1234/auto-parent" in result.output
    assert "renamed" in result.output
    assert not (tmp_path / "nv-upload-report.json").exists()
