import csv
import json
import os

import pytest
import requests
from typer.testing import CliRunner

from netvault.cli import user
from netvault.cli.http import ApiError
from netvault.doi import DoiEvidence


REJECTION = "No DOI candidate could be verified against metadata and the PDF title"


@pytest.fixture
def setup_upload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (False, ()))
    monkeypatch.setattr(user, "get_existing_pdfs_by_doi", lambda *a, **k: {})
    state = {"server": "https://vault.example/nv", "server_version": "1.0.0",
             "scans": 0, "uploads": [],
             "error": ApiError(422, REJECTION),
             "evidence": DoiEvidence("ok", "10.1234/article", "pdf-content", [])}
    monkeypatch.setattr(user, "load_credentials", lambda: {"server_url": state["server"]})
    monkeypatch.setattr(user, "api_post", lambda *a, **k: {
        "existing": {}, "server_version": state["server_version"],
    })

    def extract(path):
        state["scans"] += 1
        return state["evidence"]

    def upload(path, **kwargs):
        state["uploads"].append((path, kwargs))
        if state["error"] is not None:
            raise state["error"]
        return {"deduplicated": False,
                "pdf": {"doi": kwargs["doi"] or "10.1234/server", "original_name": path.name}}

    monkeypatch.setattr(user, "extract_doi_evidence", extract)
    monkeypatch.setattr(user, "upload_pdf", upload)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\narticle\n%%EOF")
    return pdf, state


def run_upload(pdf, *options):
    return CliRunner().invoke(user.app, ["upload", str(pdf), *options])


def saved_report(pdf):
    return json.loads((pdf.parent / "nv-upload-report.json").read_text())


def test_rejection_survives_rename_and_explicit_retry_clears_it(setup_upload):
    pdf, state = setup_upload
    first = run_upload(pdf)
    assert first.exit_code == 1, first.output
    assert REJECTION not in first.output and pdf.name not in first.output
    assert "needs review: 1" in first.output
    assert "DOI scans" not in first.output and "average request timing" not in first.output
    row = saved_report(pdf)["files"][0]
    assert row["status"] == "needs-review"
    assert row["sha256"] == user.file_sha256(pdf) and row["error"] == REJECTION

    renamed = pdf.with_name("renamed.pdf")
    pdf.rename(renamed)
    second = run_upload(renamed)
    assert second.exit_code == 0, second.output
    assert "previously rejected: 1 skipped" in second.output
    assert saved_report(renamed)["files"][0]["status"] == "review-skipped"
    assert state["scans"] == len(state["uploads"]) == 1

    state["error"] = None
    retry = run_upload(renamed, "--retry-failed")
    assert retry.exit_code == 0, retry.output
    assert len(state["uploads"]) == 2
    assert saved_report(renamed)["files"][0]["status"] == "uploaded"
    entry = user.load_identity_cache()[user.file_sha256(renamed)]
    assert not entry["upload_rejections"]


@pytest.mark.parametrize("change", ["content", "server", "server-version", "doi", "resolver", "version",
                                   "refresh", "force", "no-crossref"])
def test_changed_identity_conditions_and_overrides_allow_retry(setup_upload, monkeypatch, change):
    pdf, state = setup_upload
    assert run_upload(pdf).exit_code == 1
    options = []
    if change == "content":
        pdf.write_bytes(pdf.read_bytes() + b"\nchanged")
    elif change == "server":
        state["server"] = "https://another.example/nv"
    elif change == "server-version":
        state["server_version"] = "1.0.1"
    elif change == "doi":
        options = ["--doi", "10.1234/corrected"]
    elif change == "resolver":
        monkeypatch.setattr(user, "DOI_RESOLVER_VERSION", user.DOI_RESOLVER_VERSION + 1)
    elif change == "version":
        monkeypatch.setattr(user, "__version__", "999.0.0")
    else:
        options = ["--" + ("refresh-doi" if change == "refresh" else change)]
    state["error"] = None
    result = run_upload(pdf, *options)
    assert result.exit_code == 0, result.output
    assert len(state["uploads"]) == (1 if change in {"version", "server-version"} else 2)


@pytest.mark.parametrize("error", [ApiError(503, "metadata unavailable"),
                                  ApiError(429, "try later"),
                                  ApiError(422, "invalid request field"),
                                  requests.Timeout("connection timeout")])
def test_temporary_or_unrecognized_errors_never_block_future_uploads(setup_upload, error):
    pdf, state = setup_upload
    state["error"] = error
    assert run_upload(pdf).exit_code == 1
    state["error"] = None
    result = run_upload(pdf)
    assert result.exit_code == 0, result.output
    assert len(state["uploads"]) == 2 and state["scans"] == 1


def test_local_missing_doi_gets_server_attempt_before_it_can_be_skipped(setup_upload):
    pdf, state = setup_upload
    state["evidence"] = DoiEvidence("no-doi", None, None, [], "No DOI found")
    state["error"] = ApiError(400, "No DOI found in PDF. Pass --doi DOI when uploading.")
    assert run_upload(pdf).exit_code == 1
    assert state["uploads"][0][1]["doi"] is None
    assert run_upload(pdf).exit_code == 0
    assert len(state["uploads"]) == state["scans"] == 1


def test_corrected_csv_and_repeated_explicit_doi_respect_identity_context(setup_upload):
    pdf, state = setup_upload
    assert run_upload(pdf, "--doi", "10.1234/article").exit_code == 1
    assert run_upload(pdf, "--doi", "10.1234/article").exit_code == 0
    assert len(state["uploads"]) == 1
    csv_path = pdf.parent / "cli-downloads.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "doi", "bytes", "status"])
        writer.writeheader()
        writer.writerow({"file": pdf.name, "doi": "10.1234/article",
                         "bytes": pdf.stat().st_size, "status": "downloaded"})
    state["error"] = None
    result = run_upload(pdf, "--csv", str(csv_path))
    assert result.exit_code == 0, result.output
    assert len(state["uploads"]) == 2
    assert state["uploads"][-1][1]["doi_source"] == "csv-trusted"
    assert user.load_identity_cache()[user.file_sha256(pdf)]["source"] == "user"


def test_existing_server_hash_takes_precedence_over_cached_rejection(setup_upload, monkeypatch):
    pdf, state = setup_upload
    assert run_upload(pdf).exit_code == 1
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *a, **k: {
        user.file_sha256(pdf): {"doi": "10.1234/article", "original_name": pdf.name},
    })
    result = run_upload(pdf)
    assert result.exit_code == 0, result.output
    assert saved_report(pdf)["files"][0]["status"] == "existing"
    assert len(state["uploads"]) == 1


def test_verbose_shows_errors_but_default_only_records_them(setup_upload):
    pdf, _ = setup_upload
    result = run_upload(pdf, "--verbose")
    assert result.exit_code == 1, result.output
    assert pdf.name in result.output and "1 DOI scans" in result.output
    assert " ".join(REJECTION.split()) in " ".join(result.output.split())


def test_one_progress_task_counts_final_outcomes_and_keeps_total_fixed(setup_upload, monkeypatch):
    pdf, state = setup_upload
    for name in ("second.pdf", "third.pdf"):
        pdf.with_name(name).write_bytes(b"%PDF-1.4\n" + name.encode() + b"\n%%EOF")
    snapshots = []

    class TrackedProgress(user.Progress):
        def update(self, task_id, **kwargs):
            result = super().update(task_id, **kwargs)
            task = self.tasks[task_id]
            if task.description == "Processing uploads":
                snapshots.append((len(self.tasks), task.total, task.completed))
            return result

    monkeypatch.setattr(user, "Progress", TrackedProgress)
    result = run_upload(pdf.parent)
    assert result.exit_code == 1, result.output
    assert snapshots and all(count == 1 and total == 3 for count, total, _ in snapshots)
    assert snapshots[-1][2] == 3
    assert saved_report(pdf)["summary"]["needs-review"] == 3
    assert state["scans"] == len(state["uploads"]) == 3


def test_collection_directories_remain_uploadable_unless_explicitly_excluded(tmp_path):
    nested = tmp_path / "deep" / "Unidentifys"
    nested.mkdir(parents=True)
    pdf = nested / "new.pdf"
    pdf.write_bytes(b"%PDF-1.4\nnew article\n%%EOF")
    assert user.collect_pdf_paths([tmp_path]) == [pdf]
    assert user.collect_pdf_paths([tmp_path], {"unidentifys"}) == []
    assert user.collect_pdf_paths([nested], {"unidentifys"}) == [pdf]


def test_same_size_rewrite_with_preserved_mtime_is_not_skipped(setup_upload):
    pdf, state = setup_upload
    assert run_upload(pdf).exit_code == 1
    before = pdf.stat()
    pdf.write_bytes(pdf.read_bytes().replace(b"article", b"changed"))
    os.utime(pdf, ns=(before.st_atime_ns, before.st_mtime_ns))
    state["error"] = None
    result = run_upload(pdf)
    assert result.exit_code == 0, result.output
    assert len(state["uploads"]) == state["scans"] == 2


def test_rename_with_new_doi_hint_allows_retry(setup_upload):
    pdf, state = setup_upload
    assert run_upload(pdf).exit_code == 1
    renamed = pdf.with_name("10.1234_corrected.pdf")
    pdf.rename(renamed)
    state["error"] = None
    result = run_upload(renamed)
    assert result.exit_code == 0, result.output
    assert len(state["uploads"]) == 2
