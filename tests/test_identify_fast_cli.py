import csv
import os
from pathlib import Path
from threading import Event

import pytest
from typer.testing import CliRunner

from netvault.cli import hash_index, identify, identify_fast, user
from netvault.cli.identify_fast import fast_identify_pdfs
from test_pdf_fingerprint import make_pdf as make_content_pdf


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


def test_offline_identify_reads_each_file_once_without_pdf_parsing_or_network(tmp_path, monkeypatch):
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
    result = CliRunner().invoke(user.app, ["identify", str(tmp_path), "--rename", "--index", str(index)])
    assert result.exit_code == 0, result.output
    destination = source.with_name("10.1234_a.pdf")
    assert destination.read_bytes() == b"%PDF-example-bytes"
    assert not source.exists()
    assert unknown.exists()
    assert sorted(calls) == sorted([source, unknown])
    assert "Hash reads: 2; unchanged hashes reused: 1" in result.output
    rows = report_rows(tmp_path / "netvault-index.csv")
    assert {row["status"] for row in rows} == {"renamed", "not_in_index"}
    assert not (tmp_path / "duplicates").exists()
    assert not (tmp_path / "unidentifys").exists()


def test_offline_preview_and_occupied_destination_preserve_both_files(tmp_path):
    source = make_pdf(tmp_path / "source.pdf")
    index = make_index(tmp_path / "inventory.csv", [("10.1234/a", user.file_sha256(source))])
    runner = CliRunner()
    preview = runner.invoke(user.app, ["identify", str(tmp_path), "--index", str(index)])
    assert preview.exit_code == 0, preview.output
    assert source.exists()
    assert report_rows(tmp_path / "netvault-index.csv")[0]["status"] == "would_rename"
    destination = make_pdf(tmp_path / "10.1234_a.pdf", b"occupied")
    applied = runner.invoke(user.app, ["identify", str(tmp_path), "--rename", "--index", str(index)])
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
    result = CliRunner().invoke(user.app, ["identify", str(tmp_path), "--rename", "--index", str(index)])
    assert result.exit_code == 1
    assert "identify failed" in result.output
    assert source.exists()
    assert not (tmp_path / "netvault-index.csv").exists()


def test_index_accepts_reversed_columns_uppercase_hashes_and_identical_repeats(tmp_path):
    path = tmp_path / "inventory.csv"
    path.write_text("sha256,doi\n" + ("A" * 64 + ",10.1234/a\n") * 2)
    assert hash_index.load_hash_index(path) == {"a" * 64: {"doi": "10.1234/a"}}
    path.write_text("doi,sha256\n")
    assert hash_index.load_hash_index(path) == {}
    path.write_text("filename,doi,sha256\n")
    with pytest.raises(ValueError, match="doi,sha256"):
        hash_index.load_hash_index(path)


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
    result = CliRunner().invoke(user.app, ["identify", str(tmp_path), "--rename", "--issn", "00251909"])
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
    result = CliRunner().invoke(user.app, ["identify", str(tmp_path), "--rename"])
    assert result.exit_code == 0, result.output
    assert calls == ["/pdfs/exists"]
    assert source.with_name("10.1234_a.pdf").exists()
    assert report_rows(tmp_path / "netvault-index.csv")[0]["sha256"] == digest


def test_changed_pdf_with_restored_mtime_is_rehashed_and_left_in_place(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf", b"%PDF-first-content")
    digest = user.file_sha256(source)
    index = make_index(tmp_path / "inventory.csv", [("10.1234/a", digest)])
    original_lookup = hash_index.index_lookup

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
    result = CliRunner().invoke(user.app, ["identify", str(tmp_path), "--rename", "--index", str(index)])
    assert result.exit_code == 1
    assert source.read_bytes() == b"%PDF-other-content"
    assert not source.with_name("10.1234_a.pdf").exists()
    assert "Hash reads: 2; unchanged hashes reused: 0" in result.output
    assert "File changed" in report_rows(tmp_path / "netvault-index.csv")[0]["error"]


def test_invalid_options_fail_without_network_or_file_operations(tmp_path, monkeypatch):
    index = make_index(tmp_path / "inventory.csv", [])
    monkeypatch.setattr(user, "ensure_logged_in", lambda: pytest.fail("Login requested"))
    runner = CliRunner()
    both = runner.invoke(user.app, ["identify", str(tmp_path), "--rename", "--index", str(index), "--issn", "0025-1909"])
    assert both.exit_code == 2
    invalid = runner.invoke(user.app, ["identify", str(tmp_path), "--rename", "--issn", "0025-1900"])
    assert invalid.exit_code == 1
    assert "Invalid ISSN" in invalid.output
    assert not (tmp_path / "netvault-index.csv").exists()


def test_single_identify_entry_point_and_preview_options():
    runner = CliRunner()
    result = runner.invoke(user.app, ["rename", "--help"])
    assert result.exit_code == 2 and "No such command" in result.output
    result = runner.invoke(user.app, ["--help"])
    assert result.exit_code == 0 and "nv rename" not in result.output
    result = runner.invoke(user.app, ["identify", "--help"])
    assert result.exit_code == 0
    for option in ("--issn", "--index", "--rename", "--csv", "--check-content", "--workers"):
        assert option in result.output
    assert "--dry-run" not in result.output and "--report" in result.output


@pytest.mark.parametrize("check_content", [False, True])
@pytest.mark.parametrize("apply", [False, True])
def test_empty_inventory_never_collects_unknown_as_unidentified(tmp_path, monkeypatch, check_content, apply):
    source = make_pdf(tmp_path / "nested/unknown.pdf")
    index = make_index(tmp_path / "inventory.csv", [])
    monkeypatch.setattr(user, "ensure_logged_in", lambda: pytest.fail("Login requested"))
    monkeypatch.setattr(user, "api_post", lambda *a, **k: pytest.fail("Network requested"))
    args = ["identify", str(tmp_path), "--index", str(index)]
    if check_content:
        args.append("--check-content")
    if apply:
        args.append("--rename")
    result = CliRunner().invoke(user.app, args)
    assert result.exit_code == 0, result.output
    assert source.exists() and not list(tmp_path.rglob("unidentifys"))
    row, = report_rows(tmp_path / "netvault-index.csv")
    assert row["status"] == row["match_status"] == "not_in_index"


def test_fast_duplicate_collection_at_each_depth_preserves_bytes_and_reports(tmp_path, monkeypatch):
    keeper = make_pdf(tmp_path / "10.1234_a.pdf")
    duplicate = make_pdf(tmp_path / "nested/deeper/copy.pdf")
    archived = make_pdf(duplicate.parent / "duplicates/copy.pdf", b"old archive")
    unknown = make_pdf(tmp_path / "nested/unknown.pdf", b"unknown")
    index = make_index(tmp_path / "inventory.csv", [("10.1234/a", user.file_sha256(keeper))])
    monkeypatch.setattr(identify, "pdf_content_fingerprint", lambda *a: pytest.fail("PDF parsed"))
    runner = CliRunner()
    result = runner.invoke(user.app, ["identify", str(tmp_path), "--index", str(index)])
    assert result.exit_code == 0, result.output
    assert duplicate.exists() and unknown.exists()
    result = runner.invoke(user.app, ["identify", str(tmp_path), "--index", str(index), "--rename"])
    assert result.exit_code == 0, result.output
    rows = {row["original_path"]: row for row in report_rows(tmp_path / "netvault-index-2.csv")}
    row = rows[str(duplicate)]
    assert row["status"] == "moved_duplicate"
    assert row["duplicate_of"] == str(keeper)
    assert Path(row["path"]).parent == duplicate.parent / "duplicates"
    assert Path(row["path"]).name.startswith("copy__")
    assert user.file_sha256(Path(row["path"])) == user.file_sha256(keeper)
    assert archived.read_bytes() == b"old archive" and unknown.exists()
    result = runner.invoke(user.app, ["identify", str(tmp_path), "--index", str(index), "--rename"])
    assert result.exit_code == 0, result.output
    assert len(report_rows(tmp_path / "netvault-index-3.csv")) == 2


def test_content_comparison_is_opt_in_and_scoped_misses_stay_in_place(tmp_path):
    retained = make_content_pdf(tmp_path / "10.1234_a.pdf")
    alternative = make_content_pdf(tmp_path / "nested/copy.pdf", metadata="other")
    unknown = make_pdf(tmp_path / "nested/unknown.pdf", b"other unmatched bytes")
    index = make_index(tmp_path / "inventory.csv", [("10.1234/a", user.file_sha256(retained))])
    runner = CliRunner()
    args = ["identify", str(tmp_path), "--index", str(index), "--rename"]
    result = runner.invoke(user.app, args)
    assert result.exit_code == 0, result.output
    assert alternative.exists() and unknown.exists()
    result = runner.invoke(user.app, args + ["--check-content"])
    assert result.exit_code == 0, result.output
    rows = {row["original_path"]: row for row in report_rows(tmp_path / "netvault-index-2.csv")}
    assert rows[str(alternative)]["status"] == "moved_content_duplicate"
    assert rows[str(alternative)]["content_duplicate_of"] == str(retained)
    assert Path(rows[str(alternative)]["path"]).parent == alternative.parent / "duplicates"
    assert unknown.exists() and not list(tmp_path.rglob("unidentifys"))


def test_later_server_failure_keeps_completed_csv_rows_and_unprocessed_sources(tmp_path):
    first = make_pdf(tmp_path / "a.pdf")
    second = make_pdf(tmp_path / "b.pdf", b"second")
    calls = 0

    def lookup(hashes):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("503 unavailable")
        return {sha: {"doi": "10.1234/a"} for sha in hashes}

    with pytest.raises(RuntimeError, match="503"):
        fast_identify_pdfs(tmp_path, rename=True, duplicates_to=None, csv_path=None,
                           hash_file=user.file_sha256, lookup=lookup, workers=1, batch_size=1)
    assert not first.exists() and second.exists()
    assert (tmp_path / "10.1234_a.pdf").exists()
    row, = report_rows(tmp_path / "netvault-index.csv")
    assert row["status"] == "renamed" and row["original_path"] == str(first)


def test_fast_pipeline_moves_before_remaining_hash_reads_finish(tmp_path, monkeypatch):
    first = make_pdf(tmp_path / "a.pdf")
    second = make_pdf(tmp_path / "b.pdf", b"second")
    first_moved = Event()
    original_move = identify_fast.move_without_overwrite

    def hash_file(path):
        if path == second:
            assert first_moved.wait(3), "Pipeline waited for all hashes before its first move"
        return user.file_sha256(path)

    def move(src, dst, digest, hasher):
        original_move(src, dst, digest, hasher)
        if src == first:
            first_moved.set()

    monkeypatch.setattr(identify_fast, "move_without_overwrite", move)
    report, counts = fast_identify_pdfs(
        tmp_path, rename=True, duplicates_to=None, csv_path=None, hash_file=hash_file,
        lookup=lambda hashes: {sha: {"doi": "10.1234/a"} for sha in hashes},
        workers=1, batch_size=1,
    )
    assert first_moved.is_set()
    assert counts == {"renamed": 1, "moved_doi_conflict": 1}
    assert len(report_rows(report)) == 2


def test_changed_retained_copy_prevents_fast_duplicate_move(tmp_path):
    first = make_pdf(tmp_path / "a.pdf")
    duplicate = make_pdf(tmp_path / "nested/copy.pdf")
    digest = user.file_sha256(first)

    def progress(stage, completed, total):
        if completed == 1:
            (tmp_path / "10.1234_a.pdf").write_bytes(b"changed retained copy")

    report, counts = fast_identify_pdfs(
        tmp_path, rename=True, duplicates_to=None, csv_path=None,
        hash_file=hash_index.RunHashCache(user.file_sha256),
        lookup=lambda hashes: {sha: {"doi": "10.1234/a"} for sha in hashes},
        batch_size=1, progress=progress,
    )
    assert counts == {"renamed": 1, "error": 1}
    assert duplicate.exists() and user.file_sha256(duplicate) == digest
    assert "reference changed" in report_rows(report)[1]["error"]
