"""Regressions for DOI-first upload and explicit file-to-DOI mapping."""
import hashlib
import importlib
import io

from pypdf import PdfReader, PdfWriter
from typer.testing import CliRunner

from netvault.cli import user
from netvault.cli.upload_fast import FastIdentity, title_matches
import test_api
from test_api import login, upload

client = test_api.client


def pdf_content(doi=None):
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=450)
    if doi:
        writer.add_metadata({"/DOI": doi})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def setup_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "load_credentials", lambda: {"server_url": "https://vault.test"})
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identity.json")
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (False, ()))


def test_existing_embedded_doi_skips_without_hash(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    path = tmp_path / "article.pdf"
    path.write_bytes(pdf_content("10.1234/already-here"))
    monkeypatch.setattr(user, "get_existing_pdfs_by_doi", lambda values: {
        "10.1234/already-here": {"doi": "10.1234/already-here", "title": "Article"}
    })
    def unnecessary(*args, **kwargs):
        raise AssertionError("Should not hash or upload a stored DOI")
    monkeypatch.setattr(user, "file_sha256", unnecessary)
    monkeypatch.setattr(user, "upload_pdf", unnecessary)
    result = CliRunner().invoke(user.app, ["upload", str(path)])
    assert result.exit_code == 0, result.output
    assert "already stored: 1" in result.output


def test_simple_csv_skips_without_hash(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    path = tmp_path / "changed.pdf"
    path.write_bytes(pdf_content("10.1234/old-embedded"))
    mapping = tmp_path / "dois.csv"
    mapping.write_text("file,doi\nchanged.pdf,10.1234/user-choice\n")
    monkeypatch.setattr(user, "get_existing_pdfs_by_doi", lambda values: {
        "10.1234/user-choice": {"doi": "10.1234/user-choice"}
    })
    def unnecessary(*args, **kwargs):
        raise AssertionError("Should not hash or upload a stored DOI")
    monkeypatch.setattr(user, "file_sha256", unnecessary)
    monkeypatch.setattr(user, "upload_pdf", unnecessary)
    result = CliRunner().invoke(user.app, ["upload", str(path), "--csv", str(mapping)])
    assert result.exit_code == 0, result.output
    assert "already stored: 1" in result.output


def test_exact_letters_matching_excludes_references():
    title = "The Impact of Artificial Intelligence on Innovation"
    assert title_matches(title, "THE IMPACT OF ARTIFICIAL\nINTELLIGENCE ON INNOVATION")
    assert not title_matches(title, "Introduction\nReferences\n" + title)


def test_mismatch_rejected_before_hash(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    path = tmp_path / "article.pdf"
    path.write_bytes(pdf_content())
    monkeypatch.setattr(user, "probe_pdf", lambda *_: FastIdentity(
        "10.1234/claimed", "pdf-content", "Different Heading\f"
    ))
    monkeypatch.setattr(user, "get_existing_pdfs_by_doi", lambda *_: {})
    monkeypatch.setattr(user, "check_new_titles",
                        lambda identities: {p: False for p in identities})
    def unnecessary(*args, **kwargs):
        raise AssertionError("Should not hash or upload a rejected DOI")
    monkeypatch.setattr(user, "file_sha256", unnecessary)
    monkeypatch.setattr(user, "upload_pdf", unnecessary)
    result = CliRunner().invoke(user.app, ["upload", str(path)])
    assert result.exit_code == 1, result.output
    from netvault.cli.history import History
    records = list(History(tmp_path, "upload").records())
    assert [row["result"] for row in records].count("review") == 1
    assert not (tmp_path / "nv-upload-report.json").exists()


def test_simple_csv_direct_upload_ignores_old_pdf_doi(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    path = tmp_path / "changed.pdf"
    path.write_bytes(pdf_content("10.1234/previous"))
    mapping = tmp_path / "dois.csv"
    mapping.write_text("file,doi\nchanged.pdf,10.1234/accepted\n")
    monkeypatch.setattr(user, "get_existing_pdfs_by_doi", lambda *_: {})
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *_a, **_k: {})
    sends = []
    def send(pdf, **kw):
        sends.append(kw)
        return {"pdf": {"doi": kw["doi"], "original_name": pdf.name},
                "deduplicated": False}
    monkeypatch.setattr(user, "upload_pdf", send)
    result = CliRunner().invoke(user.app, ["upload", str(path), "--csv", str(mapping)])
    assert result.exit_code == 0, result.output
    assert len(sends) == 1
    assert sends[0]["doi"] == "10.1234/accepted"
    assert sends[0]["doi_source"] == "csv-trusted"


def test_server_manual_doi_stamped_and_raw_hash_protected(client):
    headers = login(client, "admin", "admin-pass")
    original = pdf_content("10.1234/original-info")
    target_doi = "10.1234/correct-manual"
    response = upload(
        client, headers, content=original, doi=target_doi,
        doi_source="manual-trusted"
    )
    assert response.status_code == 200, response.text
    result = response.json()["pdf"]
    assert result["doi"] == target_doi
    assert result["sha256"] != hashlib.sha256(original).hexdigest()
    from netvault_server.server.storage import object_path
    actual = object_path(result["sha256"])
    assert actual.is_file()
    assert PdfReader(str(actual)).metadata["/DOI"] == target_doi
    assert hashlib.sha256(actual.read_bytes()).hexdigest() == result["sha256"]

    conflict = upload(
        client, headers, content=original,
        doi="10.1234/wrong-duplicate", doi_source="manual-trusted",
    )
    assert conflict.status_code == 409
    db = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    with db.SessionLocal() as session:
        assert session.query(models.Pdf).count() == 1
        assert session.query(models.PdfFileAlias).count() == 1


def test_two_column_csv_dry_run_previews_without_identity_scan(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    pdf = tmp_path / "changed.pdf"
    pdf.write_bytes(pdf_content("10.1234/previous"))
    mapping = tmp_path / "doi-map.csv"
    mapping.write_text("file,doi\nchanged.pdf,10.1234/confirmed\n")
    result = CliRunner().invoke(user.app, [
        "upload", str(pdf), "--csv", str(mapping), "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    assert "10.1234/confirmed" in result.output
    assert "CSV trusted" in result.output
    assert not (tmp_path / "nv-upload-report.json").exists()

def test_doi_scan_workers_scale_with_available_cpus(monkeypatch):
    # Linux affinity or equivalent CPU budget is preferred when available.
    monkeypatch.setattr(
        user.os, "sched_getaffinity", lambda _: range(8), raising=False
    )
    assert user.doi_scan_workers(500) == 4
    assert user.doi_scan_workers(2) == 2
    monkeypatch.setattr(user.os, "sched_getaffinity", lambda _: range(24))
    assert user.doi_scan_workers(500) == 6
    monkeypatch.setattr(user.os, "sched_getaffinity", lambda _: range(1))
    assert user.doi_scan_workers(500) == 1


def test_doi_scan_is_parallel_ordered_and_isolates_errors(tmp_path, monkeypatch):
    import threading
    import time

    files = [tmp_path / f"article-{i}.pdf" for i in range(8)]
    for pdf in files:
        pdf.write_bytes(pdf_content())

    active = [0]
    max_active = [0]
    lock = threading.Lock()
    monkeypatch.setattr(user, "doi_scan_workers", lambda _: 4)

    def fake_probe(pdf):
        with lock:
            active[0] += 1
            max_active[0] = max(max_active[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        if pdf.name == "article-3.pdf":
            raise ValueError("ambiguous metadata")
        return FastIdentity(f"10.1234/{pdf.stem}", "pdf-content", "article text")

    monkeypatch.setattr(user, "probe_pdf", fake_probe)
    rows = user.scan_upload_dois(files)
    assert len(rows) == len(files)
    assert max_active[0] == 4
    assert rows[3] == (None, "ambiguous metadata")
    for i in (0, 1, 2, 4, 5, 6, 7):
        assert rows[i][0].doi == f"10.1234/article-{i}"
        assert rows[i][1] is None


def test_ordinary_dry_run_uses_fast_probe_without_hashing(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    pdf = tmp_path / "article.pdf"
    pdf.write_bytes(pdf_content("10.1234/embedded-doi"))
    def unexpected(*_args, **_kwargs):
        raise AssertionError("Fast dry-run must not hash or invoke old PDF scanner")

    monkeypatch.setattr(user, "file_sha256", unexpected)
    monkeypatch.setattr(user, "extract_doi_evidence", unexpected)
    result = CliRunner().invoke(user.app, ["upload", str(pdf), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "10.1234/embedded-doi" in result.output
    assert "[pdf-metadata]" in result.output
    assert not (tmp_path / "nv-upload-report.json").exists()
