from typer.testing import CliRunner

from netvault_server.cli import admin


runner = CliRunner()


def test_create_user_command(monkeypatch) -> None:
    calls = []

    def fake_post(path, payload=None):
        calls.append((path, payload))
        return {"username": "alice", "role": "user"}

    monkeypatch.setattr(admin, "api_post", fake_post)
    result = runner.invoke(admin.app, ["create-user", "alice"], input="alice-pass\nalice-pass\n")

    assert result.exit_code == 0
    assert calls == [
        ("/admin/users", {"username": "alice", "password": "alice-pass", "role": "user"})
    ]
    assert "Created user user alice" in result.output


def test_reset_deactivate_and_delete_commands(monkeypatch) -> None:
    posts = []
    deletes = []
    monkeypatch.setattr(admin, "api_post", lambda path, payload=None: posts.append((path, payload)) or {})
    monkeypatch.setattr(
        admin,
        "api_delete",
        lambda path: deletes.append(path)
        or {"id": 7, "original_name": "paper.pdf"},
    )

    reset = runner.invoke(
        admin.app,
        ["reset-password", "a/b"],
        input="new-password\nnew-password\n",
    )
    deactivate = runner.invoke(admin.app, ["deactivate-user", "a/b"])
    deleted = runner.invoke(admin.app, ["delete-pdf", "7"])

    assert reset.exit_code == 0
    assert deactivate.exit_code == 0
    assert deleted.exit_code == 0
    assert posts == [
        ("/admin/users/a%2Fb/reset-password", {"password": "new-password"}),
        ("/admin/users/a%2Fb/deactivate", None),
    ]
    assert deletes == ["/admin/pdfs/7"]
    assert "Deleted PDF #7" in deleted.output


def test_correct_doi_command_supports_preview_and_expected_hash(monkeypatch) -> None:
    calls = []

    def fake_post(path, payload=None):
        calls.append((path, payload))
        return {
            "pdf_id": 56240,
            "previous_doi": "10.1108/mbr-10-2023-0163/11288746/file.pdf",
            "new_doi": "10.1108/mbr-10-2023-0163",
            "title": "Host country distance",
            "dry_run": True,
        }

    monkeypatch.setattr(admin, "api_post", fake_post)
    result = runner.invoke(
        admin.app,
        [
            "correct-doi",
            "56240",
            "10.1108/MBR-10-2023-0163",
            "--reason",
            "Publisher URL was parsed as DOI",
            "--expected-sha256",
            "a" * 64,
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "/admin/pdfs/56240/correct-doi",
            {
                "doi": "10.1108/MBR-10-2023-0163",
                "reason": "Publisher URL was parsed as DOI",
                "dry_run": True,
                "expected_sha256": "a" * 64,
            },
        )
    ]
    assert "Would correct PDF #56240" in result.output
    assert "Crossref: Host country distance" in result.output
