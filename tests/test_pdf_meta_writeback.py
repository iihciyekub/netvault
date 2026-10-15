"""Automatic DOI writes require independent evidence and preserve original bytes."""

import hashlib
import io
import multiprocessing
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject,
)

from netvault.cli import pdf_meta


DOI = "10.5555/verified.article"
OTHER = "10.5555/other.article"


def make_pdf(path, *, info=None, signature=None, encrypted=False):
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": "An independently verified article", "/Custom": "keep me"})
    if info:
        writer.add_metadata(info)
    xmp = DecodedStreamObject()
    xmp.set_data(
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/" '
        b'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">'
        b'<prism:doi>other</prism:doi></x:xmpmeta>'
    )
    xmp[NameObject("/Type")] = NameObject("/Metadata")
    xmp[NameObject("/Subtype")] = NameObject("/XML")
    writer._root_object[NameObject("/Metadata")] = writer._add_object(xmp)
    if signature == "perms":
        writer._root_object[NameObject("/Perms")] = DictionaryObject()
    if signature in ("field", "nested", "flags"):
        field = DictionaryObject({NameObject("/FT"): NameObject("/Sig")})
        if signature == "nested":
            field[NameObject("/Kids")] = ArrayObject([writer._add_object(DictionaryObject())])
        form = DictionaryObject({NameObject("/Fields"): ArrayObject([writer._add_object(field)])})
        if signature == "flags":
            form = DictionaryObject({NameObject("/SigFlags"): NumberObject(3)})
        writer._root_object[NameObject("/AcroForm")] = writer._add_object(form)
    if encrypted:
        writer.encrypt("secret")
    writer.write(path)
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def receipt(path, **overrides):
    return {
        "policy_version": 1,
        "input_sha256": sha(path),
        "doi": DOI,
        "canonical_doi": DOI,
        "decision": "verified",
        "writeback_allowed": True,
        "metadata_status": "ok",
        "metadata_provider": "crossref",
        "printed_doi_confirmed": True,
        "title_match_score": 0.99,
        "author_match": True,
        "year_match": True,
        **overrides,
    }


def assert_not_changed(pdf, original):
    assert pdf.read_bytes() == original
    assert not list(pdf.parent.glob(f".{pdf.name}.nv-doi-*"))


def _concurrent_write_worker(path, verification, start, outcomes):
    start.wait(10)
    try:
        result = pdf_meta.write_verified_pdf_doi(Path(path), verification)
        outcomes.put(("ok", result["changed"]))
    except ValueError as exc:
        outcomes.put(("rejected", str(exc)))


def test_incremental_write_preserves_pages_info_xmp_mode_and_backup(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf")
    pdf.chmod(0o640)
    original = pdf.read_bytes()
    before = PdfReader(io.BytesIO(original))
    result = pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))
    assert result["changed"] is True
    assert result["doi"] == DOI
    assert result["file"] == str(pdf)
    assert result["original_sha256"] == hashlib.sha256(original).hexdigest()
    assert result["sha256"] == sha(pdf)
    assert result["sha256"] != result["original_sha256"]
    assert len(result["sha256"]) == len(result["original_sha256"]) == 64
    assert pdf.read_bytes().startswith(original)
    backup = Path(result["backup"])
    assert backup.name == f"paper.pdf.nv-doi-{result['original_sha256']}.bak"
    assert backup.read_bytes() == original
    assert stat.S_IMODE(pdf.stat().st_mode) == stat.S_IMODE(backup.stat().st_mode) == 0o640
    after = PdfReader(pdf)
    assert len(after.pages) == len(before.pages) == 2
    assert after.metadata["/DOI"] == DOI
    for key, value in before.metadata.items():
        assert after.metadata[key] == value
    assert (after.trailer["/Root"]["/Metadata"].get_object().get_data()
            == before.trailer["/Root"]["/Metadata"].get_object().get_data())


@pytest.mark.parametrize("info", [{"/DOI": ""}, {"/DOI": "  \t "}])
def test_blank_doi_field_can_be_written(tmp_path, info):
    pdf = make_pdf(tmp_path / "paper.pdf", info=info)
    assert pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))["changed"]
    assert PdfReader(pdf).metadata["/DOI"] == DOI


@pytest.mark.parametrize("info", [{"/DOI": DOI}, {"/Subject": f"DOI: {DOI}"}])
def test_same_doi_is_noop_only_with_current_independent_receipt(tmp_path, info):
    pdf = make_pdf(tmp_path / "paper.pdf", info=info)
    original = pdf.read_bytes()
    result = pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))
    assert result["changed"] is False
    assert result["backup"] is None
    assert result["original_sha256"] == result["sha256"] == sha(pdf)
    assert_not_changed(pdf, original)
    with pytest.raises(ValueError, match="SHA-256"):
        pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf, input_sha256="0" * 64))
    assert_not_changed(pdf, original)


def test_written_file_is_noop_on_fresh_receipt_but_rejects_original_receipt(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf")
    old_receipt = receipt(pdf)
    pdf_meta.write_verified_pdf_doi(pdf, old_receipt)
    written = pdf.read_bytes()
    with pytest.raises(ValueError, match="SHA-256"):
        pdf_meta.write_verified_pdf_doi(pdf, old_receipt)
    assert_not_changed(pdf, written)
    assert pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))["changed"] is False


@pytest.mark.parametrize("field", [
    "policy_version", "input_sha256", "doi", "canonical_doi", "decision",
    "writeback_allowed", "metadata_status", "metadata_provider", "printed_doi_confirmed",
    "title_match_score", "author_match", "year_match",
])
def test_receipt_cannot_omit_any_mandatory_evidence(tmp_path, field):
    pdf = make_pdf(tmp_path / "paper.pdf")
    original = pdf.read_bytes()
    verification = receipt(pdf)
    del verification[field]
    with pytest.raises(ValueError):
        pdf_meta.write_verified_pdf_doi(pdf, verification)
    assert_not_changed(pdf, original)


@pytest.mark.parametrize("overrides", [
    {"policy_version": True}, {"policy_version": 2}, {"decision": "trusted-embedded"},
    {"decision": "pending"}, {"writeback_allowed": 1}, {"printed_doi_confirmed": False},
    {"author_match": "true"}, {"year_match": False}, {"metadata_status": "unavailable"},
    {"metadata_provider": "datacite"}, {"canonical_doi": OTHER}, {"doi": DOI.upper()},
    {"input_sha256": "abcd"}, {"input_sha256": "z" * 64},
    {"title_match_score": 0.979}, {"title_match_score": True},
    {"title_match_score": float("nan")}, {"title_match_score": float("inf")},
    {"source": "pdf-metadata"}, {"source": "csv"}, {"source": "trusted-embedded"},
    {"evidence_source": "filename"}, {"verification_source": "manual-cache"},
    {"status": "pending"}, {"trusted_embedded": True}, {"csv_import": True},
])
def test_unverified_receipts_never_modify_pdf(tmp_path, overrides):
    pdf = make_pdf(tmp_path / "paper.pdf")
    original = pdf.read_bytes()
    with pytest.raises(ValueError):
        pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf, **overrides))
    assert_not_changed(pdf, original)


@pytest.mark.parametrize("info", [
    {"/DOI": OTHER}, {"/DOI": f"{DOI} {OTHER}"}, {"/DOI": "invalid DOI"},
    {"/Subject": f"DOI: {OTHER}"}, {"/DOI": DOI, "/Subject": f"DOI: {OTHER}"},
])
def test_existing_invalid_different_or_conflicting_doi_requires_review(tmp_path, info):
    pdf = make_pdf(tmp_path / "paper.pdf", info=info)
    original = pdf.read_bytes()
    with pytest.raises(ValueError):
        pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))
    assert_not_changed(pdf, original)


@pytest.mark.parametrize("signature", ["perms", "field", "nested", "flags"])
def test_signed_certified_or_signature_field_pdf_is_never_modified(tmp_path, signature):
    pdf = make_pdf(tmp_path / "paper.pdf", signature=signature)
    original = pdf.read_bytes()
    with pytest.raises(ValueError, match="[Ss]ign"):
        pdf_meta.check_pdf_writeback_safety(pdf)
    with pytest.raises(ValueError, match="[Ss]ign"):
        pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))
    assert_not_changed(pdf, original)


def test_encrypted_pdf_is_never_modified(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf", encrypted=True)
    original = pdf.read_bytes()
    with pytest.raises(ValueError, match="Encrypted"):
        pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))
    assert_not_changed(pdf, original)


def test_damaged_pdf_is_never_modified(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\ncompletely broken\n%%EOF")
    original = pdf.read_bytes()
    with pytest.raises(Exception):
        pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))
    assert_not_changed(pdf, original)


@pytest.mark.parametrize("link_type", ["symbolic", "hard"])
def test_automatic_write_refuses_file_links(tmp_path, link_type):
    original_path = make_pdf(tmp_path / "original.pdf")
    pdf = tmp_path / "linked.pdf"
    if link_type == "symbolic":
        pdf.symlink_to(original_path)
    else:
        os.link(original_path, pdf)
    original = original_path.read_bytes()
    with pytest.raises(ValueError, match="link"):
        pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))
    assert pdf.read_bytes() == original_path.read_bytes() == original


def test_automatic_write_refuses_symlinked_parent(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    pdf = make_pdf(directory / "paper.pdf")
    linked = tmp_path / "linked"
    linked.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        pdf_meta.write_verified_pdf_doi(linked / pdf.name, receipt(pdf))


def test_changed_bytes_since_verification_are_not_replaced(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf")
    verification = receipt(pdf)
    pdf.write_bytes(pdf.read_bytes() + b"\n% externally changed\n")
    external = pdf.read_bytes()
    with pytest.raises(ValueError, match="SHA-256"):
        pdf_meta.write_verified_pdf_doi(pdf, verification)
    assert_not_changed(pdf, external)


def test_external_change_during_backup_is_not_overwritten(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf")
    original = pdf.read_bytes()
    verification = receipt(pdf)
    real_backup = pdf_meta._verified_pdf_backup
    external = original + b"\n% concurrent external update\n"

    def change_after_backup(*args):
        backup = real_backup(*args)
        pdf.write_bytes(external)
        return backup

    monkeypatch.setattr(pdf_meta, "_verified_pdf_backup", change_after_backup)
    with pytest.raises(ValueError, match="changed") as failure:
        pdf_meta.write_verified_pdf_doi(pdf, verification)
    assert_not_changed(pdf, external)
    assert next(tmp_path.glob("*.bak")).read_bytes() == original
    assert Path(failure.value.pdf_writeback_backup).read_bytes() == original


def test_same_bytes_in_replaced_inode_cannot_pass_final_identity_check(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf")
    original = pdf.read_bytes()
    real_backup = pdf_meta._verified_pdf_backup

    def replace_after_backup(*args):
        backup = real_backup(*args)
        replacement = tmp_path / "replacement.pdf"
        replacement.write_bytes(original)
        os.replace(replacement, pdf)
        return backup

    monkeypatch.setattr(pdf_meta, "_verified_pdf_backup", replace_after_backup)
    with pytest.raises(ValueError, match="changed"):
        pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))
    assert_not_changed(pdf, original)


def test_backup_creation_failure_leaves_source_untouched(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf")
    original = pdf.read_bytes()
    real_open = pdf_meta.os.open

    def reject_backup(path, *args, **kwargs):
        if str(path).endswith(".bak"):
            raise PermissionError("backup directory is read-only")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(pdf_meta.os, "open", reject_backup)
    with pytest.raises(PermissionError):
        pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))
    assert_not_changed(pdf, original)
    assert not list(tmp_path.glob("*.bak"))


def test_hash_named_existing_backup_is_reused_only_if_complete(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf")
    original = pdf.read_bytes()
    verification = receipt(pdf)
    result = pdf_meta.write_verified_pdf_doi(pdf, verification)
    backup = Path(result["backup"])
    inode = backup.stat().st_ino
    pdf.write_bytes(original)
    second = pdf_meta.write_verified_pdf_doi(pdf, verification)
    assert second["backup"] == str(backup)
    assert backup.stat().st_ino == inode
    assert backup.read_bytes() == original
    pdf.write_bytes(original)
    backup.write_bytes(b"partial backup")
    with pytest.raises(ValueError, match="backup"):
        pdf_meta.write_verified_pdf_doi(pdf, verification)
    assert_not_changed(pdf, original)
    assert backup.read_bytes() == b"partial backup"


def test_bad_incremental_output_does_not_replace_original(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf")
    original = pdf.read_bytes()
    real_writer = pdf_meta.PdfWriter

    class BadWriter(real_writer):
        def write(self, stream):
            buffer = io.BytesIO()
            super().write(buffer)
            stream.write(buffer.getvalue()[1:])

    monkeypatch.setattr(pdf_meta, "PdfWriter", BadWriter)
    with pytest.raises(ValueError, match="original PDF bytes"):
        pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))
    assert_not_changed(pdf, original)


def test_page_count_verification_happens_before_backup_or_replacement(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf")
    original = pdf.read_bytes()
    real_reader = pdf_meta._safe_pdf_reader
    calls = 0

    def alter_page_tree(snapshot):
        nonlocal calls
        reader = real_reader(snapshot)
        calls += 1
        if calls == 2:
            reader.flattened_pages = []
        return reader

    monkeypatch.setattr(pdf_meta, "_safe_pdf_reader", alter_page_tree)
    with pytest.raises(ValueError, match="page integrity"):
        pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))
    assert_not_changed(pdf, original)
    assert not list(tmp_path.glob("*.bak"))


def test_postcommit_directory_sync_warning_reports_committed_file_truthfully(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf")
    real_sync = pdf_meta._sync_pdf_directory
    calls = 0

    def fail_after_commit(directory):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated durability failure")
        real_sync(directory)

    monkeypatch.setattr(pdf_meta, "_sync_pdf_directory", fail_after_commit)
    result = pdf_meta.write_verified_pdf_doi(pdf, receipt(pdf))
    assert result["changed"] is True
    assert result["sha256"] == sha(pdf)
    assert PdfReader(pdf).metadata["/DOI"] == DOI
    assert "durability failure" in result["durability_warning"]


def test_mode_preservation_falls_back_to_path_chmod_without_fchmod(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf")
    monkeypatch.setattr(pdf_meta, "os", SimpleNamespace())
    pdf_meta._set_pdf_mode(-1, pdf, 0o640)
    assert stat.S_IMODE(pdf.stat().st_mode) == 0o640


def test_windows_does_not_attempt_posix_directory_fsync(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_meta, "os", SimpleNamespace(name="nt"))
    pdf_meta._sync_pdf_directory(tmp_path)


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="POSIX lock test")
def test_two_nv_processes_cannot_both_commit_the_same_verified_input(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf")
    original = pdf.read_bytes()
    verification = receipt(pdf)
    context = multiprocessing.get_context("fork")
    start = context.Event()
    outcomes = context.Queue()
    workers = [context.Process(
        target=_concurrent_write_worker, args=(str(pdf), verification, start, outcomes)
    ) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.set()
    try:
        results = [outcomes.get(timeout=15) for _ in workers]
        assert sorted(item[0] for item in results) == ["ok", "rejected"]
        assert next(value for status, value in results if status == "ok") is True
        assert "SHA-256" in next(value for status, value in results if status == "rejected")
    finally:
        for worker in workers:
            worker.join(5)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        outcomes.close()
    assert all(worker.exitcode == 0 for worker in workers)
    assert pdf.read_bytes().startswith(original)
    assert PdfReader(pdf).metadata["/DOI"] == DOI
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
