from pathlib import Path
import logging
import hashlib
import json

import pytest
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
from netvault.cli.update import (
    build_update_command,
    github_repository_slug,
    latest_release_tag,
    release_package_url,
)
from netvault.cli.upload_index import (
    DownloadIndexError,
    DownloadIndexResolver,
    load_download_index,
)
from typer.testing import CliRunner
import netvault.cli.config as cli_config
import netvault.cli.update as update_cli
import netvault.cli.user as user_cli
import netvault.doi
from netvault.doi import DoiEvidence, extract_doi_evidence


@pytest.fixture(autouse=True)
def default_upload_index_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        user_cli,
        "load_upload_index_settings",
        lambda: (True, ("pdf-download-index.json",)),
    )


def write_download_index(
    path: Path,
    pdf: Path,
    doi: str,
    *,
    filename: str | None = None,
    sha256: str | None = None,
    validation_status: str = "valid",
    validation_reason: str | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "updatedAt": "2026-07-13T04:58:31.935Z",
                "algorithm": "SHA-256",
                "records": [
                    {
                        "doi": doi,
                        "filename": filename or pdf.name,
                        "size": pdf.stat().st_size,
                        "lastModified": 1783918710583,
                        "sha256": sha256 or file_sha256(pdf),
                        "downloadedAt": "2026-07-13T04:58:30.601Z",
                        "sourceUrl": f"/doi/pdfdirect/{doi}?download=true",
                        "validation": {
                            "status": validation_status,
                            "checkedAt": "2026-07-13T04:58:30.579Z",
                            "method": "pdf-signature-eof",
                            "reason": validation_reason,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_pypdf_logs_are_quiet_for_cli_upload() -> None:
    assert logging.getLogger("pypdf").getEffectiveLevel() >= logging.CRITICAL
    assert netvault.doi.PdfReader is not None


def test_doi_suffix_accepts_ampersand() -> None:
    doi = "10.1207/s15327663jcp1001&2_01"

    assert netvault.doi.normalize_doi(doi) == doi
    assert netvault.doi.find_dois_in_text(f"DOI: {doi}") == [doi]


def test_doi_suffix_preserves_additional_slashes() -> None:
    doi = "10.1234/456abc/zyz"

    assert netvault.doi.normalize_doi(doi) == doi
    assert netvault.doi.find_dois_in_text(f"https://doi.org/{doi}") == [doi]


def test_resolver_cache_version_invalidates_old_automatic_results() -> None:
    assert netvault.doi.DOI_RESOLVER_VERSION == 3


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


def test_update_resolves_and_pins_latest_github_release(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"tag_name": "v0.7.13"}

    monkeypatch.setattr("netvault.cli.update.requests.get", lambda *args, **kwargs: Response())

    repo_url = "https://github.com/iihciyekub/netvault.git"
    assert github_repository_slug(repo_url) == "iihciyekub/netvault"
    assert latest_release_tag(repo_url) == "v0.7.13"
    assert release_package_url(repo_url, "v0.7.13") == (
        "https://github.com/iihciyekub/netvault/releases/download/"
        "v0.7.13/netvault-0.7.13-py3-none-any.whl"
    )


def test_update_leaves_non_github_repository_unpinned() -> None:
    repo_url = "https://git.example.com/team/netvault.git"

    assert github_repository_slug(repo_url) is None
    assert latest_release_tag(repo_url) is None
    assert release_package_url(repo_url, None) == f"git+{repo_url}"


def test_update_rejects_invalid_github_release_response(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list:
            return []

    monkeypatch.setattr("netvault.cli.update.requests.get", lambda *args, **kwargs: Response())

    with pytest.raises(RuntimeError, match="invalid latest release response"):
        latest_release_tag("https://github.com/iihciyekub/netvault.git")


def test_update_from_github_installs_the_resolved_release(monkeypatch) -> None:
    package_urls: list[str] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(update_cli, "latest_release_tag", lambda _url: "v0.7.13")
    monkeypatch.setattr(
        update_cli,
        "build_update_command",
        lambda package_url: package_urls.append(package_url) or ["installer", package_url],
    )
    monkeypatch.setattr(
        update_cli.subprocess,
        "run",
        lambda command, check: commands.append(command),
    )

    update_cli.update_from_github()

    assert package_urls == [
        "https://github.com/iihciyekub/netvault/releases/download/"
        "v0.7.13/netvault-0.7.13-py3-none-any.whl"
    ]
    assert commands == [["installer", package_urls[0]]]


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


def test_download_index_loads_existing_version_one_structure(tmp_path: Path) -> None:
    pdf = tmp_path / "10.1002_mar.20228.pdf"
    pdf.write_bytes(b"%PDF-1.4\nno embedded DOI\n%%EOF\n")
    index_path = tmp_path / "pdf-download-index.json"
    write_download_index(index_path, pdf, "10.1002/mar.20228")

    index = load_download_index(index_path)
    match = index.resolve(pdf, file_sha256(pdf), pdf.stat().st_size)

    assert match is not None
    assert match.record.doi == "10.1002/mar.20228"
    assert match.record.validation_method == "pdf-signature-eof"
    assert match.renamed is False


def test_download_index_accepts_ampersand_in_doi(tmp_path: Path) -> None:
    pdf = tmp_path / "10.1207_s15327663jcp1001&2_01.pdf"
    pdf.write_bytes(b"%PDF-1.4\nno embedded DOI\n%%EOF\n")
    index_path = tmp_path / "pdf-download-index.json"
    doi = "10.1207/s15327663jcp1001&2_01"
    write_download_index(index_path, pdf, doi)

    index = load_download_index(index_path)
    match = index.resolve(pdf, file_sha256(pdf), pdf.stat().st_size)

    assert match is not None
    assert match.record.doi == doi


def test_download_index_names_are_configurable(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[upload.index]\nenabled = true\nnames = ["custom-index.json"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_config, "CONFIG_PATH", config_path)

    assert cli_config.load_upload_index_settings() == (True, ("custom-index.json",))


def test_download_index_matches_renamed_pdf_by_sha256(tmp_path: Path) -> None:
    original = tmp_path / "original.pdf"
    renamed = tmp_path / "renamed.pdf"
    original.write_bytes(b"%PDF-1.4\nno embedded DOI\n%%EOF\n")
    index_path = tmp_path / "pdf-download-index.json"
    write_download_index(index_path, original, "10.1234/renamed")
    original.rename(renamed)

    resolver = DownloadIndexResolver((index_path.name,))
    match = resolver.resolve(renamed, file_sha256(renamed), renamed.stat().st_size)

    assert match is not None
    assert match.record.doi == "10.1234/renamed"
    assert match.renamed is True


def test_download_index_rejects_stale_same_filename_record(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\ncurrent bytes\n%%EOF\n")
    index_path = tmp_path / "pdf-download-index.json"
    write_download_index(index_path, pdf, "10.1234/stale", sha256="a" * 64)

    resolver = DownloadIndexResolver((index_path.name,))

    with pytest.raises(DownloadIndexError, match="SHA-256 mismatch"):
        resolver.resolve(pdf, file_sha256(pdf), pdf.stat().st_size)


def test_download_index_rejects_non_valid_download(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\ncurrent bytes\n%%EOF\n")
    index_path = tmp_path / "pdf-download-index.json"
    write_download_index(
        index_path,
        pdf,
        "10.1234/invalid",
        validation_status="invalid",
        validation_reason="missing EOF marker",
    )

    resolver = DownloadIndexResolver((index_path.name,))

    with pytest.raises(DownloadIndexError, match="missing EOF marker"):
        resolver.resolve(pdf, file_sha256(pdf), pdf.stat().st_size)


def test_upload_uses_sibling_download_index_without_parsing_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("netvault.cli.user.HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr("netvault.cli.user.IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user_cli, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user_cli, "get_existing_pdfs_by_sha256", lambda *args, **kwargs: {})
    monkeypatch.setattr(user_cli, "get_existing_pdfs_by_doi", lambda *args, **kwargs: {})

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nno embedded DOI\n%%EOF\n")
    write_download_index(
        tmp_path / "pdf-download-index.json",
        pdf,
        "10.1234/from.index",
    )

    def fail_extract(*args, **kwargs):
        raise AssertionError("download index hit should skip PDF DOI extraction")

    monkeypatch.setattr(user_cli, "extract_doi_evidence", fail_extract)
    uploads: list[dict] = []

    def fake_upload(path, **kwargs):
        uploads.append({"path": path, **kwargs})
        return {
            "pdf": {
                "doi": kwargs["doi"],
                "sha256": kwargs["sha256"],
                "original_name": path.name,
                "title": "Indexed paper",
            },
            "deduplicated": False,
        }

    monkeypatch.setattr(user_cli, "upload_pdf", fake_upload)

    result = CliRunner().invoke(user_cli.app, ["upload", str(pdf)])

    assert result.exit_code == 0, result.output
    assert "1 download index hits" in result.output
    assert "DOI scans" not in result.output
    assert uploads[0]["doi"] == "10.1234/from.index"
    assert uploads[0]["doi_source"] == "download-index"


def test_upload_does_not_fall_back_when_indexed_filename_hash_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("netvault.cli.user.HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr("netvault.cli.user.IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user_cli, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user_cli, "get_existing_pdfs_by_sha256", lambda *args, **kwargs: {})
    monkeypatch.setattr(user_cli, "get_existing_pdfs_by_doi", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        user_cli,
        "extract_doi_evidence",
        lambda *args, **kwargs: pytest.fail("stale index must not fall back to PDF extraction"),
    )

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nDOI: 10.1234/pdf.value\n%%EOF\n")
    write_download_index(
        tmp_path / "pdf-download-index.json",
        pdf,
        "10.1234/index.value",
        sha256="b" * 64,
    )

    result = CliRunner().invoke(user_cli.app, ["upload", str(pdf)])

    assert result.exit_code == 1, result.output
    assert "SHA-256 mismatch" in result.output
    assert "indexed file paper.pdf" in result.output
    assert "DOI scans" not in result.output


def test_upload_no_index_uses_existing_pdf_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("netvault.cli.user.HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr("netvault.cli.user.IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user_cli, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user_cli, "get_existing_pdfs_by_sha256", lambda *args, **kwargs: {})
    doi_checks = []

    def existing_by_doi(dois, **kwargs):
        doi_checks.append(list(dois))
        return {"10.1234/pdf.value": {"doi": "10.1234/pdf.value"}}

    monkeypatch.setattr(user_cli, "get_existing_pdfs_by_doi", existing_by_doi)

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nDOI: 10.1234/pdf.value\n%%EOF\n")
    write_download_index(
        tmp_path / "pdf-download-index.json",
        pdf,
        "10.1234/index.value",
    )
    monkeypatch.setattr(
        user_cli,
        "extract_doi_evidence",
        lambda *args, **kwargs: DoiEvidence(
            "ok", "10.1234/pdf.value", "pdf-content", [], None
        ),
    )
    uploads: list[dict] = []

    def fake_upload(path, **kwargs):
        uploads.append({"path": path, **kwargs})
        return {
            "pdf": {
                "doi": kwargs["doi"],
                "sha256": kwargs["sha256"],
                "original_name": path.name,
                "title": "PDF-resolved paper",
            },
            "deduplicated": False,
        }

    monkeypatch.setattr(user_cli, "upload_pdf", fake_upload)

    result = CliRunner().invoke(user_cli.app, ["upload", str(pdf), "--no-index"])

    assert result.exit_code == 0, result.output
    assert "1 DOI scans" in result.output
    assert "download index hits" not in result.output
    assert uploads[0]["doi"] == "10.1234/pdf.value"
    assert uploads[0]["doi_source"] == "pdf-content"
    assert doi_checks == [[]]


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


def test_upload_collapses_local_duplicate_paths_before_server_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("netvault.cli.user.HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr("netvault.cli.user.IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user_cli, "ensure_logged_in", lambda: None)
    content = b"%PDF-1.4\nDOI: 10.1234/local.duplicate\n%%EOF\n"
    first = tmp_path / "first.pdf"
    second = tmp_path / "nested" / "second.pdf"
    second.parent.mkdir()
    first.write_bytes(content)
    second.write_bytes(content)
    sha256 = hashlib.sha256(content).hexdigest()
    existing = {
        "doi": "10.1234/local.duplicate",
        "sha256": sha256,
        "original_name": "stored.pdf",
        "title": "Stored",
    }
    server_checks: list[list[str]] = []

    def existing_by_sha(hashes, **kwargs):
        checked = list(hashes)
        server_checks.append(checked)
        return {sha256: existing}

    monkeypatch.setattr(user_cli, "get_existing_pdfs_by_sha256", existing_by_sha)
    monkeypatch.setattr(
        user_cli,
        "extract_doi_evidence",
        lambda *args, **kwargs: pytest.fail("existing unique PDF must skip DOI extraction"),
    )
    monkeypatch.setattr(
        user_cli,
        "upload_pdf",
        lambda *args, **kwargs: pytest.fail("existing unique PDF must not be uploaded"),
    )

    result = CliRunner().invoke(user_cli.app, ["upload", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert server_checks == [[sha256]]
    assert "found paths: 2" in result.output
    assert "unique PDFs: 1" in result.output
    assert "local duplicate paths: 1" in result.output
    assert "already stored: 1 skipped" in result.output
    assert "uploaded: 0" in result.output


def test_upload_force_bypasses_duplicate_skips_and_replaces(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("netvault.cli.user.HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr("netvault.cli.user.IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user_cli, "ensure_logged_in", lambda: None)
    existing = {
        "doi": "10.1234/force",
        "sha256": "a" * 64,
        "original_name": "old.pdf",
        "title": "Old",
    }
    monkeypatch.setattr(
        user_cli,
        "get_existing_pdfs_by_sha256",
        lambda *args, **kwargs: {next(iter(args[0])): existing},
    )
    monkeypatch.setattr(
        user_cli,
        "get_existing_pdfs_by_doi",
        lambda *args, **kwargs: {"10.1234/force": existing},
    )
    uploaded: list[dict] = []

    def fake_upload(path, **kwargs):
        uploaded.append({"path": path, **kwargs})
        return {
            "pdf": {**existing, "original_name": path.name, "title": "Fresh"},
            "deduplicated": False,
            "replaced": True,
        }

    monkeypatch.setattr(user_cli, "upload_pdf", fake_upload)
    pdf = tmp_path / "new.pdf"
    pdf.write_bytes(b"%PDF-1.4\nDOI: 10.1234/force\n%%EOF\n")

    result = CliRunner().invoke(
        user_cli.app,
        ["upload", str(pdf), "--force"],
    )

    assert result.exit_code == 0, result.output
    assert "1 replaced" in result.output
    assert len(uploaded) == 1
    assert uploaded[0]["force"] is True
    assert uploaded[0]["doi"] == "10.1234/force"


def test_upload_force_rejects_no_crossref(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(user_cli, "ensure_logged_in", lambda: None)
    pdf = tmp_path / "new.pdf"
    pdf.write_bytes(b"%PDF-1.4\nDOI: 10.1234/force\n%%EOF\n")

    no_crossref = CliRunner().invoke(
        user_cli.app,
        ["upload", str(pdf), "--force", "--no-crossref"],
    )

    assert no_crossref.exit_code == 2
    assert "--force cannot be combined with --no-crossref" in no_crossref.output


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


def test_filename_candidate_is_reconciled_with_nested_slash_pdf_doi(tmp_path: Path) -> None:
    pdf = tmp_path / "10_25300_misq_2025_18946.pdf"
    pdf.write_bytes(
        b"%PDF-1.4\n"
        b"DOI: 10.25300/MISQ/2025/18946\n"
        b"%%EOF\n"
    )

    evidence = extract_doi_evidence(pdf)

    assert evidence.status == "ok"
    assert evidence.doi == "10.25300/misq/2025/18946"
    assert evidence.source == "pdf-content"
    assert {candidate.doi for candidate in evidence.candidates} == {
        "10.25300/misq/2025/18946",
        "10.25300/misq_2025_18946",
    }


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


def test_smart_doi_reads_document_info_metadata(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Subject": "DOI: 10.1234/document.info"})
    with pdf.open("wb") as handle:
        writer.write(handle)

    evidence = extract_doi_evidence(pdf)

    assert evidence.status == "ok"
    assert evidence.doi == "10.1234/document.info"
    assert evidence.source == "pdf-metadata"
    assert any(candidate.detail == "document-info:/Subject" for candidate in evidence.candidates)


def test_smart_doi_rejects_publisher_download_url_suffix(tmp_path: Path) -> None:
    pdf = tmp_path / "mbr-10-2023-0163en.pdf"
    pdf.write_bytes(
        b"%PDF-1.4\n"
        b"DOI 10.1108/MBR-10-2023-0163\n"
        b"Downloaded from http://www.emerald.com/mbr/article-pdf/doi/"
        b"10.1108/MBR-10-2023-0163/11288746/mbr-10-2023-0163en.pdf\n"
        b"%%EOF\n"
    )

    evidence = extract_doi_evidence(pdf)

    assert evidence.status == "ok"
    assert evidence.doi == "10.1108/mbr-10-2023-0163"
    assert any(candidate.embedded_publisher_url for candidate in evidence.candidates)
