"""Offline and destructive-operation safeguards for nv tidy."""

import shutil

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from typer.testing import CliRunner

from netvault.cli import user
from netvault.cli.tidy import apply_tidy, plan_tidy


def make_pdf(path, *, doi=None, content=b"q 1 0 0 1 0 0 cm Q", producer="test"):
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    page = writer.add_blank_page(width=350, height=480)
    data = DecodedStreamObject()
    data.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(data)
    metadata = {"/Producer": producer}
    if doi:
        metadata["/DOI"] = doi
    writer.add_metadata(metadata)
    with path.open("wb") as f:
        writer.write(f)
    return path


def invoke(root, *options):
    return CliRunner().invoke(user.app, ["tidy", str(root), *options])


def test_preview_does_not_change_files_or_write_report(tmp_path, monkeypatch):
    monkeypatch.setattr(user, "ensure_logged_in", lambda: (_ for _ in ()).throw(
        AssertionError("tidy must run offline")
    ))
    make_pdf(tmp_path / "a.pdf", doi="10.1234/article")
    shutil.copyfile(tmp_path / "a.pdf", tmp_path / "same.pdf")
    make_pdf(tmp_path / "missing.pdf")
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.pdf")}
    result = invoke(tmp_path)
    assert result.exit_code == 0, result.output
    assert "confirmed duplicates 1" in result.output
    assert "unidentified 1" in result.output
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.pdf")} == before
    assert not (tmp_path / "unidentifys").exists()
    assert not list(tmp_path.glob("nv-tidy-report*.csv"))


def test_apply_only_deduplicates_in_same_parent_and_moves_locally(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    original = make_pdf(a / "paper.pdf", doi="10.1234/article")
    shutil.copyfile(original, a / "copy.pdf")
    b.mkdir()
    shutil.copyfile(original, b / "other-library.pdf")
    make_pdf(a / "no-doi.pdf")
    make_pdf(b / "no-doi.pdf")
    result = invoke(tmp_path, "--apply")
    assert result.exit_code == 0, result.output
    # The keeper is selected deterministically (metadata, DOI filename,
    # then lexical path), not by modification time or compression ratio.
    assert sum(path.exists() for path in (a / "paper.pdf", a / "copy.pdf")) == 1
    assert (b / "other-library.pdf").exists()
    assert not (a / "no-doi.pdf").exists()
    assert not (b / "no-doi.pdf").exists()
    assert (a / "unidentifys" / "no-doi.pdf").exists()
    assert (b / "unidentifys" / "no-doi.pdf").exists()
    from netvault.cli.history import History
    rows = list(History(tmp_path, "tidy").records())
    assert [r["action"] for r in rows].count("delete") == 1
    assert [r["action"] for r in rows].count("move") == 2
    assert not list(tmp_path.glob("nv-tidy-report*.csv"))
    again = invoke(tmp_path, "--apply")
    assert again.exit_code == 0, again.output
    assert "0 duplicates removed" in again.output
    assert "0 PDFs moved" in again.output
    assert (b / "other-library.pdf").exists()


def test_rewritten_pdf_same_doi_and_pages_is_duplicate(tmp_path):
    a = make_pdf(tmp_path / "original.pdf", doi="10.1234/work", producer="first")
    b = make_pdf(tmp_path / "changed-metadata.pdf", doi="10.1234/work", producer="second")
    assert a.read_bytes() != b.read_bytes()
    result = invoke(tmp_path, "--apply")
    assert result.exit_code == 0, result.output
    assert sum(p.exists() for p in (a, b)) == 1
    assert "1 duplicates removed" in result.output


def test_same_doi_but_different_pages_is_not_removed(tmp_path):
    a = make_pdf(tmp_path / "first.pdf", doi="10.1234/work")
    b = make_pdf(tmp_path / "second.pdf", doi="10.1234/work",
                 content=b"q 0 1 0 rg 20 20 40 40 re f Q")
    result = invoke(tmp_path, "--apply")
    assert result.exit_code == 0, result.output
    assert "0 duplicates removed" in result.output
    assert "retained conflicts" in result.output
    assert a.exists() and b.exists()


def test_filename_only_doi_does_not_rescue_missing_metadata(tmp_path):
    pdf = make_pdf(tmp_path / "10.1234_filename-doi.pdf")
    result = invoke(tmp_path, "--apply")
    assert result.exit_code == 0, result.output
    assert not pdf.exists()
    assert (tmp_path / "unidentifys" / pdf.name).exists()


def test_unidentified_same_directory_byte_duplicates_get_removed(tmp_path):
    original = make_pdf(tmp_path / "a.pdf")
    shutil.copyfile(original, tmp_path / "b.pdf")
    result = invoke(tmp_path, "--apply")
    assert result.exit_code == 0, result.output
    contents = sorted((tmp_path / "unidentifys").glob("*.pdf"))
    assert len(contents) == 1
    assert "1 duplicates removed" in result.output


def test_existing_unidentifys_not_scanned_or_overwritten(tmp_path):
    old = make_pdf(tmp_path / "unidentifys" / "missing.pdf")
    existing = old.read_bytes()
    make_pdf(tmp_path / "missing.pdf", producer="new")
    make_pdf(tmp_path / "duplicates" / "existing.pdf", doi="10.1234/skip")
    result = invoke(tmp_path, "--apply")
    assert result.exit_code == 0, result.output
    assert old.read_bytes() == existing
    saved = list((tmp_path / "unidentifys").glob("*.pdf"))
    assert len(saved) == 2
    assert (tmp_path / "duplicates" / "existing.pdf").exists()
    assert "Scanned 1 PDFs" in result.output


def test_invalid_and_symlink_pdfs_are_left_untouched(tmp_path):
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"%PDF-1.4\nnot a complete PDF\n%%EOF")
    good = make_pdf(tmp_path / "good.pdf", doi="10.1234/good")
    link = tmp_path / "linked.pdf"
    link.symlink_to(good.name)
    result = invoke(tmp_path, "--apply")
    assert result.exit_code == 1, result.output
    assert bad.is_file()
    assert link.is_symlink()
    assert good.exists()
    assert "errors" in result.output


def test_refuse_move_destination_symlink(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    dest = tmp_path / "unidentifys"
    dest.symlink_to(external, target_is_directory=True)
    pdf = make_pdf(tmp_path / "missing.pdf")
    result = invoke(tmp_path, "--apply")
    assert result.exit_code == 1, result.output
    assert pdf.exists()
    assert not list(external.glob("*.pdf"))


def test_refuse_deletion_when_pdf_changes_after_scan(tmp_path):
    keeper = make_pdf(tmp_path / "a.pdf", doi="10.1234/work")
    victim = tmp_path / "b.pdf"
    shutil.copyfile(keeper, victim)
    rows = plan_tidy(
        tmp_path, lambda paths: user.scan_upload_dois(paths, for_tidy=True), user.file_sha256
    )
    victim.write_bytes(victim.read_bytes() + b"\nmodified")
    report, counts = apply_tidy(tmp_path, rows, user.file_sha256)
    assert report.exists()
    assert counts.get("error") == 1
    assert keeper.exists() and victim.exists()


def test_tidy_does_not_read_file_hashes_for_unique_dois(tmp_path, monkeypatch):
    make_pdf(tmp_path / "a.pdf", doi="10.1234/one")
    make_pdf(tmp_path / "b.pdf", doi="10.1234/two")
    def unexpected(_):
        raise AssertionError("No hashes needed for unique DOI preview")
    monkeypatch.setattr(user, "file_sha256", unexpected)
    result = invoke(tmp_path)
    assert result.exit_code == 0, result.output
    assert "confirmed duplicates 0" in result.output


def test_upload_recursion_skips_tidy_collection_folder(tmp_path):
    make_pdf(tmp_path / "paper.pdf", doi="10.1234/one")
    make_pdf(tmp_path / "unidentifys" / "no.pdf")
    make_pdf(tmp_path / "duplicates" / "old.pdf", doi="10.1234/old")
    selected = user.collect_pdf_paths(
        [tmp_path], excluded_dir_names=user.DEFAULT_UPLOAD_EXCLUDED_DIRS
    )
    assert len(selected) == 1
    assert selected[0].name == "paper.pdf"
