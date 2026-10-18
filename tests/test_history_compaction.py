"""Compaction keeps pending work, confirmations, and irreversible file audits."""

from netvault.cli.history import History, file_signature
from netvault.cli.upload_state import UploadState


def test_success_cleared_and_unresolved_preserved(tmp_path):
    log = History(tmp_path, "upload")
    server = "https://vault.example"
    done, failed, other = (tmp_path / name for name in
                           ("done.pdf", "failed.pdf", "other.pdf"))
    for file in (done, failed, other):
        file.write_bytes(b"%PDF-1.4\n%test")
    log.write("pending", file=done, result="pending", server=server)
    log.write("error", file=done, result="review", server=server)
    log.write("clear", file=done, server=server)
    log.write("pending", file=failed, server=server, result="pending")
    log.write("error", file=failed, server=server, result="review",
              signature=file_signature(failed))
    log.write("error", file=other, server="https://other", result="failed",
              signature=file_signature(other))
    log.cmd = "tidy"
    log.write("delete", file=tmp_path / "duplicate.pdf", target=done)
    log.cmd = "doi"
    log.write("confirm", file=done, sha256="a" * 64, doi="10.1234/new",
              identity={"doi": "10.1234/new", "source": "user"})
    log.cmd = "upload"
    log.write("summary", scanned=3, counts={"failed": 2})
    before = len(list(log.records()))
    stats = log.compact_uploads()
    journal = list(log.records())
    assert stats["before"] == before and stats["after"] < before
    assert not any(row.get("file") == "done.pdf" and row["cmd"] == "upload"
                   for row in journal)
    assert sum(row["cmd"] == "upload" and row["action"] == "error"
               for row in journal) == 2
    assert any(row["cmd"] == "tidy" and row["action"] == "delete" for row in journal)
    assert any(row["cmd"] == "doi" and row["action"] == "confirm" for row in journal)
    assert log.compact_uploads()["after"] == len(journal)
    with UploadState(root=tmp_path) as state:
        assert state.path_status(failed, server) == "needs-review"
        assert state.path_status(other, "https://other") == "failed"
        assert state.path_status(done, server) is None


def test_legacy_tombstone_clears_error_without_touching_database(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%test")
    database = tmp_path / ".netvault-upload.sqlite3"
    database.write_bytes(b"old binary data not modified")
    log = History(tmp_path, "upload")
    log.write("error", file=pdf, result="review", server="https://vault")
    log.write("clear", file=pdf, server="https://vault",
              signature=file_signature(pdf))
    log.compact_uploads()
    events = list(log.records())
    assert len(events) == 1
    assert events[0]["action"] == "clear"
    assert "sha256" not in events[0]
    with UploadState(root=tmp_path) as state:
        assert state.path_status(pdf, "https://vault") is None
    assert database.read_bytes() == b"old binary data not modified"


def test_only_latest_manual_doi_survives(tmp_path):
    log = History(tmp_path, "doi")
    pdf = tmp_path / "paper.pdf"
    log.write("confirm", file=pdf, sha256="a" * 64, identity={"doi": "10.1234/old"})
    log.write("confirm", file=pdf, sha256="a" * 64, identity={"doi": "10.1234/new"})
    log.write("forget", file=pdf, sha256="b" * 64)
    log.compact_uploads()
    records = list(log.records())
    assert len(records) == 2
    assert next(row for row in records if row["action"] == "confirm")["identity"]["doi"] == "10.1234/new"



def test_500_existing_dois_write_only_one_summary(tmp_path):
    from netvault.cli.upload_report import UploadReport
    paths = [tmp_path / f"paper-{n:04d}.pdf" for n in range(500)]
    journal = History(tmp_path, "upload")
    report = UploadReport(None, paths, history=journal)
    report.server = "https://vault.example"
    for path in paths:
        report.record(path, "existing")
    report.checkpoint(finished=True)
    journal.compact_uploads()
    entries = list(journal.records())
    assert len(entries) == 1
    assert entries[0]["action"] == "summary"
    assert entries[0]["counts"]["existing"] == 500
    assert not (tmp_path / ".netvault-upload.sqlite3").exists()


def test_history_paths_resolve_directory_aliases(tmp_path):
    folder = tmp_path / "actual"
    folder.mkdir()
    shortcut = tmp_path / "alias"
    shortcut.symlink_to(folder, target_is_directory=True)
    pdf = folder / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    history = History(folder, "upload")
    assert history.file_key(shortcut / pdf.name) == "paper.pdf"
    assert history.absolute_path("paper.pdf") == pdf.resolve()
