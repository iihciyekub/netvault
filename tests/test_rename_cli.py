import csv
import os

import pytest
from typer.testing import CliRunner

from netvault.cli import identify, rename, user


def make_index(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["doi", "sha256"])
        writer.writerows(rows)
    return path


def make_pdf(path, content=b"%PDF-example-bytes"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def report_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_offline_rename_reads_each_file_once_without_pdf_parsing_or_network(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "nested" / "source.PDF")
    unknown = make_pdf(tmp_path / "unknown.pdf", b"%PDF-other-bytes")
    digest = user.file_sha256(source)
    index = make_index(tmp_path / "inventory.csv", [("10.1234/a", digest)])
    calls = []
    original_hash = user.file_sha256

    def hash_file(path):
        calls.append(path)
        return original_hash(path)

    monkeypatch.setattr(user, "file_sha256", hash_file)
    monkeypatch.setattr(user, "ensure_logged_in", lambda: pytest.fail("Login requested"))
    monkeypatch.setattr(user, "api_post", lambda *a, **k: pytest.fail("Network requested"))
    monkeypatch.setattr(identify, "pdf_content_fingerprint", lambda *a: pytest.fail("PDF parsed"))
    result = CliRunner().invoke(user.app, ["rename", str(tmp_path), "--index", str(index)])
    assert result.exit_code == 0, result.output
    destination = source.with_name("10.1234_a.pdf")
    assert destination.read_bytes() == b"%PDF-example-bytes"
    assert not source.exists()
    assert unknown.exists()
    assert sorted(calls) == sorted([source, unknown])
    assert "Hash reads: 2; unchanged hashes reused: 1" in result.output
    rows = report_rows(tmp_path / "netvault-index.csv")
    assert {row["status"] for row in rows} == {"renamed", "not_found"}
    assert not (tmp_path / "duplicates").exists()
    assert not (tmp_path / "unidentifys").exists()


def test_offline_preview_and_occupied_destination_preserve_both_files(tmp_path):
    source = make_pdf(tmp_path / "source.pdf")
    index = make_index(tmp_path / "inventory.csv", [("10.1234/a", user.file_sha256(source))])
    runner = CliRunner()
    preview = runner.invoke(user.app, ["rename", str(tmp_path), "--index", str(index), "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert source.exists()
    assert report_rows(tmp_path / "netvault-index.csv")[0]["status"] == "would_rename"
    destination = make_pdf(tmp_path / "10.1234_a.pdf", b"occupied")
    applied = runner.invoke(user.app, ["rename", str(tmp_path), "--index", str(index)])
    assert applied.exit_code == 1
    assert source.exists() and destination.read_bytes() == b"occupied"
    assert "conflict" in applied.output
    assert (tmp_path / "netvault-index-2.csv").exists()


@pytest.mark.parametrize("rows", [
    [("10.1234/a", "bad")],
    [("invalid-doi", "a" * 64)],
    [("10.1234/a", "a" * 64), ("10.1234/b", "a" * 64)],
])
def test_invalid_or_ambiguous_index_fails_before_hashing_or_renaming(tmp_path, monkeypatch, rows):
    source = make_pdf(tmp_path / "source.pdf")
    index = make_index(tmp_path / "inventory.csv", rows)
    monkeypatch.setattr(user, "file_sha256", lambda path: pytest.fail("PDF read before validation"))
    result = CliRunner().invoke(user.app, ["rename", str(tmp_path), "--index", str(index)])
    assert result.exit_code == 1
    assert "rename failed" in result.output
    assert source.exists()
    assert not (tmp_path / "netvault-index.csv").exists()


def test_index_accepts_reversed_columns_uppercase_hashes_and_identical_repeats(tmp_path):
    path = tmp_path / "inventory.csv"
    path.write_text("sha256,doi\n" + ("A" * 64 + ",10.1234/a\n") * 2)
    assert rename.load_hash_index(path) == {"a" * 64: {"doi": "10.1234/a"}}
    path.write_text("doi,sha256\n")
    assert rename.load_hash_index(path) == {}
    path.write_text("filename,doi,sha256\n")
    with pytest.raises(ValueError, match="doi,sha256"):
        rename.load_hash_index(path)


def test_issn_mode_downloads_inventory_once_before_local_matching(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf")
    digest = user.file_sha256(source)
    downloads, logins = [], []

    def download(issn, destination):
        assert issn == "0025-1909"
        downloads.append(destination)
        make_index(destination, [("10.1234/a", digest)])
        return 1

    monkeypatch.setattr(user, "ensure_logged_in", lambda: logins.append(True))
    monkeypatch.setattr(user, "save_issn_export", download)
    monkeypatch.setattr(user, "api_post", lambda *a, **k: pytest.fail("Bulk lookup requested"))
    result = CliRunner().invoke(user.app, ["rename", str(tmp_path), "--issn", "00251909"])
    assert result.exit_code == 0, result.output
    assert len(downloads) == 1 and len(logins) == 1
    assert not downloads[0].exists()
    assert source.with_name("10.1234_a.pdf").exists()


def test_server_mode_uses_strict_bulk_lookup_and_supports_alias_hashes(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf")
    digest = user.file_sha256(source)
    calls = []
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)

    def post(path, json):
        calls.append(path)
        assert json == {"sha256": [digest]}
        return {"existing": {digest: {"doi": "10.1234/a", "sha256": "f" * 64}}}

    monkeypatch.setattr(user, "api_post", post)
    monkeypatch.setattr(user, "get_existing_pdf_by_sha256", lambda *a: pytest.fail("Serial fallback"))
    result = CliRunner().invoke(user.app, ["rename", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert calls == ["/pdfs/exists"]
    assert source.with_name("10.1234_a.pdf").exists()
    assert report_rows(tmp_path / "netvault-index.csv")[0]["sha256"] == digest


def test_changed_pdf_with_restored_mtime_is_rehashed_and_left_in_place(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf", b"%PDF-first-content")
    digest = user.file_sha256(source)
    index = make_index(tmp_path / "inventory.csv", [("10.1234/a", digest)])
    original_lookup = rename.index_lookup

    def lookup(records):
        lookup_hashes = original_lookup(records)

        def resolve(hashes):
            matches = lookup_hashes(hashes)
            stat = source.stat()
            source.write_bytes(b"%PDF-other-content")
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return matches

        return resolve

    monkeypatch.setattr(user, "index_lookup", lookup)
    result = CliRunner().invoke(user.app, ["rename", str(tmp_path), "--index", str(index)])
    assert result.exit_code == 1
    assert source.read_bytes() == b"%PDF-other-content"
    assert not source.with_name("10.1234_a.pdf").exists()
    assert "Hash reads: 2; unchanged hashes reused: 0" in result.output
    assert "File changed" in report_rows(tmp_path / "netvault-index.csv")[0]["error"]


def test_invalid_options_fail_without_network_or_file_operations(tmp_path, monkeypatch):
    index = make_index(tmp_path / "inventory.csv", [])
    monkeypatch.setattr(user, "ensure_logged_in", lambda: pytest.fail("Login requested"))
    runner = CliRunner()
    both = runner.invoke(user.app, ["rename", str(tmp_path), "--index", str(index), "--issn", "0025-1909"])
    assert both.exit_code == 2
    invalid = runner.invoke(user.app, ["rename", str(tmp_path), "--issn", "0025-1900"])
    assert invalid.exit_code == 1
    assert "Invalid ISSN" in invalid.output
    assert not (tmp_path / "netvault-index.csv").exists()
