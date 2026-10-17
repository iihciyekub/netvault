"""Admin Console security, review binding and job queue regression tests.

All tests use SQLite and temporary storage; no SSH, Docker or production PDFs.
"""
from datetime import timedelta
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NETVAULT_DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    monkeypatch.setenv("NETVAULT_BASE_PATH", "")
    monkeypatch.setenv("NETVAULT_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("NETVAULT_SECRET_KEY", "admin-test-secret")
    monkeypatch.setenv("NETVAULT_BOOTSTRAP_ADMIN", "admin")
    monkeypatch.setenv("NETVAULT_BOOTSTRAP_ADMIN_PASSWORD", "admin-password")
    for module in list(sys.modules):
        if module.startswith("netvault_server.server"):
            del sys.modules[module]
    main = importlib.import_module("netvault_server.server.main")
    with TestClient(main.app) as test_client:
        yield test_client


def web_login(client: TestClient, username="admin", password="admin-password"):
    client.get("/web/login")
    csrf = client.cookies["netvault_csrf"]
    response = client.post("/web/login", data={
        "username": username, "password": password, "csrf_token": csrf,
    })
    assert response.status_code == 200


def queued_job(client, kind="audit", limit=10):
    response = client.post("/web/admin/maintenance/start", data={
        "kind": kind, "limit": limit, "csrf_token": client.cookies["netvault_csrf"],
    }, follow_redirects=False)
    assert response.status_code == 303
    models = importlib.import_module("netvault_server.server.models")
    database = importlib.import_module("netvault_server.server.database")
    with database.SessionLocal() as db:
        job = db.scalar(select(models.MaintenanceJob).where(
            models.MaintenanceJob.kind == kind
        ))
        assert job is not None
        return job.id


def completed_report(job_id, report: dict):
    models = importlib.import_module("netvault_server.server.models")
    database = importlib.import_module("netvault_server.server.database")
    maintenance = importlib.import_module("netvault_server.server.maintenance")
    path = maintenance.report_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(report).encode()
    path.write_bytes(data)
    with database.SessionLocal() as db:
        job = db.get(models.MaintenanceJob, job_id)
        job.status = "completed"
        job.finished_at = models.utc_now()
        job.result_sha256 = hashlib.sha256(data).hexdigest()
        summary = report.get("counts", {}) if job.kind == "audit" else {
            "verified": report.get("verified", 0),
            "applied": report.get("applied", 0),
        }
        if job.kind == "audit":
            summary = {**summary, "candidates": len(report["compression_candidates"])}
        job.result_json = json.dumps(summary)
        db.commit()
    return path


def audit_report():
    return {
        "read_only": True, "counts": {"needs_compression": 2, "needs_bibliography": 3},
        "compression_candidates": [
            {"id": 10, "sha256": "a" * 64, "doi": "10.1234/a"},
            {"id": 11, "sha256": "b" * 64, "doi": "10.1234/b"},
        ],
        "bibliography_candidates": [],
    }


def test_admin_console_authorization_and_csrf(client):
    assert client.get("/web/admin").status_code == 200  # redirects to login
    assert client.get("/web/admin").url.path == "/web/login"
    assert client.get("/web/admin/maintenance/state").status_code == 401
    assert client.post("/web/admin/maintenance/start", data={
        "kind": "audit", "limit": 2,
    }, follow_redirects=False).status_code == 303

    web_login(client)
    assert client.post("/web/admin/maintenance/start", data={
        "kind": "audit", "limit": 2, "csrf_token": "forged",
    }).status_code == 403
    assert client.post("/web/admin/maintenance/start", data={
        "kind": "arbitrary-command", "limit": 2, "csrf_token": client.cookies["netvault_csrf"],
    }).status_code == 400

    page = client.get("/web/admin")
    assert page.status_code == 200
    assert "Library statistics" in page.text
    assert "PostgreSQL size" in page.text
    assert 'aria-current="page"' in page.text
    assert 'href="/web/admin/jobs"' in page.text
    assert 'href="/web/admin/users"' in page.text
    assert "Create User" not in page.text
    assert "Start Full Library Optimization + DOI" not in page.text
    assert "/static/admin-maintenance.js?v=" in page.text
    assert "&amp;h=" in page.text
    assert "Offline / not configured" in page.text
    actions = client.get("/web/admin/maintenance")
    assert actions.status_code == 200
    assert "Run Read-Only Audit" in actions.text
    assert "Preview Metadata Changes" in actions.text
    assert "Full Library Optimization + DOI Writeback" in actions.text
    assert "User Management" not in actions.text
    assert "/static/admin-maintenance.js" not in actions.text
    people = client.get("/web/admin/users")
    assert people.status_code == 200
    assert "Team accounts" in people.text
    assert "Create User" in people.text
    assert "Reset Password" in people.text
    assert "Run Read-Only Audit" not in people.text
    history = client.get("/web/admin/jobs")
    assert history.status_code == 200
    assert "Task History" in history.text
    assert 'data-tooltip="Admin Console"' in page.text
    assert 'class="sidebar-admin-caption">Admin</span>' in page.text
    assert 'data-no-pjax' in page.text
    dashboard = client.get("/web")
    assert dashboard.status_code == 200
    assert 'class="dashboard-admin-console"' not in dashboard.text
    assert 'class="sidebar-icon sidebar-admin-link' in dashboard.text
    assert 'href="/web/admin" data-no-pjax' in dashboard.text
    assert 'data-tooltip="Admin Console"' in dashboard.text

    models = importlib.import_module("netvault_server.server.models")
    database = importlib.import_module("netvault_server.server.database")
    security = importlib.import_module("netvault_server.server.security")
    with database.SessionLocal() as db:
        db.add(models.User(username="reader", password_hash=security.hash_password("reader-secret"),
                           role=models.UserRole.user))
        db.commit()

    client.post("/web/logout", data={"csrf_token": client.cookies["netvault_csrf"]})
    web_login(client, "reader", "reader-secret")
    assert client.get("/web/admin").status_code == 403
    for target in ("/web/admin/maintenance", "/web/admin/jobs", "/web/admin/users"):
        assert client.get(target).status_code == 403
    assert client.get("/web/admin/maintenance/state").status_code == 403
    assert client.get("/web/admin/maintenance/jobs/" + "a" * 32 + "/report").status_code == 403
    assert client.post("/web/admin/maintenance/start", data={
        "kind": "audit", "limit": 2, "csrf_token": client.cookies["netvault_csrf"],
    }).status_code == 403
    reader_dashboard = client.get("/web")
    assert reader_dashboard.status_code == 200
    assert 'data-tooltip="Admin Console"' not in reader_dashboard.text
    assert 'dashboard-admin-console' not in reader_dashboard.text
    assert 'sidebar-admin-caption' not in reader_dashboard.text


def test_read_only_audit_job_compression_approval_is_bounded_and_single_use(client):
    web_login(client)
    job_id = queued_job(client, kind="audit", limit=25)
    assert client.get("/web/admin/maintenance/state").json()["running"]
    busy = client.post("/web/admin/maintenance/start", data={
        "kind": "metadata_preview", "limit": 2, "csrf_token": client.cookies["netvault_csrf"],
    })
    assert busy.status_code == 409

    completed_report(job_id, audit_report())
    report = client.get(f"/web/admin/maintenance/jobs/{job_id}/report")
    assert report.status_code == 200
    assert report.json()["read_only"]
    page = client.get("/web/admin/jobs")
    assert "COMPRESS PDFS" in page.text
    assert "2 to optimize" in page.text
    assert client.post(f"/web/admin/maintenance/jobs/{job_id}/approve", data={
        "operation": "compression", "limit": 3,
        "confirmation": "COMPRESS PDFS", "csrf_token": client.cookies["netvault_csrf"],
    }).status_code == 400
    assert client.post(f"/web/admin/maintenance/jobs/{job_id}/approve", data={
        "operation": "compression", "limit": 2,
        "confirmation": "compress pdfs", "csrf_token": client.cookies["netvault_csrf"],
    }).status_code == 400

    approved = client.post(f"/web/admin/maintenance/jobs/{job_id}/approve", data={
        "operation": "compression", "limit": 2,
        "confirmation": "COMPRESS PDFS", "csrf_token": client.cookies["netvault_csrf"],
    }, follow_redirects=False)
    assert approved.status_code == 303
    models = importlib.import_module("netvault_server.server.models")
    database = importlib.import_module("netvault_server.server.database")
    with database.SessionLocal() as db:
        job = db.scalar(select(models.MaintenanceJob).where(
            models.MaintenanceJob.kind == "compression"
        ))
        assert job.source_job_id == job_id
        assert json.loads(job.options_json)["approved_digest"] == db.get(
            models.MaintenanceJob, job_id
        ).result_sha256
        assert json.loads(job.options_json)["limit"] == 2
    assert client.post(f"/web/admin/maintenance/jobs/{job_id}/approve", data={
        "operation": "compression", "limit": 1,
        "confirmation": "COMPRESS PDFS", "csrf_token": client.cookies["netvault_csrf"],
    }).status_code == 409


def test_metadata_preview_requires_verified_bound_report(client):
    web_login(client)
    job_id = queued_job(client, kind="metadata_preview", limit=5)
    report = {"mode": "preview", "selected_rows": 2, "verified": 1, "applied": 0,
              "proposals": [
                  {"id": 25, "doi": "10.1000/doc", "sha256": "c" * 64,
                   "decision": "verified", "fill_only": {"authors": "E. Example"}},
                  {"id": 26, "doi": "10.1000/other", "sha256": "d" * 64,
                   "decision": "review", "reason": "identity cannot be confirmed"},
              ]}
    path = completed_report(job_id, report)
    assert client.get("/web/admin").status_code == 200

    path.write_text('{"mode": "preview", "verified": 2}', encoding="utf-8")
    blocked = client.post(f"/web/admin/maintenance/jobs/{job_id}/approve", data={
        "operation": "metadata_apply", "confirmation": "APPLY METADATA",
        "csrf_token": client.cookies["netvault_csrf"],
    })
    assert blocked.status_code == 409
    completed_report(job_id, report)
    approved = client.post(f"/web/admin/maintenance/jobs/{job_id}/approve", data={
        "operation": "metadata_apply", "confirmation": "APPLY METADATA",
        "csrf_token": client.cookies["netvault_csrf"],
    }, follow_redirects=False)
    assert approved.status_code == 303
    models = importlib.import_module("netvault_server.server.models")
    database = importlib.import_module("netvault_server.server.database")
    with database.SessionLocal() as db:
        assert db.scalar(select(models.MaintenanceJob).where(
            models.MaintenanceJob.kind == "metadata_apply"
        )).source_job_id == job_id


def test_approval_expires_and_worker_command_fails_closed(client, tmp_path, monkeypatch):
    web_login(client)
    job_id = queued_job(client)
    completed_report(job_id, audit_report())
    models = importlib.import_module("netvault_server.server.models")
    database = importlib.import_module("netvault_server.server.database")
    with database.SessionLocal() as db:
        job = db.get(models.MaintenanceJob, job_id)
        job.finished_at = models.utc_now() - timedelta(minutes=61)
        db.commit()
    response = client.post(f"/web/admin/maintenance/jobs/{job_id}/approve", data={
        "operation": "compression", "limit": 2,
        "confirmation": "COMPRESS PDFS", "csrf_token": client.cookies["netvault_csrf"],
    })
    assert response.status_code == 409

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    worker = importlib.import_module("run_admin_maintenance_worker")
    worker.TMP = tmp_path / "storage" / "tmp" / "admin-maintenance"
    worker.TMP.mkdir(parents=True, exist_ok=True)
    with database.SessionLocal() as db:
        audit = db.get(models.MaintenanceJob, job_id)
        cmd = worker.command_for(db, audit, None)
        assert cmd[1] == "scripts/audit_pdf_maintenance.py"
        assert "--output" in cmd
        audit.options_json = json.dumps({"limit": 500})
        db.commit()
        with pytest.raises(ValueError, match="unsafe"):
            worker.command_for(db, audit, None)
    with pytest.raises(ValueError, match="fingerprint"):
        worker._approved_candidates({
            "read_only": True, "compression_candidates": [
                {"id": 3, "sha256": "not-a-hash"}
            ],
        }, 1)


def test_metadata_apply_only_changes_exact_approved_proposals(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    repair = importlib.import_module("repair_pdf_bibliography")
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    def candidate(ident, sha):
        return {
            "id": ident, "doi": f"10.5555/{ident}", "sha256": sha, "size": 100,
            "storage_path": f"objects/{sha[:2]}/{sha}.pdf", "crossref_status": "pending",
            "title": None, "authors": None, "container_title": None, "publisher": None,
            "published_year": None, "crossref_url": None,
        }
    original = candidate(10, "a" * 64)
    newer = candidate(11, "b" * 64)
    monkeypatch.setattr(repair, "get_candidates", lambda *a, **k: [original, newer])
    applied = []
    monkeypatch.setattr(repair, "propose", lambda row, root: ({
        "id": row["id"], "doi": row["doi"], "sha256": row["sha256"],
        "decision": "verified", "fill_only": {"authors": "Verified Author"},
    }, object()))
    monkeypatch.setattr(repair, "apply_if_unchanged", lambda db, row, proposal, metadata: (
        applied.append(row["id"]) or "applied: test"
    ))
    reviewed = [{
        "id": 10, "doi": original["doi"], "sha256": original["sha256"],
        "decision": "verified", "fill_only": {"authors": "Verified Author"},
    }]
    updates = []
    result = repair.run(
        engine, limit=2, apply=True, approved_proposals=reviewed,
        progress=updates.append,
    )
    assert result["applied"] == 1
    assert applied == [10]
    assert updates == [
        {"mode": "metadata_apply", "total": 1, "processed": 0, "verified": 0, "applied": 0},
        {"mode": "metadata_apply", "total": 1, "processed": 1, "verified": 1, "applied": 1},
    ]

    reviewed[0]["fill_only"] = {"authors": "Different Author"}
    applied.clear()
    result = repair.run(engine, limit=2, apply=True, approved_proposals=reviewed)
    assert result["applied"] == 0
    assert applied == []
    assert result["proposals"][0]["decision"] == "review"
    with pytest.raises(ValueError, match="candidates have changed"):
        repair.run(engine, limit=2, apply=True, approved_proposals=[
            {**reviewed[0], "id": 999},
        ])


def test_metadata_progress_reports_only_bounded_counters(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    worker = importlib.import_module("run_admin_maintenance_worker")
    worker.TMP = tmp_path
    job = SimpleNamespace(
        id="d" * 32, kind="metadata_preview",
        options_json=json.dumps({"limit": 10}), result_json=None,
    )
    path = worker.TMP / f"{job.id}.progress.json"

    class FakeDb:
        committed = 0

        def commit(self):
            self.committed += 1

    db = FakeDb()
    path.write_text(json.dumps({
        "mode": "metadata_preview", "total": 4, "processed": 2,
        "verified": 1, "applied": 0, "doi": "should never leave host progress",
    }))
    worker.refresh_progress(db, job)
    assert db.committed == 1
    assert json.loads(job.result_json) == {
        "total": 4, "processed": 2, "verified": 1, "applied": 0,
    }
    assert "doi" not in job.result_json
    assert importlib.import_module(
        "netvault_server.server.maintenance"
    ).jobs_revision([{
        "id": "abc", "status": "running", "summary": {"processed": 2},
    }]) == "abc:running:2:0"
    path.write_text(json.dumps({
        "mode": "metadata_preview", "total": 4, "processed": 5,
        "verified": 1, "applied": 0,
    }))
    worker.refresh_progress(db, job)
    assert db.committed == 1


def test_compression_preflights_all_approved_fingerprints_before_any_write(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    compression = importlib.import_module("compress_pdfs_parallel")
    monkeypatch.setattr(compression, "TMP", tmp_path / "tmp")
    monkeypatch.setattr(compression, "LOCK", tmp_path / "tmp" / "pdfcraft.lock")
    approved = tmp_path / "approved.json"
    approved.write_text(json.dumps({"candidates": [
        {"id": 10, "sha256": "a" * 64},
        {"id": 11, "sha256": "b" * 64},
    ]}), encoding="utf-8")
    args = SimpleNamespace(approved_candidates=approved, limit=2, jobs=1, pilot=True,
                           result_output=tmp_path / "result.json")

    class FakeCursor:
        def __init__(self):
            self.current_id = None

        def __enter__(self):
            return self

        def __exit__(self, *unused):
            pass

        def execute(self, sql, values):
            self.current_id = values[0]
            assert "sha256=%s" in sql
            assert "pdfcraft_processed_sha256 IS DISTINCT FROM sha256" in sql

        def fetchone(self):
            if self.current_id == 11:
                return None  # Changed between audit and approval.
            return (10, "10.5555/doc", "a" * 64, 120, "objects/aa/" + "a" * 64 + ".pdf")

    class FakeDb:
        def __enter__(self):
            return self

        def __exit__(self, *unused):
            pass

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(compression, "connect_db", FakeDb)
    modified = []
    monkeypatch.setattr(compression, "process_one", lambda row, pilot: (
        modified.append(row[0]) or {"status": "unchanged", "old": 120, "new": 120}, None
    ))
    with pytest.raises(ValueError, match="changed since audit"):
        compression.run_approved(args)
    assert modified == []
    assert not args.result_output.exists()

    approved.write_text(json.dumps({"candidates": [
        {"id": 10, "sha256": "a" * 64},
    ]}), encoding="utf-8")
    args.limit = 1
    assert compression.run_approved(args) == 0
    assert modified == [10]
    assert json.loads(args.result_output.read_text())["attempted"] == 1


def test_worker_maintains_liveness_and_inherits_lock_during_long_task(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    worker = importlib.import_module("run_admin_maintenance_worker")
    commands = []
    calls = []

    class Process:
        def __init__(self):
            self.waits = 0

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(["python"], timeout)
            return 0

    def fake_popen(cmd, **kwargs):
        commands.append((cmd, kwargs))
        return Process()

    monkeypatch.setattr(worker.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(worker, "heartbeat", lambda: calls.append("heartbeat"))
    monkeypatch.setattr(worker, "refresh_progress",
                        lambda db, job: calls.append("progress"))
    result = worker.run_host_command(
        ["python", "--test"], None, kind="audit", db=None,
        job=SimpleNamespace(), worker_lock_fd=37,
    )
    assert result == 0
    assert commands[0][1]["start_new_session"] is True
    assert commands[0][1]["pass_fds"] == (37,)
    assert calls.count("heartbeat") >= 2


def test_worker_timeout_terminates_entire_process_group(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    worker = importlib.import_module("run_admin_maintenance_worker")
    signals = []

    class Process:
        pid = 9876

        def poll(self):
            return None

        def wait(self, timeout):
            assert timeout == 45
            return 0

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(worker.os, "killpg", lambda pid, signal: signals.append((pid, signal)))
    monkeypatch.setattr(worker, "heartbeat", lambda: None)
    monkeypatch.setattr(worker, "refresh_progress", lambda db, job: None)
    monkeypatch.setitem(worker.TIMEOUTS, "audit", 0)
    with pytest.raises(subprocess.TimeoutExpired):
        worker.run_host_command(
            ["python", "--audit"], None, kind="audit", db=None,
            job=SimpleNamespace(), worker_lock_fd=12,
        )
    assert signals == [(9876, worker.signal.SIGTERM)]


def test_live_compression_progress_changes_revision_and_admin_page(client, tmp_path, monkeypatch):
    web_login(client)
    audit_id = queued_job(client, "audit", 10)
    completed_report(audit_id, audit_report())
    response = client.post(f"/web/admin/maintenance/jobs/{audit_id}/approve", data={
        "operation": "compression", "limit": 2,
        "confirmation": "COMPRESS PDFS", "csrf_token": client.cookies["netvault_csrf"],
    }, follow_redirects=False)
    assert response.status_code == 303
    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    worker = importlib.import_module("run_admin_maintenance_worker")
    worker.TMP = tmp_path / "storage" / "tmp" / "admin-maintenance"
    worker.TMP.mkdir(parents=True, exist_ok=True)
    state_before = client.get("/web/admin/maintenance/state").json()["revision"]
    with database.SessionLocal() as db:
        job = db.scalar(select(models.MaintenanceJob).where(
            models.MaintenanceJob.kind == "compression"
        ))
        job.status = "running"
        db.commit()
        path = worker.TMP / f"{job.id}.progress.json"
        path.write_text(json.dumps({
            "mode": "approved_compression", "total": 2, "attempted": 1,
            "replaced": 1, "unchanged": 0, "failed": 0, "saved_bytes": 128,
        }), encoding="utf-8")
        worker.refresh_progress(db, job)
        assert json.loads(job.result_json)["attempted"] == 1
    state_after = client.get("/web/admin/maintenance/state").json()["revision"]
    assert state_before != state_after
    assert "1/2 processed" in client.get("/web/admin/jobs").text


def test_full_library_requires_admin_csrf_explicit_acknowledgement_and_unique_queue(
    client, monkeypatch,
):
    assert client.post("/web/admin/maintenance/full", data={
        "jobs": "3", "confirmation": "RUN FULL LIBRARY",
        "acknowledge_no_backup": "true",
    }, follow_redirects=False).status_code == 303
    web_login(client)
    maintenance = importlib.import_module("netvault_server.server.maintenance")
    monkeypatch.setattr(maintenance, "worker_is_online", lambda: True)
    page = client.get("/web/admin/maintenance")
    assert "Full Library Optimization + DOI Writeback" in page.text
    assert "scripts/compress_pdfs_parallel.py --all --jobs 3" in page.text
    assert "START FULL LIBRARY" not in page.text
    assert "Start Full Library Optimization + DOI" in page.text

    path = "/web/admin/maintenance/full"
    form = {
        "jobs": "3", "confirmation": "RUN FULL LIBRARY",
        "acknowledge_no_backup": "true",
        "csrf_token": client.cookies["netvault_csrf"],
    }
    assert client.post(path, data={**form, "csrf_token": "wrong"}).status_code == 403
    assert client.post(path, data={**form, "confirmation": "run full library"}).status_code == 400
    assert client.post(path, data={k:v for k,v in form.items() if k != "acknowledge_no_backup"}).status_code == 400
    assert client.post(path, data={**form, "jobs": "12"}).status_code == 400
    assert client.post(path, data=form, follow_redirects=False).status_code == 303
    assert client.post(path, data=form).status_code == 409

    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    with database.SessionLocal() as db:
        task = db.scalar(select(models.MaintenanceJob).where(
            models.MaintenanceJob.kind == "full_compression"
        ))
        assert task.status == "queued"
        assert task.source_job_id is None
        assert json.loads(task.options_json) == {"jobs": 3, "no_backup": True}
        task_id = task.id
    assert "Full Compression" in client.get("/web/admin/jobs").text

    stop = f"/web/admin/maintenance/jobs/{task_id}/stop"
    assert client.post(stop, data={
        "csrf_token": client.cookies["netvault_csrf"],
        "confirmation": "wrong",
    }).status_code == 400
    assert client.post(stop, data={
        "csrf_token": client.cookies["netvault_csrf"],
        "confirmation": "STOP FULL RUN",
    }, follow_redirects=False).status_code == 303
    with database.SessionLocal() as db:
        task = db.get(models.MaintenanceJob, task_id)
        assert task.status == "cancelled" and task.cancel_requested
    assert client.post(stop, data={
        "csrf_token": client.cookies["netvault_csrf"],
        "confirmation": "STOP FULL RUN",
    }).status_code == 409


def test_full_library_stop_running_keeps_job_lock_and_is_admin_only(client, monkeypatch):
    web_login(client)
    maintenance = importlib.import_module("netvault_server.server.maintenance")
    monkeypatch.setattr(maintenance, "worker_is_online", lambda: True)
    form = {
        "jobs": "2", "confirmation": "RUN FULL LIBRARY",
        "acknowledge_no_backup": "true",
        "csrf_token": client.cookies["netvault_csrf"],
    }
    assert client.post("/web/admin/maintenance/full", data=form, follow_redirects=False).status_code == 303
    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    security = importlib.import_module("netvault_server.server.security")
    with database.SessionLocal() as db:
        task = db.scalar(select(models.MaintenanceJob).where(
            models.MaintenanceJob.kind == "full_compression"
        ))
        task.status = "running"
        task_id = task.id
        db.add(models.User(
            username="reader", password_hash=security.hash_password("reader-secret"),
            role=models.UserRole.user,
        ))
        db.commit()
    stop = f"/web/admin/maintenance/jobs/{task_id}/stop"
    client.post("/web/logout", data={"csrf_token": client.cookies["netvault_csrf"]})
    web_login(client, "reader", "reader-secret")
    assert client.post("/web/admin/maintenance/full", data=form).status_code == 403
    assert client.post(stop, data={
        "csrf_token": client.cookies["netvault_csrf"],
        "confirmation": "STOP FULL RUN",
    }).status_code == 403
    assert client.get(f"/web/admin/maintenance/jobs/{task_id}/log").status_code == 403
    client.post("/web/logout", data={"csrf_token": client.cookies["netvault_csrf"]})
    web_login(client)
    assert client.post(stop, data={
        "csrf_token": client.cookies["netvault_csrf"],
        "confirmation": "STOP FULL RUN",
    }, follow_redirects=False).status_code == 303
    with database.SessionLocal() as db:
        task = db.get(models.MaintenanceJob, task_id)
        assert task.status == "running" and task.cancel_requested
    assert client.post("/web/admin/maintenance/full", data={
        **form, "csrf_token": client.cookies["netvault_csrf"],
    }).status_code == 409
    page = client.get("/web/admin/jobs")
    assert "Stopping" in page.text


def test_full_library_job_progress_is_not_previous_cumulative_state(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    worker = importlib.import_module("run_admin_maintenance_worker")
    maintenance = importlib.import_module("netvault_server.server.maintenance")
    monkeypatch.setattr(worker, "TMP", tmp_path)
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    monkeypatch.setattr(worker, "FULL_STATE", tmp_path / "pdfcraft-unprocessed-state.json")
    monkeypatch.setattr(
        worker.shutil, "disk_usage",
        lambda path: SimpleNamespace(free=10 * 1024**3),
    )
    old = {"attempted": 122786, "replaced": 115822, "unchanged": 5750,
           "failed": 1214, "doi_written": 51978,
           "old_bytes": 118210060049, "new_bytes": 69880009678}
    state = {"full_cursor": 0, "last_full_scan_at": "yesterday", "full": old}
    worker.FULL_STATE.write_text(json.dumps(state))
    job = SimpleNamespace(
        id="f" * 32, kind="full_compression",
        options_json=json.dumps({"jobs": 3, "no_backup": True}),
        result_json=None, cancel_requested=False,
    )

    class FakeDB:
        committed = 0

        def scalar(self, query):
            return 2

        def commit(self):
            self.committed += 1

    db = FakeDB()
    assert worker.command_for(db, job, None)[1:] == [
        "scripts/compress_pdfs_parallel.py", "--all", "--jobs", "3",
    ]
    worker.prepare_full_run(db, job)
    first = worker.full_snapshot(job)
    assert first["attempted"] == 0 and first["doi_written"] == 0
    assert first["total"] == 2
    state["full"] = {**old, "attempted": old["attempted"] + 2,
                     "replaced": old["replaced"] + 1,
                     "unchanged": old["unchanged"] + 1,
                     "doi_written": old["doi_written"] + 1,
                     "old_bytes": old["old_bytes"] + 1200,
                     "new_bytes": old["new_bytes"] + 700}
    state["last_full_scan_at"] = "today"
    worker.FULL_STATE.write_text(json.dumps(state))
    progress = worker.full_snapshot(job)
    assert progress["attempted"] == 2 and progress["doi_written"] == 1
    assert progress["saved_bytes"] == 500
    assert progress["completed_sweep"]
    worker.refresh_progress(db, job)
    assert db.committed == 1
    assert json.loads(job.result_json)["attempted"] == 2
    assert "122786" not in job.result_json
    job.options_json = json.dumps({"jobs": 8, "no_backup": True})
    with pytest.raises(ValueError, match="count"):
        worker.command_for(db, job, None)
    job.options_json = json.dumps({"jobs": 3})
    with pytest.raises(ValueError, match="authorization"):
        worker.command_for(db, job, None)
    assert maintenance.FULL_CONFIRMATION == "RUN FULL LIBRARY"


def test_full_library_log_is_admin_only_sanitized(client, monkeypatch):
    web_login(client)
    maintenance = importlib.import_module("netvault_server.server.maintenance")
    monkeypatch.setattr(maintenance, "worker_is_online", lambda: True)
    assert client.post("/web/admin/maintenance/full", data={
        "jobs": 1, "confirmation": "RUN FULL LIBRARY",
        "acknowledge_no_backup": "true",
        "csrf_token": client.cookies["netvault_csrf"],
    }, follow_redirects=False).status_code == 303
    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    with database.SessionLocal() as db:
        job = db.scalar(select(models.MaintenanceJob).where(
            models.MaintenanceJob.kind == "full_compression"
        ))
        job_id = job.id
    log = maintenance.report_path(job_id).with_suffix(".log")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "2026-10-16T10:00:00+00:00 START mode=full jobs=3\n"
        "password=top-secret\n"
        "2026-10-16T10:05:00+00:00 SKIP id=11 password=top-secret\n"
        "2026-10-16T10:05:05+00:00 OK id=12 status=replaced bytes=900->800 doi_written=True\n"
    )
    response = client.get(f"/web/admin/maintenance/jobs/{job_id}/log")
    assert response.status_code == 200
    lines = response.json()["lines"]
    assert len(lines) == 3
    assert "top-secret" not in str(lines)
    assert "doi_written=True" in str(lines)
    assert response.headers["cache-control"] == "no-store"


def test_full_library_worker_gracefully_stops_only_coordinator(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    worker = importlib.import_module("run_admin_maintenance_worker")
    signals = []
    heartbeats = []

    class FakeProcess:
        pid = 817

        def __init__(self):
            self.waits = 0

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("pdfcraft", timeout)
            return 0

        def send_signal(self, signum):
            signals.append(signum)

    class FakeDB:
        def refresh(self, job, attribute_names):
            assert attribute_names == ["cancel_requested"]
            job.cancel_requested = True

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(worker, "heartbeat", lambda: heartbeats.append(True))
    monkeypatch.setattr(worker, "refresh_progress", lambda *args: None)
    monkeypatch.setattr(
        worker.os, "killpg",
        lambda pid, signum: pytest.fail("Full-library cancel must not kill a live PDFCraft child"),
    )
    result = worker.run_host_command(
        ["python", "scripts/compress_pdfs_parallel.py", "--all"], None,
        kind="full_compression", db=FakeDB(),
        job=SimpleNamespace(cancel_requested=False), worker_lock_fd=None,
    )
    assert result == 0
    assert signals == [worker.signal.SIGTERM]
    assert len(heartbeats) >= 2


def test_existing_maintenance_jobs_table_gets_additive_cancel_flag():
    from sqlalchemy import create_engine, inspect, text

    migrations = importlib.import_module("netvault_server.server.migrations")
    db = create_engine("sqlite://")
    with db.begin() as conn:
        conn.execute(text(
            "CREATE TABLE maintenance_jobs ("
            "id VARCHAR(32) PRIMARY KEY, kind VARCHAR(32), status VARCHAR(20), "
            "singleton_key VARCHAR(24), created_by_id INTEGER, source_job_id VARCHAR(32))"
        ))
    migrations._add_maintenance_jobs(db)
    migrations._add_maintenance_jobs(db)
    fields = {row["name"] for row in inspect(db).get_columns("maintenance_jobs")}
    assert "cancel_requested" in fields
    with db.begin() as conn:
        flag = conn.scalar(text(
            "SELECT cancel_requested FROM maintenance_jobs LIMIT 1"
        ))
    assert flag is None


def test_admin_jobs_history_paginate_without_leaking_user_forms(client):
    """Task list is a real pageable route; active progress and admin auth remain intact."""
    web_login(client)
    database = importlib.import_module("netvault_server.server.database")
    models = importlib.import_module("netvault_server.server.models")
    with database.SessionLocal() as db:
        admin = db.scalar(select(models.User).where(models.User.username == "admin"))
        for index in range(47):
            db.add(models.MaintenanceJob(
                id=f"{index + 1:032x}", kind="audit", status="completed",
                created_by_id=admin.id, singleton_key="maintenance",
                options_json='{"limit": 10}', result_json='{"needs_compression": 2}',
            ))
        db.commit()
    first = client.get("/web/admin/jobs")
    assert first.status_code == 200
    assert 'Page 1 of 3' in first.text
    assert first.text.count('class="maintenance-nowrap"') == 20
    assert 'id="job-0000000000000000000000000000002f"' in first.text
    assert 'href="/web/admin/jobs?page=2"' in first.text
    assert "Create User" not in first.text
    second = client.get("/web/admin/jobs?page=2")
    assert second.status_code == 200
    assert second.text.count('class="maintenance-nowrap"') == 20
    assert 'Page 2 of 3' in second.text
    assert 'href="/web/admin/jobs?page=1"' in second.text
    third = client.get("/web/admin/jobs?page=3")
    assert third.text.count('class="maintenance-nowrap"') == 7
    assert 'Page 3 of 3' in third.text
    excessive = client.get("/web/admin/jobs?page=9999")
    assert excessive.status_code == 200
    assert 'Page 3 of 3' in excessive.text
    assert client.get("/web/admin/jobs?page=-1").status_code == 422


def test_admin_section_navigation_and_user_errors_stay_on_people_page(client):
    web_login(client)
    routes = {
        "/web/admin": "Overview",
        "/web/admin/maintenance": "Maintenance",
        "/web/admin/jobs": "Task history",
        "/web/admin/users": "People",
    }
    for url, label in routes.items():
        response = client.get(url)
        assert response.status_code == 200
        assert 'class="admin-console-shell"' in response.text
        assert 'aria-label="Admin sections"' in response.text
        assert response.text.count('data-no-pjax') >= 4
        assert response.text.count('aria-current="page"') >= 1
        assert label in response.text
    people = client.get("/web/admin/users")
    assert "Create User" in people.text
    assert "Reset Password" in people.text
    bad = client.post("/web/admin/users/create", data={
        "username": "", "password": "temporary-pass",
        "role": "user", "csrf_token": client.cookies["netvault_csrf"],
    })
    assert bad.status_code == 200
    assert 'class="admin-console-shell"' in bad.text
    assert 'Team accounts' in bad.text
    assert 'Create User' in bad.text
    assert 'Run Read-Only Audit' not in bad.text


def test_task_submit_redirects_to_new_history_page(client):
    web_login(client)
    response = client.post("/web/admin/maintenance/start", data={
        "kind": "audit", "limit": 10,
        "csrf_token": client.cookies["netvault_csrf"],
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/web/admin/jobs?notice=queued"
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "Maintenance task queued." in page.text
    assert "Task History" in page.text

