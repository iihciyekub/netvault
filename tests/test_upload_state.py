import json
import shutil

import pytest

from netvault.cli import user
from netvault.cli.sync import synchronize
from netvault.cli.upload_state import STATE_NAME, UploadState
from test_upload_memory import run_upload, setup_upload as upload_fixture

setup_upload = upload_fixture


def test_directory_rejection_survives_cache_report_removal_and_release_upgrade(setup_upload, monkeypatch):
    pdf, state = setup_upload
    assert run_upload(pdf).exit_code == 1
    for name in ("identity-cache.json", "hash-cache.json", "nv-upload-report.json"):
        (pdf.parent / name).unlink()
    monkeypatch.setattr(user, "__version__", "999.0.0")
    state["server_version"] = "999.0.0"
    result = run_upload(pdf)
    assert result.exit_code == 0, result.output
    assert len(state["uploads"]) == state["scans"] == 1
    assert "previously rejected: 1 skipped" in result.output
    with UploadState() as ledger:
        row = ledger.get(pdf, user.file_sha256(pdf), state["server"])
        assert row["status"] == "needs-review" and row["attempts"] == 1


def test_retry_uses_directory_state_when_report_is_missing_or_overwritten(setup_upload):
    pdf, state = setup_upload
    assert run_upload(pdf).exit_code == 1
    second = pdf.with_name("second.pdf")
    second.write_bytes(b"%PDF-1.4\nsecond\n%%EOF")
    assert run_upload(second).exit_code == 1
    (pdf.parent / "nv-upload-report.json").unlink()
    state["error"] = None
    result = run_upload(pdf.parent, "--retry-failed")
    assert result.exit_code == 0, result.output
    assert len(state["uploads"]) == 4


def test_folder_copy_keeps_rejection_on_another_machine(setup_upload, monkeypatch, tmp_path):
    pdf, state = setup_upload
    assert run_upload(pdf).exit_code == 1
    target = tmp_path / "moved"
    target.mkdir()
    copied = target / "renamed.pdf"
    shutil.copy2(pdf, copied)
    shutil.copy2(pdf.parent / STATE_NAME, target / STATE_NAME)
    (pdf.parent / "identity-cache.json").unlink()
    monkeypatch.chdir(target)
    result = run_upload(copied)
    assert result.exit_code == 0, result.output
    assert len(state["uploads"]) == state["scans"] == 1


def test_legacy_global_rejection_is_imported_without_uploading_again(setup_upload):
    pdf, state = setup_upload
    assert run_upload(pdf).exit_code == 1
    (pdf.parent / STATE_NAME).unlink()
    cache = user.load_identity_cache()
    sha = user.file_sha256(pdf)
    cache[sha]["upload_rejections"][state["server"]]["context"]["client_version"] = "0.0.1"
    user.save_identity_cache(cache)
    assert run_upload(pdf).exit_code == 0
    assert len(state["uploads"]) == 1
    assert (pdf.parent / STATE_NAME).exists()


def test_sync_distinguishes_doi_presence_from_exact_hash_and_accepts_aliases(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\narticle\n%%EOF")
    sha = user.file_sha256(pdf)
    responses = [{"existing_doi": {"10.1234/article": {"doi": "10.1234/article"}}, "existing": {}},
                 {"existing_doi": {}, "existing": {sha: {"doi": "10.1234/article", "sha256": "f" * 64}}}]
    def lookup(*args, **kwargs):
        return responses.pop(0)
    first = synchronize([tmp_path], [pdf], ["10.1234/article"], "https://vault.example/nv",
                        api_post=lookup, hash_file=user.file_sha256)
    assert first["doi"]["10.1234/article"]["present"] is True
    assert first["files"][0]["server_present"] is False
    assert first["summary"]["pending_upload"] == 1
    second = synchronize([tmp_path], [pdf], [], "https://vault.example/nv",
                         api_post=lookup, hash_file=user.file_sha256)
    assert second["files"][0]["sha256"] == sha
    assert second["files"][0]["server_present"] is True
    with UploadState() as state:
        assert state.get(pdf, sha, second["server"])["pdf"]["sha256"] == "f" * 64


def test_sync_outage_does_not_turn_cached_success_into_absence(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\narticle\n%%EOF")
    sha = user.file_sha256(pdf)
    with UploadState() as state:
        state.hash_file(pdf, user.file_sha256)
        state.record(pdf, sha, "https://vault.example/nv", "uploaded", pdf={"doi": "10.1234/article"})
    def unavailable(*args, **kwargs):
        raise TimeoutError("temporary outage")
    result = synchronize([tmp_path], [pdf], [], "https://vault.example/nv",
                         api_post=unavailable, hash_file=lambda p: pytest.fail("unchanged file rehashed"))
    assert result["status"] == "offline"
    assert result["files"][0]["server_present"] is None
    assert result["doi"]["10.1234/article"]["present"] is True
    assert result["doi"]["10.1234/article"]["source"] == "cached"


def test_committed_outcome_is_visible_before_writer_exits(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\narticle\n%%EOF")
    with UploadState() as writer:
        sha, _ = writer.hash_file(pdf, user.file_sha256)
        writer.record(pdf, sha, "https://vault.example", "needs-review", context={"doi": None}, error="no title")
        with UploadState() as reader:
            assert reader.get(pdf, sha, "https://vault.example")["error"] == "no title"


def test_public_cli_does_not_add_integration_commands():
    result = user.app.registered_commands
    assert "sync" not in {command.name for command in result}
    help_result = __import__("typer.testing", fromlist=["CliRunner"]).CliRunner().invoke(user.app, ["upload", "--help"])
    assert "retry-review" not in help_result.output


def test_report_links_directory_state(setup_upload):
    pdf, _ = setup_upload
    assert run_upload(pdf).exit_code == 1
    report = json.loads((pdf.parent / "nv-upload-report.json").read_text())
    assert report["state_files"] == [str(pdf.resolve().parent / STATE_NAME)]
