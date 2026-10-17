import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from pypdf import PdfWriter
from typer.testing import CliRunner

from netvault.cli import http, user
from netvault.cli.pdf_meta import read_pdf_metadata, write_pdf_metadata
from netvault.cli.upload_report import UploadReport
from netvault.cli.upload_state import UploadState
from netvault.doi import DoiEvidence


DOI = "10.1234/verified"


def make_pdf(path, metadata=None):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    if metadata:
        writer.add_metadata(metadata)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def receipt(sha256, **changes):
    return {
        "policy_version": 1, "input_sha256": sha256, "doi": DOI,
        "canonical_doi": DOI, "decision": "verified", "writeback_allowed": True,
        "metadata_status": "ok", "metadata_provider": "crossref",
        "printed_doi_confirmed": True, "title_match_score": 1.0,
        "author_match": True, "year_match": True, "reason": "independently verified",
        "verified_at": "2026-10-15T00:00:00+00:00", **changes,
    }


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "load_credentials", lambda: {"server_url": "https://vault.test"})
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (False, ()))
    calls = {"verify": [], "write": [], "upload": [], "exists": []}

    def verify(path, sha256):
        assert sha256 == user.file_sha256(path)
        assert read_pdf_metadata(path)["status"] == "no-doi"
        calls["verify"].append((path, sha256))
        return receipt(sha256)

    def write(path, verification):
        assert verification["input_sha256"] == user.file_sha256(path)
        calls["write"].append(path)
        backup = path.with_suffix(".pdf.original.bak")
        backup.write_bytes(path.read_bytes())
        result = write_pdf_metadata(path, verification["doi"])
        return {**result, "original_sha256": verification["input_sha256"],
                "sha256": user.file_sha256(path), "backup": str(backup),
                "verification": verification}

    def existing(hashes, **kwargs):
        calls["exists"].append(set(hashes))
        return {}

    def upload(path, **kwargs):
        assert kwargs["sha256"] == user.file_sha256(path)
        calls["upload"].append((path, kwargs))
        return {"deduplicated": False,
                "pdf": {"doi": kwargs["doi"], "original_name": path.name}}

    monkeypatch.setattr(user, "verify_pdf_doi", verify)
    monkeypatch.setattr(user, "write_verified_pdf_doi", write)
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", existing)
    monkeypatch.setattr(user, "get_existing_pdfs_by_doi", lambda *a, **k: {})
    monkeypatch.setattr(user, "upload_pdf", upload)
    return tmp_path, calls


def invoke(path, *options):
    return CliRunner().invoke(user.app, ["upload", str(path), *options])


def report(path):
    return json.loads((path / "nv-upload-report.json").read_text())


def test_writeback_verifies_original_writes_then_uploads_new_hash(configured, monkeypatch):
    directory, calls = configured
    pdf = make_pdf(directory / "paper.pdf")
    original = pdf.read_bytes()
    original_sha = user.file_sha256(pdf)
    # Poison both caches with a matching stat signature. --write-doi must bypass both.
    hash_cache = {}
    user.cached_file_sha256(pdf, hash_cache)
    hash_cache[user.file_cache_key(pdf)]["sha256"] = "0" * 64
    user.save_hash_cache(hash_cache)
    with UploadState() as state:
        state.hash_file(pdf, lambda _: "1" * 64)
    result = invoke(pdf, "--write-doi")
    assert result.exit_code == 0, result.output
    assert calls["verify"] == [(pdf, original_sha)]
    assert calls["write"] == [pdf]
    new_sha = user.file_sha256(pdf)
    assert new_sha != original_sha
    assert calls["exists"] == [{new_sha}]
    assert calls["upload"][0][1]["sha256"] == new_sha
    assert calls["upload"][0][1]["doi_source"] == "pdf-metadata"
    row = report(directory)["files"][0]
    assert row["status"] == "uploaded"
    writeback = row["metadata_writeback"]
    assert writeback["status"] == "written" and writeback["doi"] == DOI
    assert writeback["original_sha256"] == original_sha and writeback["sha256"] == new_sha
    assert Path(writeback["backup"]).read_bytes() == original
    assert user.load_hash_cache()[user.file_cache_key(pdf)]["sha256"] == new_sha


@pytest.mark.parametrize("decision", ["unverified", "conflict", "blocked", "unavailable", "already-present"])
def test_nonverified_receipt_never_writes_or_uploads(configured, monkeypatch, decision):
    directory, calls = configured
    pdf = make_pdf(directory / "paper.pdf")
    original = pdf.read_bytes()
    monkeypatch.setattr(user, "verify_pdf_doi", lambda path, sha:
                        receipt(sha, decision=decision, writeback_allowed=False,
                                reason="manual review required"))
    result = invoke(pdf, "--write-doi")
    assert result.exit_code == 1, result.output
    assert pdf.read_bytes() == original
    assert calls["write"] == calls["upload"] == []
    row = report(directory)["files"][0]
    assert row["status"] == ("failed" if decision == "unavailable" else "needs-review")
    assert row["metadata_writeback"]["status"] == decision
    assert row["metadata_writeback"]["verification"]["decision"] == decision


@pytest.mark.parametrize("failure", [requests.ReadTimeout("timed out"),
                                   http.ApiError(404, "server 0.7.66 required"),
                                   ValueError("invalid verification response")])
def test_verification_failure_does_not_fall_back_to_upload(configured, monkeypatch, failure):
    directory, calls = configured
    pdf = make_pdf(directory / "paper.pdf")
    before = pdf.read_bytes()

    def fail(*args):
        raise failure

    monkeypatch.setattr(user, "verify_pdf_doi", fail)
    result = invoke(pdf, "--write-doi")
    assert result.exit_code == 1, result.output
    assert pdf.read_bytes() == before
    assert calls["upload"] == calls["write"] == []
    assert report(directory)["files"][0]["metadata_writeback"]["reason"] == str(failure)


def test_writer_failure_does_not_upload_original_and_next_file_continues(configured, monkeypatch):
    directory, calls = configured
    bad = make_pdf(directory / "bad.pdf")
    good = make_pdf(directory / "good.pdf", {"/Title": "Different document"})
    original_writer = user.write_verified_pdf_doi
    before = bad.read_bytes()

    def write(path, verification):
        if path == bad:
            raise OSError("backup could not be created")
        return original_writer(path, verification)

    monkeypatch.setattr(user, "write_verified_pdf_doi", write)
    result = invoke(directory, "--write-doi")
    assert result.exit_code == 1, result.output
    assert bad.read_bytes() == before
    assert [path for path, _ in calls["upload"]] == [good]
    rows = {Path(row["path"]).name: row for row in report(directory)["files"]}
    assert rows["bad.pdf"]["status"] == "failed"
    assert rows["bad.pdf"]["metadata_writeback"]["status"] == "failed"
    assert rows["good.pdf"]["status"] == "uploaded"


def test_writeback_failure_supersedes_old_upload_success_for_retry(configured, monkeypatch):
    directory, calls = configured
    pdf = make_pdf(directory / "paper.pdf")
    monkeypatch.setattr(user, "extract_doi_evidence", lambda path:
                        DoiEvidence("ok", DOI, "pdf-content", []))
    first = invoke(pdf)
    assert first.exit_code == 0, first.output
    assert len(calls["upload"]) == 1
    successful_verify = user.verify_pdf_doi
    monkeypatch.setattr(user, "verify_pdf_doi", lambda path, sha:
                        receipt(sha, decision="unverified", writeback_allowed=False,
                                reason="insufficient evidence"))
    failed = invoke(pdf, "--write-doi")
    assert failed.exit_code == 1, failed.output
    with UploadState() as state:
        assert state.path_status(pdf, "https://vault.test", hash_file=user.file_sha256) == "needs-review"
    monkeypatch.setattr(user, "verify_pdf_doi", successful_verify)
    retried = invoke(pdf, "--write-doi", "--retry-failed")
    assert retried.exit_code == 0, retried.output
    assert len(calls["upload"]) == 2
    assert report(directory)["files"][0]["metadata_writeback"]["status"] == "written"


def test_failure_report_keeps_a_backup_created_before_replace_failure(configured, monkeypatch):
    directory, calls = configured
    pdf = make_pdf(directory / "paper.pdf")
    original = pdf.read_bytes()
    backup = directory / "retained-original.bak"
    def fail_after_backup(*args):
        backup.write_bytes(original)
        failure = OSError("atomic replacement failed")
        failure.pdf_writeback_backup = str(backup)
        raise failure
    monkeypatch.setattr(user, "write_verified_pdf_doi", fail_after_backup)
    result = invoke(pdf, "--write-doi")
    assert result.exit_code == 1
    assert not calls["upload"]
    assert pdf.read_bytes() == original
    writeback = report(directory)["files"][0]["metadata_writeback"]
    assert Path(writeback["backup"]).read_bytes() == original


def test_existing_unique_info_doi_is_retained_without_verification(configured, monkeypatch):
    directory, calls = configured
    pdf = make_pdf(directory / "paper.pdf", {"/DOI": DOI})
    before = pdf.read_bytes()
    # Existing signed metadata must remain usable through the ordinary upload path.
    monkeypatch.setattr(user, "check_pdf_writeback_safety", lambda path:
                        pytest.fail("must not attempt to modify an existing DOI"))
    result = invoke(pdf, "--write-doi")
    assert result.exit_code == 0, result.output
    assert pdf.read_bytes() == before
    assert calls["verify"] == calls["write"] == []
    assert len(calls["upload"]) == 1
    assert report(directory)["files"][0]["metadata_writeback"]["status"] == "unchanged"


@pytest.mark.parametrize("metadata", [{"/DOI": "not a valid DOI"},
                                      {"/DOI": DOI, "/Subject": "10.1234/other"}])
def test_invalid_or_conflicting_info_doi_is_not_bypassed(configured, metadata):
    directory, calls = configured
    pdf = make_pdf(directory / "paper.pdf", metadata)
    before = pdf.read_bytes()
    result = invoke(pdf, "--write-doi")
    assert result.exit_code == 1, result.output
    assert pdf.read_bytes() == before
    assert calls["verify"] == calls["write"] == calls["upload"] == []
    assert report(directory)["files"][0]["status"] == "needs-review"


def test_duplicate_originals_are_written_individually_before_new_hash_dedup(configured):
    directory, calls = configured
    first = make_pdf(directory / "first.pdf")
    second = directory / "second.pdf"
    second.write_bytes(first.read_bytes())
    result = invoke(directory, "--write-doi")
    assert result.exit_code == 0, result.output
    assert {path for path, _ in calls["verify"]} == {first, second}
    assert set(calls["write"]) == {first, second}
    assert len(calls["upload"]) == 1
    assert first.read_bytes() == second.read_bytes()
    rows = report(directory)["files"]
    assert {row["status"] for row in rows} == {"uploaded", "local-duplicate"}
    assert all(row["metadata_writeback"]["status"] == "written" for row in rows)


def test_cached_manual_hint_and_automatic_csv_cannot_supply_writeback_evidence(configured, monkeypatch):
    directory, calls = configured
    pdf = make_pdf(directory / "paper.pdf")
    original_sha = user.file_sha256(pdf)
    user.save_identity_cache({original_sha: {
        "status": "confirmed", "source": "user", "doi": "10.1234/wrong-cache",
        "resolver_version": user.DOI_RESOLVER_VERSION,
    }})
    (directory / "cli-downloads.csv").write_text(
        "file,status,doi,bytes\n"
        f"paper.pdf,downloaded,10.1234/wrong-index,{pdf.stat().st_size}\n"
    )
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (True, ("pdf-download-index.json",)))
    result = invoke(pdf, "--write-doi")
    assert result.exit_code == 0, result.output
    assert calls["verify"] == [(pdf, original_sha)]
    assert read_pdf_metadata(pdf)["doi"] == DOI
    assert calls["upload"][0][1]["doi"] == DOI
    assert calls["upload"][0][1]["doi_source"] == "pdf-metadata"


@pytest.mark.parametrize("options", [["--no-crossref"], ["--doi", DOI], ["--csv", "pairs.csv"]])
def test_writeback_flags_are_mutually_exclusive_before_login(configured, monkeypatch, options):
    directory, _ = configured
    pdf = make_pdf(directory / "paper.pdf")
    (directory / "pairs.csv").write_text("file,doi,bytes\n")
    monkeypatch.setattr(user, "ensure_logged_in", lambda: pytest.fail("must not log in"))
    result = invoke(pdf, "--write-doi", *options)
    assert result.exit_code == 2, result.output
    assert "cannot be combined" in result.output
    assert not (directory / "nv-upload-report.json").exists()


def test_writeback_dry_run_never_authenticates_verifies_or_writes(configured, monkeypatch):
    directory, calls = configured
    pdf = make_pdf(directory / "paper.pdf", {"/Title": "A local candidate"})
    before_files = {path.name: path.read_bytes() for path in directory.iterdir()}
    monkeypatch.setattr(user, "ensure_logged_in", lambda: pytest.fail("must not log in"))
    result = invoke(pdf, "--write-doi", "--dry-run", "--retry-failed")
    assert result.exit_code == 0, result.output
    assert "online verification was not performed" in result.output
    assert calls == {"verify": [], "write": [], "upload": [], "exists": []}
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before_files


def test_default_upload_does_not_attempt_writeback(configured, monkeypatch):
    directory, calls = configured
    pdf = make_pdf(directory / "paper.pdf", {"/DOI": DOI})
    before = pdf.read_bytes()
    monkeypatch.setattr(user, "read_pdf_metadata", lambda path:
                        pytest.fail("ordinary upload must not enter the writeback flow"))
    result = invoke(pdf)
    assert result.exit_code == 0, result.output
    assert pdf.read_bytes() == before
    assert calls["verify"] == calls["write"] == []
    assert "metadata_writeback" not in report(directory)["files"][0]


def test_upload_report_retains_writeback_after_later_outcomes(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf")
    saved = tmp_path / "report.json"
    upload_report = UploadReport(saved, [pdf])
    evidence = {"status": "written", "doi": DOI, "backup": "paper.pdf.bak"}
    upload_report.record_metadata_writeback(pdf, evidence)
    upload_report.record(pdf, "local-duplicate", sha256="a" * 64)
    upload_report.record(pdf, "uploaded", pdf={"doi": DOI})
    upload_report.checkpoint(force=True, finished=True)
    assert json.loads(saved.read_text())["files"][0]["metadata_writeback"] == evidence


def test_force_hash_recomputes_even_when_directory_signature_matches(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf")
    with UploadState() as state:
        assert state.hash_file(pdf, lambda _: "stale") == ("stale", False)
        assert state.hash_file(pdf, lambda _: "fresh") == ("stale", True)
        assert state.hash_file(pdf, lambda _: "fresh", force=True) == ("fresh", False)


def test_verification_http_sends_exact_hash_and_pdf_with_authentication(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf")
    sha = user.file_sha256(pdf)
    monkeypatch.setattr(http, "auth_headers", lambda: {"Authorization": "Bearer secret"})
    monkeypatch.setattr(http, "server_url", lambda: "https://vault.test")

    def post(url, **kwargs):
        assert url == "https://vault.test/pdfs/verify-doi" and kwargs["timeout"] == 300
        assert kwargs["headers"]["Authorization"] == "Bearer secret"
        body = kwargs["data"].read()
        assert b'name="expected_sha256"' in body and sha.encode() in body
        assert pdf.read_bytes() in body
        return SimpleNamespace(ok=True, status_code=200, json=lambda: receipt(sha))

    monkeypatch.setattr(http.requests, "post", post)
    assert http.verify_pdf_doi(pdf, sha)["input_sha256"] == sha


def test_verification_http_only_retries_connection_failures(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf")
    calls = []
    monkeypatch.setattr(http, "sleep", lambda _: None)

    def disconnected(path, sha):
        calls.append(path)
        if len(calls) < 3:
            raise requests.ConnectionError("cannot connect")
        return receipt(sha)

    monkeypatch.setattr(http, "_verify_pdf_doi_once", disconnected)
    assert http.verify_pdf_doi(pdf, "a" * 64)["decision"] == "verified"
    assert len(calls) == 3
    for failure in (requests.ReadTimeout("slow"), http.ApiError(503, "unavailable")):
        calls.clear()

        def fail(path, sha):
            calls.append(path)
            raise failure

        monkeypatch.setattr(http, "_verify_pdf_doi_once", fail)
        with pytest.raises(type(failure)):
            http.verify_pdf_doi(pdf, "a" * 64)
        assert len(calls) == 1


def test_verification_http_404_explains_required_server_version(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf")
    monkeypatch.setattr(http, "auth_headers", lambda: {})
    monkeypatch.setattr(http, "server_url", lambda: "https://vault.test")
    monkeypatch.setattr(http.requests, "post", lambda *a, **k:
                        SimpleNamespace(ok=False, status_code=404))
    with pytest.raises(http.ApiError, match="0.7.66"):
        http.verify_pdf_doi(pdf, "a" * 64)


def test_upload_http_binds_received_bytes_to_expected_sha256(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf")
    sha = user.file_sha256(pdf)
    monkeypatch.setattr(http, "auth_headers", lambda: {})
    monkeypatch.setattr(http, "server_url", lambda: "https://vault.test")

    def post(url, **kwargs):
        assert kwargs["headers"]["Idempotency-Key"] == sha
        assert kwargs["headers"]["X-NetVault-Expected-SHA256"] == sha
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"deduplicated": False})

    monkeypatch.setattr(http, "http_session", lambda: SimpleNamespace(post=post))
    assert http.upload_pdf(pdf, sha256=sha)["deduplicated"] is False
