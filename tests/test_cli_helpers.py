from pathlib import Path
import logging
import hashlib
import json

from pypdf import PdfWriter
from netvault.cli.user import (
    cached_identity,
    cached_file_sha256,
    download_part_path,
    file_sha256,
    find_existing_pdf_before_upload,
    get_existing_pdfs_by_sha256,
    has_pdf_header,
    load_hash_cache,
    load_identity_cache,
    manual_identity,
    plan_download_destination,
    pdf_open_error,
    save_hash_cache,
    save_identity_cache,
    collect_dois,
    collect_pdf_paths,
    unique_destination,
)
from netvault.cli.update import build_update_command
from typer.testing import CliRunner
import netvault.cli.user as user_cli
import netvault.doi
from netvault.doi import DoiEvidence, extract_doi_evidence


def test_pypdf_logs_are_quiet_for_cli_upload() -> None:
    assert logging.getLogger("pypdf").getEffectiveLevel() >= logging.CRITICAL
    assert netvault.doi.PdfReader is not None


def test_update_command_uses_uv_for_uv_tool_install(monkeypatch) -> None:
    monkeypatch.setattr("netvault.cli.update.shutil.which", lambda name: "/opt/homebrew/bin/uv" if name == "uv" else None)

    command = build_update_command(
        "git+https://github.com/iihciyekub/netvault.git",
        "/Users/yjli/.local/share/uv/tools/netvault/bin/python",
    )

    assert command == [
        "/opt/homebrew/bin/uv",
        "tool",
        "install",
        "--force",
        "git+https://github.com/iihciyekub/netvault.git",
    ]


def test_update_command_uses_pipx_for_pipx_install(monkeypatch) -> None:
    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/pipx" if name == "pipx" else None

    monkeypatch.setattr("netvault.cli.update.shutil.which", fake_which)

    command = build_update_command(
        "git+https://github.com/iihciyekub/netvault.git",
        "/Users/yjli/.local/share/pipx/venvs/netvault/bin/python",
    )

    assert command == [
        "/usr/local/bin/pipx",
        "install",
        "--force",
        "git+https://github.com/iihciyekub/netvault.git",
    ]


def test_update_command_falls_back_to_current_python_pip(monkeypatch) -> None:
    monkeypatch.setattr("netvault.cli.update.shutil.which", lambda name: None)

    command = build_update_command("git+https://github.com/iihciyekub/netvault.git", "/tmp/venv/bin/python")

    assert command == [
        "/tmp/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--force-reinstall",
        "git+https://github.com/iihciyekub/netvault.git",
    ]


def test_list_command_json_output(monkeypatch) -> None:
    rows = [{"id": 1, "doi": "10.1234/test"}]
    monkeypatch.setattr(user_cli, "api_get", lambda *args, **kwargs: rows)

    result = CliRunner().invoke(user_cli.app, ["list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == rows


def test_collect_pdf_paths_recurses_and_deduplicates(tmp_path: Path) -> None:
    papers = tmp_path / "papers"
    nested = papers / "nested"
    nested.mkdir(parents=True)
    first = papers / "a.pdf"
    second = nested / "b.PDF"
    ignored = nested / "notes.txt"
    first.write_bytes(b"%PDF-1.4\n%%EOF\n")
    second.write_bytes(b"%PDF-1.4\n%%EOF\n")
    ignored.write_text("not a pdf", encoding="utf-8")

    assert collect_pdf_paths([papers, first]) == [first, second]


def test_collect_pdf_paths_prunes_excluded_directories(tmp_path: Path) -> None:
    papers = tmp_path / "papers"
    ignored_directory = papers / "node_modules" / "fixture"
    ignored_directory.mkdir(parents=True)
    included = papers / "included.pdf"
    ignored = ignored_directory / "ignored.pdf"
    included.write_bytes(b"%PDF-1.4\n%%EOF\n")
    ignored.write_bytes(b"%PDF-1.4\n%%EOF\n")

    assert collect_pdf_paths([papers], excluded_dir_names={"node_modules"}) == [included]
    assert collect_pdf_paths([ignored_directory], excluded_dir_names={"node_modules"}) == [ignored]


def test_has_pdf_header_rejects_html_saved_as_pdf(tmp_path: Path) -> None:
    html_pdf = tmp_path / "download.pdf"
    real_pdf = tmp_path / "paper.pdf"
    html_pdf.write_bytes(b"<!DOCTYPE html><title>Access denied</title>")
    real_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

    assert has_pdf_header(real_pdf) is True
    assert has_pdf_header(html_pdf) is False


def write_valid_pdf(path: Path, *, encrypted: bool = False) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("secret")
    with path.open("wb") as handle:
        writer.write(handle)


def test_pdf_open_error_accepts_valid_and_encrypted_pdfs(tmp_path: Path) -> None:
    valid = tmp_path / "valid.pdf"
    encrypted = tmp_path / "encrypted.pdf"
    invalid = tmp_path / "invalid.pdf"
    write_valid_pdf(valid)
    write_valid_pdf(encrypted, encrypted=True)
    invalid.write_bytes(b"not a PDF")

    assert pdf_open_error(valid) is None
    assert pdf_open_error(encrypted) is None
    assert pdf_open_error(invalid) is not None


def test_check_pdfs_moves_only_invalid_pdfs_to_cwd_error(tmp_path: Path, monkeypatch) -> None:
    working = tmp_path / "working"
    source = tmp_path / "source"
    nested = source / "nested"
    working.mkdir()
    nested.mkdir(parents=True)
    valid = source / "valid.pdf"
    invalid = nested / "broken.PDF"
    ignored = source / "notes.txt"
    write_valid_pdf(valid)
    invalid.write_bytes(b"%PDF-1.4\ntruncated")
    ignored.write_text("not a PDF", encoding="utf-8")
    monkeypatch.chdir(working)

    result = CliRunner().invoke(user_cli.app, ["check-pdfs", str(source)])

    assert result.exit_code == 0, result.output
    assert valid.exists()
    assert ignored.exists()
    assert not invalid.exists()
    assert (working / "error" / "broken.PDF").exists()
    assert "1 invalid moved" in result.output


def test_check_pdfs_avoids_collisions_and_skips_error_directory(tmp_path: Path, monkeypatch) -> None:
    error_directory = tmp_path / "error"
    error_directory.mkdir()
    existing = error_directory / "broken.pdf"
    existing.write_bytes(b"already quarantined")
    invalid = tmp_path / "broken.pdf"
    invalid.write_bytes(b"not a PDF")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(user_cli.app, ["check-pdfs"])

    assert result.exit_code == 0, result.output
    assert existing.read_bytes() == b"already quarantined"
    assert (error_directory / "broken-2.pdf").exists()
    assert "1 PDFs checked" in result.output


def test_check_pdfs_dry_run_does_not_move_files(tmp_path: Path, monkeypatch) -> None:
    invalid = tmp_path / "broken.pdf"
    invalid.write_bytes(b"not a PDF")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(user_cli.app, ["check-pdfs", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert invalid.exists()
    assert not (tmp_path / "error").exists()
    assert "would move" in result.output
    assert "dry run: 1 PDFs checked; 1 invalid" in result.output


def test_collect_dois_from_arguments_and_file(tmp_path: Path) -> None:
    doi_file = tmp_path / "dois.txt"
    doi_file.write_text(
        "See https://doi.org/10.1016/j.ijpe.2018.04.006 and 10.1234/example.test.",
        encoding="utf-8",
    )

    assert collect_dois(["doi:10.1234/example.test"], [doi_file]) == [
        "10.1234/example.test",
        "10.1016/j.ijpe.2018.04.006",
    ]


def test_unique_destination_avoids_overwriting(tmp_path: Path) -> None:
    existing = tmp_path / "paper.pdf"
    existing.write_bytes(b"already here")
    used: set[Path] = set()

    assert unique_destination(tmp_path, "paper.pdf", used) == tmp_path / "paper-2.pdf"
    assert unique_destination(tmp_path, "paper.pdf", used) == tmp_path / "paper-3.pdf"


def test_download_plan_skips_complete_file(tmp_path: Path) -> None:
    existing = tmp_path / "paper.pdf"
    existing.write_bytes(b"abc")
    used: set[Path] = set()

    destination, part_path, offset, complete = plan_download_destination(tmp_path, "paper.pdf", 3, used)

    assert destination == existing
    assert part_path == download_part_path(existing)
    assert offset == 3
    assert complete is True


def test_download_plan_resumes_part_file(tmp_path: Path) -> None:
    destination = tmp_path / "paper.pdf"
    part = download_part_path(destination)
    part.write_bytes(b"ab")
    used: set[Path] = set()

    planned_destination, part_path, offset, complete = plan_download_destination(tmp_path, "paper.pdf", 5, used)

    assert planned_destination == destination
    assert part_path == part
    assert offset == 2
    assert complete is False


def test_download_plan_restarts_oversized_part_file(tmp_path: Path) -> None:
    destination = tmp_path / "paper.pdf"
    part = download_part_path(destination)
    part.write_bytes(b"abcdef")
    used: set[Path] = set()

    planned_destination, part_path, offset, complete = plan_download_destination(tmp_path, "paper.pdf", 3, used)

    assert planned_destination == destination
    assert part_path == part
    assert offset == 0
    assert complete is False
    assert not part.exists()


def test_download_plan_does_not_skip_same_size_corrupt_file(tmp_path: Path) -> None:
    existing = tmp_path / "paper.pdf"
    expected = b"good"
    existing.write_bytes(b"evil")
    used: set[Path] = set()

    destination, _, offset, complete = plan_download_destination(
        tmp_path,
        "paper.pdf",
        len(expected),
        used,
        expected_sha256=hashlib.sha256(expected).hexdigest(),
    )

    assert destination == tmp_path / "paper-2.pdf"
    assert offset == 0
    assert complete is False


def test_hash_cache_reuses_unchanged_file(tmp_path: Path, monkeypatch) -> None:
    cache_path = tmp_path / "hash-cache.json"
    monkeypatch.setattr("netvault.cli.user.HASH_CACHE_PATH", cache_path)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nDOI: 10.1234/cache\n%%EOF\n")

    cache = load_hash_cache()
    sha256, from_cache = cached_file_sha256(pdf, cache)
    assert from_cache is False
    assert sha256 == file_sha256(pdf)
    save_hash_cache(cache)

    reloaded_cache = load_hash_cache()
    cached_sha256, cached = cached_file_sha256(pdf, reloaded_cache)
    assert cached is True
    assert cached_sha256 == sha256


def test_manual_identity_cache_survives_file_rename(tmp_path: Path, monkeypatch) -> None:
    hash_cache_path = tmp_path / "hash-cache.json"
    identity_cache_path = tmp_path / "identity-cache.json"
    monkeypatch.setattr("netvault.cli.user.HASH_CACHE_PATH", hash_cache_path)
    monkeypatch.setattr("netvault.cli.user.IDENTITY_CACHE_PATH", identity_cache_path)
    original = tmp_path / "original.pdf"
    renamed = tmp_path / "renamed.pdf"
    original.write_bytes(b"%PDF-1.4\nno embedded DOI\n%%EOF\n")

    sha256, _ = cached_file_sha256(original, {})
    save_identity_cache({sha256: manual_identity("10.1234/user.confirmed")})
    original.rename(renamed)
    renamed_sha256, _ = cached_file_sha256(renamed, {})
    identity = cached_identity(load_identity_cache(), renamed_sha256)

    assert renamed_sha256 == sha256
    assert identity is not None
    assert identity["doi"] == "10.1234/user.confirmed"
    assert identity["status"] == "confirmed"
    assert identity["source"] == "user"


def test_automatic_identity_expires_but_manual_identity_does_not(monkeypatch) -> None:
    sha256 = "a" * 64
    automatic = {
        "doi": "10.1234/automatic",
        "status": "ok",
        "source": "pdf-metadata",
        "resolver_version": 1,
    }
    confirmed = manual_identity("10.1234/confirmed")

    monkeypatch.setattr(user_cli, "DOI_RESOLVER_VERSION", 2)

    assert cached_identity({sha256: automatic}, sha256) is None
    assert cached_identity({sha256: confirmed}, sha256) == confirmed


def test_doi_set_show_and_remove_manage_sha_identity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("netvault.cli.user.HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr("netvault.cli.user.IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nno embedded DOI\n%%EOF\n")
    runner = CliRunner()

    saved = runner.invoke(user_cli.app, ["doi", str(pdf), "--set", "10.1234/manual"])
    shown = runner.invoke(user_cli.app, ["doi", str(pdf), "--show-cache"])
    removed = runner.invoke(user_cli.app, ["doi", str(pdf), "--remove"])
    missing = runner.invoke(user_cli.app, ["doi", str(pdf), "--show-cache"])

    assert saved.exit_code == 0, saved.output
    assert "source=user" in saved.output
    assert shown.exit_code == 0, shown.output
    assert "10.1234/manual" in shown.output
    assert "confirmed" in shown.output
    assert removed.exit_code == 0, removed.output
    assert missing.exit_code == 0, missing.output
    assert "no cached identity" in missing.output


def test_upload_reuses_manual_identity_without_parsing_pdf(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("netvault.cli.user.HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr("netvault.cli.user.IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user_cli, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user_cli, "get_existing_pdfs_by_sha256", lambda *args, **kwargs: {})
    existing = {
        "doi": "10.1234/manual",
        "sha256": "b" * 64,
        "original_name": "existing.pdf",
        "title": "Existing",
    }
    monkeypatch.setattr(
        user_cli,
        "get_existing_pdfs_by_doi",
        lambda *args, **kwargs: {"10.1234/manual": existing},
    )
    registered: list[tuple[str, str]] = []

    def capture_aliases(aliases):
        registered.extend(aliases)
        return {}

    monkeypatch.setattr(user_cli, "register_pdf_aliases", capture_aliases)

    def fail_extract(*args, **kwargs):
        raise AssertionError("manual identity should skip DOI extraction")

    monkeypatch.setattr(user_cli, "extract_doi_evidence", fail_extract)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nno embedded DOI\n%%EOF\n")
    sha256 = file_sha256(pdf)
    save_identity_cache({sha256: manual_identity("10.1234/manual")})

    result = CliRunner().invoke(user_cli.app, ["upload", str(pdf)])

    assert result.exit_code == 0, result.output
    assert "1 skipped" in result.output
    assert "1 DOI cache hits" in result.output
    assert "DOI scans" not in result.output
    assert registered == [(sha256, "10.1234/manual")]


def test_upload_caches_no_doi_result_until_refresh(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("netvault.cli.user.HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr("netvault.cli.user.IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user_cli, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user_cli, "get_existing_pdfs_by_sha256", lambda *args, **kwargs: {})
    monkeypatch.setattr(user_cli, "get_existing_pdfs_by_doi", lambda *args, **kwargs: {})
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nno embedded DOI\n%%EOF\n")
    monkeypatch.setattr(
        user_cli,
        "extract_doi_evidence",
        lambda *args, **kwargs: DoiEvidence("no-doi", None, None, [], "No DOI found"),
    )

    first = CliRunner().invoke(user_cli.app, ["upload", str(pdf)])

    assert first.exit_code == 1, first.output
    assert "1 DOI scans" in first.output

    def fail_extract(*args, **kwargs):
        raise AssertionError("cached no-doi result should skip DOI extraction")

    monkeypatch.setattr(user_cli, "extract_doi_evidence", fail_extract)
    second = CliRunner().invoke(user_cli.app, ["upload", str(pdf)])

    assert second.exit_code == 1, second.output
    assert "1 DOI cache hits" in second.output
    assert "DOI scans" not in second.output


def test_existing_sha_skips_doi_extraction(monkeypatch) -> None:
    def fail_extract(*args, **kwargs):
        raise AssertionError("DOI extraction should not run for an existing sha256")

    monkeypatch.setattr("netvault.cli.user.extract_local_doi", fail_extract)
    existing = {"sha256": "a" * 64, "doi": "10.1234/existing"}

    assert (
        find_existing_pdf_before_upload(
            Path("paper.pdf"),
            None,
            "a" * 64,
            {"a" * 64: existing},
        )
        == existing
    )


def test_server_precheck_reports_progress(monkeypatch) -> None:
    monkeypatch.setattr(
        user_cli,
        "api_post",
        lambda *args, **kwargs: {"existing": {"a" * 64: {"sha256": "a" * 64}}},
    )
    completed: list[int] = []

    existing = get_existing_pdfs_by_sha256(
        ["a" * 64, "b" * 64],
        progress_callback=completed.append,
    )

    assert list(existing) == ["a" * 64]
    assert completed == [2]


def test_smart_doi_prefers_filename_over_content_noise(tmp_path: Path) -> None:
    pdf = tmp_path / "10.1016_j.chb.2015.03.041.pdf"
    pdf.write_bytes(
        b"%PDF-1.4\n"
        b"References\n"
        b"https://doi.org/10.9999/reference.noise\n"
        b"%%EOF\n"
    )

    evidence = extract_doi_evidence(pdf)

    assert evidence.status == "ok"
    assert evidence.doi == "10.1016/j.chb.2015.03.041"
    assert evidence.source == "filename"


def test_filename_doi_fast_path_skips_text_extractors(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "10.1234_fast-path.pdf"
    pdf.write_bytes(b"%PDF-1.4\nno embedded metadata\n%%EOF\n")

    def fail_scan(*args, **kwargs):
        raise AssertionError("filename DOI fast path should not run pdftotext")

    def fail_reader(*args, **kwargs):
        raise AssertionError("filename DOI fast path should not run pypdf")

    monkeypatch.setattr(netvault.doi, "scan_with_pdftotext", fail_scan)
    monkeypatch.setattr(netvault.doi, "PdfReader", fail_reader)

    evidence = extract_doi_evidence(pdf)

    assert evidence.status == "ok"
    assert evidence.doi == "10.1234/fast-path"
    assert evidence.source == "filename"


def test_smart_doi_selects_labeled_content_candidate(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(
        b"%PDF-1.4\n"
        b"DOI: 10.1234/article.main\n"
        b"References\n"
        b"10.9999/reference.noise\n"
        b"%%EOF\n"
    )

    evidence = extract_doi_evidence(pdf)

    assert evidence.status == "ok"
    assert evidence.doi == "10.1234/article.main"
