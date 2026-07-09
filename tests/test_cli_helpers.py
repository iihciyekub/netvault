from pathlib import Path
import logging

from netvault.cli.user import (
    cached_file_sha256,
    file_sha256,
    find_existing_pdf_before_upload,
    has_pdf_header,
    load_hash_cache,
    save_hash_cache,
    collect_dois,
    collect_pdf_paths,
    unique_destination,
)
from netvault.cli.update import build_update_command
import netvault.doi
from netvault.doi import extract_doi_evidence


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


def test_has_pdf_header_rejects_html_saved_as_pdf(tmp_path: Path) -> None:
    html_pdf = tmp_path / "download.pdf"
    real_pdf = tmp_path / "paper.pdf"
    html_pdf.write_bytes(b"<!DOCTYPE html><title>Access denied</title>")
    real_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

    assert has_pdf_header(real_pdf) is True
    assert has_pdf_header(html_pdf) is False


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
