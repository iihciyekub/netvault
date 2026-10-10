from pathlib import Path
from threading import Barrier, Event, Lock

from typer.testing import CliRunner

from netvault.cli.upload_pipeline import UploadPipeline, UploadStats
from netvault.cli import user
from netvault.doi import DoiEvidence


def test_uploads_overlap_stay_bounded_and_continue_after_failure():
    barrier = Barrier(3, timeout=5)
    lock = Lock()
    active = peak = 0
    stats = UploadStats()

    def upload(path, *, progress_callback, timing_callback):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait()
            progress_callback(4, 10)
            progress_callback(10, 10)
            timing_callback(0.5, 0.2, {"parse": 0.1})
            if path.name == "1.pdf":
                raise RuntimeError("provider unavailable")
            return {"pdf": {"doi": path.stem}, "deduplicated": False}
        finally:
            with lock:
                active -= 1

    outcomes = []
    with UploadPipeline(upload, 3, stats) as pipeline:
        for index in range(6):
            outcomes.extend(pipeline.submit(Path(f"{index}.pdf")))
            assert len(pipeline.pending) <= 3
        outcomes.extend(pipeline.drain())

    assert peak == 3
    assert len(outcomes) == 6
    assert sum(outcome.error is not None for outcome in outcomes) == 1
    assert stats.sent == 60
    assert stats.active == 0
    assert stats.measured == 6
    assert stats.server_timings["parse"][1] == 6


def test_cli_starts_upload_before_all_doi_scans_and_keeps_automatic_verification(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash-cache.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identity-cache.json")
    monkeypatch.setattr(user, "UPLOAD_PREPARE_BATCH_SIZE", 2)
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (False, ()))
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *args, **kwargs: {})
    # Automatic DOI hints must still reach server verification, even if a DOI exists.
    monkeypatch.setattr(user, "get_existing_pdfs_by_doi", lambda *args, **kwargs: {
        "10.1234/article": {"sha256": "another-file"},
    })
    first_upload = Event()
    third_scan = Event()
    scans = uploads = 0
    lock = Lock()
    for index in range(4):
        (tmp_path / f"{index}.pdf").write_bytes(f"%PDF-1.4\n{index}\n%%EOF".encode())

    def extract(path):
        nonlocal scans
        scans += 1
        if scans == 3:
            assert first_upload.wait(5), "upload waited for every DOI scan"
            third_scan.set()
        return DoiEvidence("ok", "10.1234/article", "pdf-content", [])

    def upload(path, **kwargs):
        nonlocal uploads
        first_upload.set()
        assert third_scan.wait(5)
        with lock:
            uploads += 1
        assert kwargs["doi_source"] == "pdf-content"
        assert kwargs["sha256"]
        return {"pdf": {"doi": kwargs["doi"], "original_name": path.name}, "deduplicated": False}

    monkeypatch.setattr(user, "extract_doi_evidence", extract)
    monkeypatch.setattr(user, "upload_pdf", upload)
    result = CliRunner().invoke(user.app, ["upload", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert scans == uploads == 4
    assert "uploaded: 4" in result.output


def test_force_uploads_preserve_input_order():
    calls = []

    def upload(path, **_kwargs):
        calls.append(path.name)
        return {}

    with UploadPipeline(upload, 1, UploadStats()) as pipeline:
        for index in range(5):
            pipeline.submit(Path(f"{index}.pdf"))
        list(pipeline.drain())
    assert calls == [f"{index}.pdf" for index in range(5)]


def test_http_reports_sent_bytes_and_server_timing(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from netvault.cli import http

    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4\n" + b"x" * 32000 + b"\n%%EOF")
    monkeypatch.setattr(http, "auth_headers", lambda: {})
    monkeypatch.setattr(http, "server_url", lambda: "https://example.test")
    clock = iter([1.0, 2.0, 3.0])
    monkeypatch.setattr(http, "monotonic", lambda: next(clock))

    def post(_url, **kwargs):
        assert kwargs["headers"]["Idempotency-Key"] == "test-sha"
        while kwargs["data"].read(8192):
            pass
        return SimpleNamespace(
            ok=True, headers={"Server-Timing": "parse;dur=100, verify;dur=250, bad;dur=oops"},
            json=lambda: {"deduplicated": False},
        )

    monkeypatch.setattr(http, "http_session", lambda: SimpleNamespace(post=post))
    progress, timing = [], []
    http.upload_pdf(path, sha256="test-sha", progress_callback=lambda *args: progress.append(args),
                    timing_callback=lambda *args: timing.append(args))
    assert progress[-1][0] == progress[-1][1]
    assert progress[-1][0] >= path.stat().st_size
    assert timing == [(1.0, 1.0, {"parse": 0.1, "verify": 0.25})]
