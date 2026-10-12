import csv
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

from netvault.cli import export as export_cli, user


def response(monkeypatch, body, *, count=1, failure=None):
    class Response:
        ok = True
        headers = {"Content-Type": "text/csv; charset=utf-8", "X-NetVault-Rows": str(count)}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def iter_content(self, chunk_size):
            assert chunk_size == 1024 * 1024
            yield body[:10]
            if failure:
                raise failure
            yield body[10:]

    class Session:
        def get(self, url, **kwargs):
            assert url == "https://example.test/nv/pdfs/export"
            assert kwargs["params"] == {"issn": "0025-1909"}
            assert kwargs["headers"] == {"Authorization": "Bearer test"}
            assert kwargs["stream"] is True
            return Response()

    monkeypatch.setattr(export_cli, "http_session", Session)
    monkeypatch.setattr(export_cli, "server_url", lambda: "https://example.test/nv")
    monkeypatch.setattr(export_cli, "auth_headers", lambda: {"Authorization": "Bearer test"})


def test_export_command_saves_normalized_issn_and_complete_csv(tmp_path, monkeypatch):
    payload = ("doi,sha256\r\n10.1234/a," + "a" * 64 + "\r\n").encode()
    response(monkeypatch, payload)
    output = tmp_path / "subfolder" / "papers.csv"
    result = CliRunner().invoke(user.app, ["export", "--issn", "00251909", "--out", str(output)])
    assert result.exit_code == 0, result.output
    assert "Exported 1 DOI/SHA-256 rows" in result.output
    assert output.read_bytes() == payload
    with output.open(newline="") as handle:
        assert list(csv.DictReader(handle)) == [{"doi": "10.1234/a", "sha256": "a" * 64}]
    assert list(output.parent.glob(".nv-export-*")) == []


@pytest.mark.parametrize("failure,body,count", [
    (requests.ConnectionError("interrupted"), b"doi,sha256\r\n", 1),
    (None, b"doi,sha256\r\n10.1234/a,partial\r\n", 1),
    (None, b"doi,sha256\r\n", 1),
    (None, b"title,sha256\r\n", 0),
])
def test_failed_export_preserves_existing_file_and_cleans_temporary(tmp_path, monkeypatch, failure, body, count):
    output = tmp_path / "papers.csv"
    output.write_text("previous export")
    response(monkeypatch, body, count=count, failure=failure)
    with pytest.raises((RuntimeError, requests.ConnectionError)):
        export_cli.save_issn_export("0025-1909", output, force=True)
    assert output.read_text() == "previous export"
    assert list(tmp_path.glob(".nv-export-*")) == []


def test_export_refuses_overwrite_and_supports_empty_inventory(tmp_path, monkeypatch):
    output = tmp_path / "papers.csv"
    output.write_text("existing")
    with pytest.raises(FileExistsError):
        export_cli.save_issn_export("0025-1909", output)
    response(monkeypatch, b"doi,sha256\r\n", count=0)
    assert export_cli.save_issn_export("0025-1909", output, force=True) == 0
    assert output.read_text() == "doi,sha256\n"


def test_export_checks_issn_before_contacting_server(monkeypatch):
    monkeypatch.setattr(user, "save_issn_export", lambda *_args, **_kwargs: pytest.fail("Network called"))
    for value in ("0025-1900", "invalid", "００２５１９０９"):
        result = CliRunner().invoke(user.app, ["export", "--issn", value])
        assert result.exit_code == 2
        assert "Invalid ISSN" in result.output
    assert export_cli.normalize_export_issn("1526 548x") == "1526-548X"


def test_export_default_filename_uses_normalized_identifier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    observed = []
    monkeypatch.setattr(user, "save_issn_export", lambda issn, path, **kwargs: observed.append((issn, path)) or 0)
    result = CliRunner().invoke(user.app, ["export", "--issn", "1526548x"])
    assert result.exit_code == 0
    assert observed == [("1526-548X", Path("netvault-1526-548X.csv").resolve())]


@pytest.mark.parametrize("force", [False, True])
def test_export_without_output_preserves_previous_csv_or_explicitly_replaces_it(
    tmp_path, monkeypatch, force,
):
    monkeypatch.chdir(tmp_path)
    original = tmp_path / "netvault-0025-1909.csv"
    original.write_text("previous export")
    numbered = tmp_path / "netvault-0025-1909-2.csv"
    numbered.write_text("second export")
    payload = ("doi,sha256\r\n10.1234/a," + "a" * 64 + "\r\n").encode()
    response(monkeypatch, payload)
    args = ["export", "--issn", "00251909"]
    if force:
        args.append("--force")
    result = CliRunner().invoke(user.app, args)
    assert result.exit_code == 0, result.output
    destination = original if force else tmp_path / "netvault-0025-1909-3.csv"
    assert str(destination) in result.output.replace("\n", "")
    assert destination.read_bytes() == payload
    assert numbered.read_text() == "second export"
    if not force:
        assert original.read_text() == "previous export"
    assert list(tmp_path.glob(".nv-export-*")) == []
