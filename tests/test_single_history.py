"""End-to-end guarantees of the single NetVault JSONL journal."""
import json
import shutil

from typer.testing import CliRunner

from netvault.cli import user
from netvault.cli.history import History
from netvault.cli.upload_state import STATE_NAME
from test_tidy import make_pdf

pytest_plugins = ("test_upload_memory",)


def rows(path):
    journal = path / ".netvault" / "history.jsonl"
    assert journal.exists(), f"missing {journal}"
    return [json.loads(row) for row in journal.read_text().splitlines()]


def test_upload_failures_then_retry_use_one_history_without_sqlite(setup_upload):
    pdf, state = setup_upload
    first = CliRunner().invoke(user.app, ["upload", str(pdf)])
    assert first.exit_code == 1, first.output
    assert not (pdf.parent / "nv-upload-report.json").exists()
    assert not (pdf.parent / STATE_NAME).exists()
    assert any(row["cmd"] == "upload" and row["result"] == "review"
               for row in rows(pdf.parent))
    state["error"] = None
    retried = CliRunner().invoke(user.app, ["upload", str(pdf), "--retry-failed"])
    assert retried.exit_code == 0, retried.output
    assert len(state["uploads"]) == 2
    journal = rows(pdf.parent)
    assert not any(row.get("file") == pdf.name and row["action"] in
                   {"pending", "error", "upload", "clear"} for row in journal)
    assert journal[-1]["action"] == "summary"
    assert journal[-1]["counts"]["uploaded"] == 1
    assert not (pdf.parent / STATE_NAME).exists()


def test_tidy_only_one_history_not_csv(tmp_path):
    root = tmp_path
    a = make_pdf(root / "a" / "paper.pdf", doi="10.1234/item")
    shutil.copyfile(a, root / "a" / "copy.pdf")
    make_pdf(root / "a" / "missing.pdf")
    dry = CliRunner().invoke(user.app, ["tidy", str(root)])
    assert dry.exit_code == 0, dry.output
    assert not (root / ".netvault").exists()
    applied = CliRunner().invoke(user.app, ["tidy", str(root), "--apply"])
    assert applied.exit_code == 0, applied.output
    journal = rows(root)
    assert sum(r["action"] == "delete" for r in journal) == 1
    assert sum(r["action"] == "move" for r in journal) == 1
    assert journal[-1]["action"] == "summary"
    assert not list(root.glob("nv-tidy-report*.csv"))
    assert not (root / STATE_NAME).exists()


def test_identify_hash_matching_command_is_removed():
    result = CliRunner().invoke(user.app, ["identify", "--help"])
    assert result.exit_code == 2, result.output
    assert "No such command" in result.output
    top = CliRunner().invoke(user.app, ["--help"])
    assert top.exit_code == 0, top.output
    assert "identify" not in top.output
    assert "tidy" in top.output


def test_download_summary_has_no_download_report(tmp_path, monkeypatch):
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "server_url", lambda: "https://example.test")
    monkeypatch.setattr(user, "auth_headers", lambda: {})
    monkeypatch.setattr(
        user, "api_post", lambda *_a, **_k: {"existing_doi": {}},
    )
    result = CliRunner().invoke(
        user.app, ["download", "10.1234/not-present", "--to", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "nv-download-report.json").exists()
    assert rows(tmp_path)[-1]["counts"]["unavailable"] == 1


def test_history_ignores_truncated_lines(tmp_path):
    log = History(tmp_path, "tidy")
    log.path.parent.mkdir()
    log.path.write_bytes(b'{"incomplete":')
    log.write("move", file=tmp_path / "article.pdf", result="ok")
    data = list(log.records())
    assert len(data) == 1
    assert data[0]["action"] == "move"
