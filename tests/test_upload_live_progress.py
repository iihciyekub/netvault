"""Large directory uploads must expose real progress without changing DOI policy."""

from threading import Event, Lock, Thread

import pytest
from typer.testing import CliRunner

from netvault.cli import upload_fast, user
from netvault.cli.upload_fast import FastIdentity


def test_scan_stream_reports_fast_files_before_blocked_first_pdf(tmp_path, monkeypatch):
    paths = [tmp_path / f"paper-{index}.pdf" for index in range(30)]
    for path in paths:
        path.write_bytes(b"%PDF-1.5\n%%EOF\n")
    started, release = Event(), Event()
    calls = []
    lock = Lock()
    monkeypatch.setattr(user, "doi_scan_workers", lambda count: min(3, count))

    def probe(path):
        with lock:
            calls.append(path)
        if path == paths[0]:
            started.set()
            assert release.wait(5), "slow first PDF should be released by test"
        if path == paths[3]:
            raise ValueError("conflicting DOI fields")
        return FastIdentity(f"10.1234/{path.stem}", "pdf-metadata")

    monkeypatch.setattr(user, "probe_pdf", probe)
    iterator = user.iter_scan_upload_dois(paths)
    try:
        index, outcome = next(iterator)
        assert started.wait(2)
        assert index != 0, "a slow first PDF must not block completed later PDFs"
        assert outcome[1] is None
        # No thousands of queued PDF scans ahead of actual consumption.
        with lock:
            assert len(calls) <= 6
    finally:
        release.set()
    records = {index: result for index, result in [(index, outcome), *iterator]}
    assert len(records) == len(paths)
    assert records[3] == (None, "conflicting DOI fields")
    assert records[0][0].doi == "10.1234/paper-0"
    # Historical synchronous callers still get their original input order.
    ordered = user.scan_upload_dois(paths)
    assert ordered[0][0].doi == "10.1234/paper-0"
    assert ordered[3][1] == "conflicting DOI fields"


def test_title_checks_report_each_finished_lookup_and_preserve_identity(tmp_path, monkeypatch):
    paths = [tmp_path / f"paper-{i}.pdf" for i in range(5)]
    title = "An Original Research Article About Learning Progress"
    identities = {
        path: FastIdentity(f"10.1234/title.{i % 4}", "pdf-content", title)
        for i, path in enumerate(paths)
    }
    blocked, release, early_progress = Event(), Event(), Event()
    queried = []
    events = []
    lock = Lock()

    def fetch(doi):
        with lock:
            queried.append(doi)
        if doi.endswith(".0"):
            blocked.set()
            assert release.wait(5)
        return title

    def callback(stage, completed, total):
        events.append((stage, completed, total))
        if stage == "Looking up DOI titles" and completed >= 1 and not release.is_set():
            early_progress.set()

    monkeypatch.setattr(upload_fast, "fetch_csl_title", fetch)
    results = {}
    errors = []

    def run():
        try:
            results.update(upload_fast.check_new_titles(identities, progress_callback=callback))
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=run, daemon=True)
    thread.start()
    try:
        assert blocked.wait(2)
        assert early_progress.wait(3), "other completed DOI lookups should update progress"
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors
    assert len(queried) == 4, "each unique DOI is looked up only once"
    assert results == {path: True for path in paths}
    assert ("Looking up DOI titles", 4, 4) in events
    assert ("Matching PDF titles", 5, 5) in events


class RecordingProgress:
    """Capture Rich stage advances without terminal timing dependencies."""

    def __init__(self, *args, **kwargs):
        self.description = ""
        self.completed = 0
        self.events = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def add_task(self, description, **kwargs):
        self.description = description
        self.completed = 0
        self.events.append(("stage", description, kwargs.get("total")))
        return 1

    def reset(self, task, *, description=None, completed=0, total=None, **kwargs):
        self.description = description or self.description
        self.completed = completed
        self.events.append(("stage", self.description, total))

    def update(self, task, *, completed=None, **kwargs):
        if completed is not None:
            self.completed = completed
            self.events.append(("advance", self.description, self.completed))

    def advance(self, task, amount=1):
        self.completed += amount
        self.events.append(("advance", self.description, self.completed))


def test_large_existing_directory_updates_each_preflight_phase(tmp_path, monkeypatch):
    records = []
    for index in range(551):
        path = tmp_path / f"paper-{index:04d}.pdf"
        path.write_bytes(b"%PDF-1.5\n%%EOF\n")
        records.append(path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identities.json")
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "load_credentials", lambda: {"server_url": "https://vault.test/nv"})
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (False, ()))
    monkeypatch.setattr(user, "doi_scan_workers", lambda count: min(3, count))
    monkeypatch.setattr(
        user, "probe_pdf",
        lambda path: FastIdentity(f"10.1234/progress.{path.stem}", "pdf-metadata"),
    )
    lookup_sizes = []

    def existing_dois(values):
        values = list(values)
        lookup_sizes.append(len(values))
        return {
            doi: {"doi": doi, "title": "Existing research paper", "original_name": "paper.pdf"}
            for doi in values
        }

    monkeypatch.setattr(user, "get_existing_pdfs_by_doi", existing_dois)
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *args, **kwargs: {})
    monkeypatch.setattr(user, "upload_pdf", lambda *args, **kwargs: pytest.fail("DOI exists"))
    progress = RecordingProgress()
    monkeypatch.setattr(user, "Progress", lambda *args, **kwargs: progress)
    result = CliRunner().invoke(user.app, ["upload", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "already stored: 551 skipped" in result.output
    assert sorted(lookup_sizes) == [51, 500]
    maximums = {}
    for kind, stage, amount in progress.events:
        if kind == "advance":
            maximums[stage] = max(amount, maximums.get(stage, 0))
    assert maximums["DOI preflight"] == 551
    assert maximums["Checking stored DOIs on server"] == 551
    assert maximums["Classifying DOI matches"] == 551
    assert maximums["Recording DOI decisions"] == 551


def test_async_scan_preserves_original_first_doi_upload(tmp_path, monkeypatch):
    from time import sleep

    first, second = tmp_path / "a-first.pdf", tmp_path / "b-second.pdf"
    first.write_bytes(b"%PDF-1.5\nA\n%%EOF")
    second.write_bytes(b"%PDF-1.5\nB\n%%EOF")
    second_done = Event()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identity.json")
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "load_credentials", lambda: {"server_url": "https://vault.test/nv"})
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (False, ()))
    monkeypatch.setattr(user, "doi_scan_workers", lambda count: 2)

    def probe(path):
        if path == first:
            assert second_done.wait(5)
            sleep(0.03)
        else:
            second_done.set()
        return FastIdentity("10.1234/same.paper", "pdf-content", "Matching real article text")

    monkeypatch.setattr(user, "probe_pdf", probe)
    monkeypatch.setattr(user, "check_new_titles", lambda rows, **kwargs: {p: True for p in rows})
    monkeypatch.setattr(user, "get_existing_pdfs_by_doi", lambda values: {})
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *args, **kwargs: {})
    uploads = []

    def upload(path, **kwargs):
        uploads.append(path)
        return {
            "pdf": {"doi": kwargs["doi"], "original_name": path.name},
            "deduplicated": False,
        }

    monkeypatch.setattr(user, "upload_pdf", upload)
    output = CliRunner().invoke(user.app, ["upload", str(tmp_path)])
    assert output.exit_code == 0, output.output
    assert uploads == [first], "probe finish order must not change DOI selection"
    assert "local duplicate paths: 1" in output.output
