import csv
import errno
from pathlib import Path

import pytest
from typer.testing import CliRunner
from test_pdf_fingerprint import make_pdf

import netvault.cli.identify as identify
import netvault.cli.user as cli


DOI = "10.1234/example"


def write_pdf(directory: Path, name: str, content: bytes = b"%PDF-1.4\nexample\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return path


def read_rows(report: Path) -> list[dict]:
    with report.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def run(directory: Path, *, rename=False, duplicates_to=None, csv_path=None, lookup=None,
        unidentified_to=None):
    return identify.identify_pdfs(
        directory, rename=rename, duplicates_to=duplicates_to, csv_path=csv_path,
        hash_file=cli.file_sha256,
        lookup=lookup or (lambda hashes: {sha: {"doi": DOI} for sha in hashes}),
        unidentified_to=unidentified_to,
    )


def test_preview_uses_local_hash_for_alias_and_preserves_pdf(tmp_path):
    pdf = write_pdf(tmp_path / "nested", 'original,名字.PDF')
    sha256 = cli.file_sha256(pdf)
    report, counts = run(
        tmp_path, lookup=lambda hashes: {sha256: {"doi": DOI, "sha256": "f" * 64}},
    )
    row, = read_rows(report)
    assert pdf.exists()
    assert not pdf.with_name("10.1234_example.pdf").exists()
    assert counts == {"would_rename": 1}
    assert row["sha256"] == sha256
    assert row["doi"] == DOI
    assert row["path"] == str(pdf)
    assert row["directory"] == str(pdf.parent)
    assert row["original_filename"] == 'original,名字.PDF'
    assert row["proposed_path"] == str(pdf.with_name("10.1234_example.pdf"))


def test_rename_keeps_bytes_and_repeated_run_keeps_reports(tmp_path):
    pdf = write_pdf(tmp_path, "paper.pdf")
    sha256 = cli.file_sha256(pdf)
    report, counts = run(tmp_path, rename=True)
    row, = read_rows(report)
    destination = tmp_path / "10.1234_example.pdf"
    assert not pdf.exists()
    assert cli.file_sha256(destination) == sha256
    assert counts == {"renamed": 1}
    assert row["original_path"] == str(pdf)
    assert row["path"] == str(destination)
    second_report, counts = run(tmp_path, rename=True)
    assert counts == {"already_named": 1}
    assert second_report.name == "netvault-index-2.csv"
    assert read_rows(report) == [row]


def test_unmatched_pdf_and_non_pdf_are_unchanged(tmp_path):
    pdf = write_pdf(tmp_path, "unknown.pdf")
    text = tmp_path / "note.txt"
    text.write_text("keep")
    report, counts = run(tmp_path, rename=True, lookup=lambda hashes: {})
    assert counts == {"not_found": 1}
    assert read_rows(report)[0]["doi"] == ""
    assert pdf.exists() and text.read_text() == "keep"


def test_conflicting_different_hashes_are_not_overwritten(tmp_path):
    first = write_pdf(tmp_path, "a.pdf", b"%PDF-first")
    second = write_pdf(tmp_path, "b.pdf", b"%PDF-second")
    report, counts = run(tmp_path, rename=True)
    assert counts == {"renamed": 1, "conflict": 1}
    assert (tmp_path / "10.1234_example.pdf").read_bytes() == b"%PDF-first"
    assert second.read_bytes() == b"%PDF-second"
    assert not first.exists()
    assert read_rows(report)[1]["path"] == str(second)


@pytest.mark.parametrize("rename", [False, True])
def test_content_duplicates_across_dois_and_directories_keep_one_file(tmp_path, rename):
    retained = make_pdf(tmp_path / '10.1234_example.pdf', notice=True)
    alternative = make_pdf(tmp_path / 'nested/10.1234_example_2.pdf',
                           doi='10.1234/example_2', notice=True, metadata='other')
    identical = alternative.with_name('copy.pdf')
    identical.write_bytes(alternative.read_bytes())
    unmatched = make_pdf(tmp_path / 'unknown.pdf', doi='10.1234/example_3',
                         notice=True, metadata='unmatched')
    mapping = {cli.file_sha256(retained): {'doi': DOI},
               cli.file_sha256(alternative): {'doi': DOI + '_2'}}
    report, counts = run(tmp_path, rename=rename, duplicates_to=tmp_path/'duplicates',
                         unidentified_to=tmp_path/'unidentifys', lookup=lambda hashes: mapping)
    rows = {row['original_path']: row for row in read_rows(report)}
    status = 'moved_content_duplicate' if rename else 'would_move_content_duplicate'
    assert counts == {'already_named': 1, status: 2,
                      'moved_duplicate' if rename else 'would_move_duplicate': 1}
    assert rows[str(alternative)]['doi'] == DOI + '_2'
    assert rows[str(unmatched)]['doi'] == ''
    assert rows[str(unmatched)]['match_status'] == 'not_found'
    for path in (alternative, unmatched):
        row = rows[str(path)]
        assert row['content_duplicate_of'] == str(retained)
        assert row['content_duplicate_of_sha256'] == cli.file_sha256(retained)
        assert row['content_sha256'] == rows[str(retained)]['content_sha256']
        assert row['duplicate_of'] == '' and row['conflict_with'] == ''
        assert cli.file_sha256(Path(row['path'])) == row['sha256']
    assert rows[str(identical)]['duplicate_of'] == rows[str(alternative)]['path']
    assert retained.exists()
    if rename:
        assert not alternative.exists() and not identical.exists() and not unmatched.exists()
        _, second_counts = run(tmp_path, rename=True, duplicates_to=tmp_path/'duplicates')
        assert second_counts == {'already_named': 1}
    else:
        assert alternative.exists() and identical.exists() and unmatched.exists()
        assert not (tmp_path/'duplicates').exists()


def test_content_duplicate_reference_tracks_renamed_keeper(tmp_path):
    first = make_pdf(tmp_path/'a.pdf', notice=True)
    second = make_pdf(tmp_path/'b.pdf', metadata='second', notice=True)
    report, counts = run(tmp_path, rename=True, duplicates_to=tmp_path/'duplicates')
    rows = read_rows(report)
    assert counts == {'renamed': 1, 'moved_content_duplicate': 1}
    assert rows[1]['content_duplicate_of'] == rows[0]['path']
    assert Path(rows[1]['content_duplicate_of']).exists()
    assert not first.exists() and not second.exists()


def test_changed_content_reference_retains_source(tmp_path, monkeypatch):
    retained = make_pdf(tmp_path/'10.1234_example.pdf')
    alternative = make_pdf(tmp_path/'b.pdf', metadata='alternative')
    original = identify.file_signature
    calls = 0

    def signature(path):
        nonlocal calls
        if path == retained:
            calls += 1
            if calls == 5:
                retained.write_bytes(b'%PDF-changed-reference')
        return original(path)

    monkeypatch.setattr(identify, 'file_signature', signature)
    report, counts = run(tmp_path, rename=True, duplicates_to=tmp_path/'duplicates')
    assert counts == {'already_named': 1, 'error': 1}
    assert alternative.exists()
    assert 'Content reference changed' in read_rows(report)[1]['error']


@pytest.mark.parametrize("rename", [False, True])
@pytest.mark.parametrize("already_named", [False, True])
def test_doi_conflicts_are_collected_separately_from_hash_duplicates(tmp_path, rename, already_named):
    first = write_pdf(tmp_path, "10.1234_example.pdf" if already_named else "a.pdf", b"%PDF-first")
    second_name = "000.pdf" if already_named else "b.pdf"
    second = write_pdf(tmp_path, second_name, b"%PDF-second")
    copy = write_pdf(tmp_path, "c.pdf", b"%PDF-second")
    duplicates = tmp_path / "duplicates"
    existing = write_pdf(duplicates, second_name, b"%PDF-preserved archive")
    report, counts = run(tmp_path, rename=rename, duplicates_to=duplicates)
    rows = {row["original_path"]: row for row in read_rows(report)}
    conflict = rows[str(second)]
    assert conflict["status"] == ("moved_doi_conflict" if rename else "would_move_doi_conflict")
    assert conflict["match_status"] == "matched" and conflict["error"] == ""
    assert conflict["duplicate_of"] == ""
    assert conflict["conflict_with"] == rows[str(first)]["path"]
    assert conflict["conflict_with_sha256"] == rows[str(first)]["sha256"]
    assert conflict["sha256"] != conflict["conflict_with_sha256"]
    assert Path(conflict["proposed_path"]).parent == duplicates
    assert Path(conflict["proposed_path"]).name.startswith(f"{second.stem}__")
    assert rows[str(copy)]["status"] == ("moved_duplicate" if rename else "would_move_duplicate")
    assert rows[str(copy)]["duplicate_of"] == conflict["path"]
    assert rows[str(copy)]["conflict_with"] == ""
    assert "conflict" not in counts
    assert existing.read_bytes() == b"%PDF-preserved archive"
    for row in rows.values():
        assert cli.file_sha256(Path(row["path"])) == row["sha256"]
    if rename:
        assert not second.exists() and not copy.exists()
        _, next_counts = run(tmp_path, rename=True, duplicates_to=duplicates)
        assert next_counts == {"already_named": 1}
    else:
        assert first.exists() and second.exists() and copy.exists()


def test_unverified_destination_is_not_assumed_to_share_doi(tmp_path):
    target = write_pdf(tmp_path, "10.1234_example.pdf", b"%PDF-unmatched")
    source = write_pdf(tmp_path, "a.pdf", b"%PDF-matched")
    source_sha = cli.file_sha256(source)
    report, counts = run(tmp_path, rename=True, duplicates_to=tmp_path / "duplicates",
                         lookup=lambda hashes: {source_sha: {"doi": DOI}})
    assert counts == {"not_found": 1, "conflict": 1}
    assert target.exists() and source.exists()
    assert all(row["conflict_with"] == "" for row in read_rows(report))


def test_changed_doi_conflict_reference_retains_source(tmp_path, monkeypatch):
    first = write_pdf(tmp_path, "10.1234_example.pdf", b"%PDF-first")
    second = write_pdf(tmp_path, "b.pdf", b"%PDF-second")
    original = identify.file_signature
    calls = 0

    def signature(path):
        nonlocal calls
        if path == first:
            calls += 1
            if calls == 5:
                first.write_bytes(b"%PDF-reference changed")
        return original(path)

    monkeypatch.setattr(identify, "file_signature", signature)
    report, counts = run(tmp_path, rename=True, duplicates_to=tmp_path / "duplicates")
    assert counts == {"already_named": 1, "error": 1}
    assert second.exists()
    assert "DOI conflict reference changed" in read_rows(report)[1]["error"]


@pytest.mark.parametrize("rename", [False, True])
def test_encoding_collisions_are_recorded(tmp_path, rename):
    first = write_pdf(tmp_path, "a.pdf", b"%PDF-first")
    second = write_pdf(tmp_path, "b.pdf", b"%PDF-second")
    mapping = {
        cli.file_sha256(first): {"doi": "10.1234/a/b"},
        cli.file_sha256(second): {"doi": "10.1234/a_b"},
    }
    report, counts = run(tmp_path, rename=rename, lookup=lambda hashes: mapping,
                         duplicates_to=tmp_path / "duplicates")
    assert counts == {"renamed" if rename else "would_rename": 1, "conflict": 1}
    assert second.exists()
    assert {row["doi"] for row in read_rows(report)} == {"10.1234/a/b", "10.1234/a_b"}


@pytest.mark.parametrize("rename", [False, True])
def test_duplicates_keep_correctly_named_copy_and_exclude_destination(tmp_path, rename):
    keeper = write_pdf(tmp_path, "10.1234_example.pdf")
    duplicate = write_pdf(tmp_path / "nested", "copy.pdf")
    duplicates = tmp_path / "duplicates"
    existing = write_pdf(duplicates, "copy.pdf", b"%PDF-already there")
    report, counts = run(tmp_path, rename=rename, duplicates_to=duplicates)
    rows = read_rows(report)
    assert len(rows) == 2
    assert keeper.exists()
    assert existing.read_bytes() == b"%PDF-already there"
    row = next(row for row in rows if row["original_path"] == str(duplicate))
    destination = Path(row["proposed_path"])
    assert destination.name.startswith("copy__")
    assert row["duplicate_of"] == str(keeper)
    if rename:
        assert counts == {"already_named": 1, "moved_duplicate": 1}
        assert not duplicate.exists()
        assert cli.file_sha256(destination) == cli.file_sha256(keeper)
        assert row["path"] == str(destination)
        assert row["directory"] == str(duplicates)
        second_report, second_counts = run(tmp_path, rename=True, duplicates_to=duplicates)
        assert len(read_rows(second_report)) == 1
        assert second_counts == {"already_named": 1}
    else:
        assert counts == {"already_named": 1, "would_move_duplicate": 1}
        assert duplicate.exists()
        assert not destination.exists()
        assert row["path"] == str(duplicate)


def test_duplicate_moves_record_final_keeper_path_even_without_server_match(tmp_path):
    first = write_pdf(tmp_path, "a.pdf")
    duplicate = write_pdf(tmp_path / "nested", "b.pdf")
    report, counts = run(
        tmp_path, rename=True, duplicates_to=tmp_path / "duplicates", lookup=lambda hashes: {},
    )
    assert counts == {"not_found": 1, "moved_duplicate": 1}
    rows = read_rows(report)
    assert rows[1]["duplicate_of"] == str(first)
    assert rows[1]["match_status"] == "not_found"
    assert first.exists() and not duplicate.exists()


def test_duplicate_of_tracks_renamed_keeper(tmp_path):
    write_pdf(tmp_path, "a.pdf")
    write_pdf(tmp_path / "nested", "b.pdf")
    report, counts = run(tmp_path, rename=True, duplicates_to=tmp_path / "duplicates")
    assert counts == {"renamed": 1, "moved_duplicate": 1}
    rows = read_rows(report)
    assert rows[1]["duplicate_of"] == rows[0]["path"]
    assert Path(rows[1]["duplicate_of"]).exists()


@pytest.mark.parametrize("stem", ["文" * 80, "📄" * 62])
def test_duplicate_destination_names_fit_mac_limits_with_unicode(tmp_path, stem):
    for folder in (tmp_path, tmp_path / "nested-a", tmp_path / "nested-b"):
        write_pdf(folder, f"{stem}.pdf")
    duplicates = tmp_path / "duplicates"
    write_pdf(duplicates, f"{stem}.pdf", b"%PDF-existing destination")
    report, counts = run(tmp_path, rename=True, duplicates_to=duplicates)
    assert counts == {"renamed": 1, "moved_duplicate": 2}
    moved = [row for row in read_rows(report) if row["status"] == "moved_duplicate"]
    assert len({row["path"] for row in moved}) == 2
    assert all(len(Path(row["path"]).name.encode("utf-8")) <= 255 for row in moved)
    assert all(Path(row["path"]).exists() for row in moved)


def test_server_failure_leaves_pdfs_and_existing_report_untouched(tmp_path):
    first = write_pdf(tmp_path, "a.pdf")
    duplicate = write_pdf(tmp_path, "b.pdf")
    old_report = tmp_path / "netvault-index.csv"
    old_report.write_text("keep")

    def failed_lookup(hashes):
        raise RuntimeError("503 unavailable")

    with pytest.raises(RuntimeError, match="503"):
        run(tmp_path, rename=True, duplicates_to=tmp_path / "duplicates", lookup=failed_lookup)
    assert first.exists() and duplicate.exists()
    assert old_report.read_text() == "keep"
    assert not (tmp_path / "netvault-index-2.csv").exists()


def test_explicit_existing_csv_prevents_file_operations(tmp_path):
    pdf = write_pdf(tmp_path, "a.pdf")
    report = tmp_path / "report.csv"
    report.write_text("previous audit")
    with pytest.raises(FileExistsError):
        run(tmp_path, rename=True, csv_path=report)
    assert pdf.exists()
    assert report.read_text() == "previous audit"


@pytest.mark.parametrize("destination", [".", ".."])
def test_reject_duplicate_destination_that_contains_scan(tmp_path, destination):
    write_pdf(tmp_path, "a.pdf")
    with pytest.raises(ValueError, match="ancestor"):
        run(tmp_path, rename=True, duplicates_to=tmp_path / destination)
    assert not (tmp_path / "netvault-index.csv").exists()


def test_symlink_is_not_followed_or_moved(tmp_path):
    outside = write_pdf(tmp_path / "outside", "outside.pdf")
    source = tmp_path / "source"
    source.mkdir()
    link = source / "link.pdf"
    link.symlink_to(outside)
    report, counts = run(source, rename=True)
    assert counts == {"error": 1}
    assert read_rows(report)[0]["sha256"] == ""
    assert link.is_symlink() and outside.exists()


def test_file_changed_during_query_is_not_renamed(tmp_path):
    pdf = write_pdf(tmp_path, "a.pdf")

    def lookup(hashes):
        response = {sha: {"doi": DOI} for sha in hashes}
        pdf.write_bytes(b"%PDF-edited")
        return response

    report, counts = run(tmp_path, rename=True, lookup=lookup)
    assert counts == {"error": 1}
    assert pdf.read_bytes() == b"%PDF-edited"
    assert "changed" in read_rows(report)[0]["error"]


def test_changed_keeper_does_not_cause_duplicate_move(tmp_path, monkeypatch):
    keeper = write_pdf(tmp_path, "10.1234_example.pdf")
    duplicate = write_pdf(tmp_path, "b.pdf")
    original = identify.file_signature
    calls = 0

    def signature(path):
        nonlocal calls
        if path == keeper:
            calls += 1
            if calls == 5:
                keeper.write_bytes(b"%PDF-new keeper content")
        return original(path)

    monkeypatch.setattr(identify, "file_signature", signature)
    report, counts = run(tmp_path, rename=True, duplicates_to=tmp_path / "duplicates")
    assert counts == {"already_named": 1, "error": 1}
    assert duplicate.exists()
    assert "Retained copy changed" in read_rows(report)[1]["error"]


def test_exclusive_move_cannot_replace_a_concurrent_destination(tmp_path, monkeypatch):
    pdf = write_pdf(tmp_path, "a.pdf")
    original = identify.os.link

    def link(source, destination):
        destination.write_bytes(b"concurrent file")
        original(source, destination)

    monkeypatch.setattr(identify.os, "link", link)
    report, counts = run(tmp_path, rename=True)
    assert counts == {"conflict": 1}
    assert pdf.exists()
    assert (tmp_path / "10.1234_example.pdf").read_bytes() == b"concurrent file"
    assert "appeared" in read_rows(report)[0]["error"]


@pytest.mark.parametrize("corrupt", [False, True])
def test_cross_filesystem_move_verifies_copy_before_removing_source(tmp_path, monkeypatch, corrupt):
    pdf = write_pdf(tmp_path, "a.pdf")
    sha256 = cli.file_sha256(pdf)

    def link(source, destination):
        raise OSError(errno.EXDEV, "different filesystem")

    monkeypatch.setattr(identify.os, "link", link)
    if corrupt:
        monkeypatch.setattr(identify.shutil, "copyfileobj", lambda src, dst, **kwargs: dst.write(b"bad"))
    report, counts = run(tmp_path, rename=True)
    destination = tmp_path / "10.1234_example.pdf"
    if corrupt:
        assert counts == {"error": 1}
        assert cli.file_sha256(pdf) == sha256
        assert not destination.exists()
        assert "copying" in read_rows(report)[0]["error"]
    else:
        assert counts == {"renamed": 1}
        assert not pdf.exists()
        assert cli.file_sha256(destination) == sha256


def test_invalid_server_doi_is_recorded_without_rename(tmp_path):
    pdf = write_pdf(tmp_path, "a.pdf")
    report, counts = run(tmp_path, rename=True, lookup=lambda hashes: {
        sha: {"doi": "not-a-doi"} for sha in hashes
    })
    assert counts == {"error": 1}
    assert pdf.exists()
    assert read_rows(report)[0]["match_status"] == "error"


def test_cli_wires_strict_bulk_lookup_and_handles_auth_failure(tmp_path, monkeypatch):
    write_pdf(tmp_path, "a.pdf")
    monkeypatch.setattr(cli, "ensure_logged_in", lambda: None)

    def post(path, json):
        assert path == "/pdfs/exists"
        assert len(json["sha256"]) == 1
        return {"existing": {sha: {"doi": DOI} for sha in json["sha256"]}}

    monkeypatch.setattr(cli, "api_post", post)
    result = CliRunner().invoke(cli.app, ["identify", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Preview only" in result.output
    assert (tmp_path / "a.pdf").exists()

    def denied(*args, **kwargs):
        raise RuntimeError("401 expired")

    monkeypatch.setattr(cli, "api_post", denied)
    monkeypatch.setattr(cli, "get_existing_pdf_by_sha256", lambda sha: pytest.fail("No fallback"))
    result = CliRunner().invoke(cli.app, ["identify", str(tmp_path), "--rename"])
    assert result.exit_code == 1
    assert "401 expired" in result.output
    assert (tmp_path / "a.pdf").exists()


def test_bulk_query_chunks_hashes_without_uploading(monkeypatch):
    hashes = [f"{number:064x}" for number in range(1001)]
    sizes = []

    def post(path, json):
        assert path == "/pdfs/exists"
        sizes.append(len(json["sha256"]))
        return {"existing": {sha: {"doi": DOI} for sha in json["sha256"]}}

    monkeypatch.setattr(cli, "api_post", post)
    assert len(cli.get_existing_pdfs_by_sha256(hashes, fallback=False)) == 1001
    assert sizes == [500, 500, 1]


def test_cli_rename_automatically_collects_into_working_directory(tmp_path, monkeypatch):
    working = tmp_path / "working"
    working.mkdir()
    source = tmp_path / "papers"
    matched = write_pdf(source, "a.pdf")
    alternative = write_pdf(source, "b.pdf", b"%PDF-alternate-server-hash")
    write_pdf(source / "nested", "copy.pdf")
    unmatched = write_pdf(source, "unknown.pdf", b"%PDF-not-on-server")
    write_pdf(source / "nested", "unknown-copy.pdf", b"%PDF-not-on-server")
    matched_sha = cli.file_sha256(matched)
    alternative_sha = cli.file_sha256(alternative)
    monkeypatch.chdir(working)
    monkeypatch.setattr(cli, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(cli, "api_post", lambda path, json: {
        "existing": {matched_sha: {"doi": DOI}, alternative_sha: {"doi": DOI}},
    })
    runner = CliRunner()
    preview = runner.invoke(cli.app, ["identify", str(source)])
    assert preview.exit_code == 0, preview.output
    assert matched.exists() and unmatched.exists()
    assert not (working / "duplicates").exists()
    assert not (working / "unidentifys").exists()
    preview_rows = read_rows(source / "netvault-index.csv")
    assert sorted(row["status"] for row in preview_rows) == [
        "would_move_doi_conflict", "would_move_duplicate", "would_move_duplicate",
        "would_move_unidentified", "would_rename",
    ]

    applied = runner.invoke(cli.app, ["identify", str(source), "--rename"])
    assert applied.exit_code == 0, applied.output
    rows = read_rows(source / "netvault-index-2.csv")
    assert sorted(row["status"] for row in rows) == [
        "moved_doi_conflict", "moved_duplicate", "moved_duplicate", "moved_unidentified", "renamed",
    ]
    assert len(list((working / "duplicates").glob("*.pdf"))) == 3
    assert len(list((working / "unidentifys").glob("*.pdf"))) == 1
    assert not (source / "duplicates").exists()
    assert not (source / "unidentifys").exists()
    for row in rows:
        actual = Path(row["path"])
        assert actual.exists() and cli.file_sha256(actual) == row["sha256"]
        assert row["directory"] == str(actual.parent)
        if row["duplicate_of"]:
            assert cli.file_sha256(Path(row["duplicate_of"])) == row["sha256"]
        if row["conflict_with"]:
            assert row["status"] == "moved_doi_conflict"
            assert cli.file_sha256(Path(row["conflict_with"])) == row["conflict_with_sha256"]
        if row["status"] == "moved_unidentified":
            assert actual.parent == working / "unidentifys"
            assert row["doi"] == "" and row["match_status"] == "not_found"


def test_default_scan_excludes_both_collection_directories_on_repeated_runs(tmp_path, monkeypatch):
    first = write_pdf(tmp_path, "new.pdf", b"%PDF-new-unmatched")
    old_unmatched = write_pdf(tmp_path / "unidentifys", "old.pdf", b"%PDF-old-unmatched")
    old_duplicate = write_pdf(tmp_path / "duplicates", "old-copy.pdf", b"%PDF-old-duplicate")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "ensure_logged_in", lambda: None)
    queried = []

    def post(path, json):
        queried.extend(json["sha256"])
        return {"existing": {}}

    monkeypatch.setattr(cli, "api_post", post)
    result = CliRunner().invoke(cli.app, ["identify", "--rename"])
    assert result.exit_code == 0, result.output
    assert queried == [cli.file_sha256(tmp_path / "unidentifys/new.pdf")]
    row, = read_rows(tmp_path / "netvault-index.csv")
    assert row["status"] == "moved_unidentified"
    assert not first.exists()
    assert old_unmatched.read_bytes() == b"%PDF-old-unmatched"
    assert old_duplicate.read_bytes() == b"%PDF-old-duplicate"
    result = CliRunner().invoke(cli.app, ["identify", "--rename"])
    assert result.exit_code == 0, result.output
    assert read_rows(tmp_path / "netvault-index-2.csv") == []


def test_unidentified_basename_collisions_keep_every_pdf(tmp_path):
    source = tmp_path / "papers"
    first = write_pdf(source, "paper.pdf", b"%PDF-one")
    second = write_pdf(source / "nested", "paper.pdf", b"%PDF-two")
    unidentified = tmp_path / "unidentifys"
    old = write_pdf(unidentified, "paper.pdf", b"%PDF-prior-run")
    report, counts = run(
        source, rename=True, lookup=lambda hashes: {}, unidentified_to=unidentified,
    )
    assert counts == {"moved_unidentified": 2}
    assert not first.exists() and not second.exists()
    assert old.read_bytes() == b"%PDF-prior-run"
    rows = read_rows(report)
    assert len({row["path"] for row in rows}) == 2
    for row in rows:
        assert Path(row["path"]).parent == unidentified
        assert cli.file_sha256(Path(row["path"])) == row["sha256"]


def test_failed_unidentified_move_retains_source_and_tracks_duplicates(tmp_path, monkeypatch):
    source = tmp_path / "papers"
    first = write_pdf(source, "a.pdf")
    duplicate = write_pdf(source, "b.pdf")
    unidentified = tmp_path / "unidentifys"
    original = identify.move_without_overwrite

    def move(src, dst, sha256, hash_file):
        if dst.parent == unidentified:
            raise PermissionError("No write access")
        return original(src, dst, sha256, hash_file)

    monkeypatch.setattr(identify, "move_without_overwrite", move)
    report, counts = run(
        source, rename=True, lookup=lambda hashes: {}, unidentified_to=unidentified,
        duplicates_to=tmp_path / "duplicates",
    )
    assert counts == {"error": 1, "moved_duplicate": 1}
    rows = read_rows(report)
    # The retained source is still present and is safely referenced by the duplicate.
    assert first.exists() and not duplicate.exists()
    assert rows[1]["duplicate_of"] == str(first)
    assert rows[0]["path"] == str(first)


def test_unidentified_destination_validation_prevents_all_moves(tmp_path):
    pdf = write_pdf(tmp_path, "a.pdf")
    with pytest.raises(ValueError, match="Unidentified.*ancestor"):
        run(tmp_path, rename=True, unidentified_to=tmp_path)
    with pytest.raises(ValueError, match="must not overlap"):
        run(tmp_path, rename=True, unidentified_to=tmp_path / "output",
            duplicates_to=tmp_path / "output/nested")
    assert pdf.exists()


def test_invalid_server_doi_is_an_error_not_an_unidentified_move(tmp_path):
    pdf = write_pdf(tmp_path, "a.pdf")
    report, counts = run(
        tmp_path, rename=True, unidentified_to=tmp_path / "unidentifys",
        lookup=lambda hashes: {sha: {"doi": "invalid"} for sha in hashes},
    )
    assert counts == {"error": 1}
    assert pdf.exists()
    assert not (tmp_path / "unidentifys").exists()
    assert read_rows(report)[0]["match_status"] == "error"
