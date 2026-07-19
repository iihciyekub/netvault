import os
from pathlib import Path
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_unix_installer_uses_release_wheel_and_absolute_nv_path(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    tool_bin = tmp_path / "tool-bin"
    fake_bin.mkdir()
    tool_bin.mkdir()
    log_path = tmp_path / "uv.log"

    uv_script = fake_bin / "uv"
    uv_script.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${FAKE_UV_LOG}"
if [[ "$*" == "tool dir --bin" ]]; then
  printf '%s\\n' "${FAKE_TOOL_BIN}"
fi
""",
        encoding="utf-8",
    )
    uv_script.chmod(0o755)

    nv_script = tool_bin / "nv"
    nv_script.write_text(
        "#!/usr/bin/env bash\nprintf 'NetVault 0.7.15\\n'\n",
        encoding="utf-8",
    )
    nv_script.chmod(0o755)
    netvault_script = tool_bin / "netvault"
    netvault_script.write_text(nv_script.read_text(encoding="utf-8"), encoding="utf-8")
    netvault_script.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_TOOL_BIN": str(tool_bin),
            "FAKE_UV_LOG": str(log_path),
            "NETVAULT_RELEASE_TAG": "v0.7.15",
        }
    )
    result = subprocess.run(
        ["bash", "scripts/install.sh"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "NetVault 0.7.15" in result.stdout
    log = log_path.read_text(encoding="utf-8")
    assert (
        "tool install --force "
        "https://github.com/iihciyekub/netvault/releases/download/"
        "v0.7.15/netvault-0.7.15-py3-none-any.whl"
    ) in log
    assert "tool update-shell" in log
    assert "tool dir --bin" in log


def test_unix_installer_tolerates_path_update_failure_and_normalizes_git_url(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    tool_bin = tmp_path / "tool-bin"
    fake_bin.mkdir()
    tool_bin.mkdir()
    log_path = tmp_path / "uv.log"

    uv_script = fake_bin / "uv"
    uv_script.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${FAKE_UV_LOG}"
if [[ "$*" == "tool update-shell" ]]; then exit 7; fi
if [[ "$*" == "tool dir --bin" ]]; then printf '%s\\n' "${FAKE_TOOL_BIN}"; fi
""",
        encoding="utf-8",
    )
    uv_script.chmod(0o755)
    for command in ("nv", "netvault"):
        executable = tool_bin / command
        executable.write_text(
            "#!/usr/bin/env bash\nprintf 'NetVault 0.7.15\\n'\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_TOOL_BIN": str(tool_bin),
            "FAKE_UV_LOG": str(log_path),
            "NETVAULT_UPDATE_URL": "git+https://example.com/team/netvault.git",
        }
    )
    result = subprocess.run(
        ["bash", "scripts/install.sh"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "PATH could not be updated automatically" in result.stderr
    log = log_path.read_text(encoding="utf-8")
    assert "tool install --force git+https://example.com/team/netvault.git" in log
    assert "git+git+" not in log


def test_windows_installer_and_docs_include_complete_platform_commands() -> None:
    installer = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    user_guide = (ROOT / "docs" / "user-guide.md").read_text(encoding="utf-8")

    assert "https://astral.sh/uv/install.ps1" in installer
    assert "Invoke-Expression" not in installer
    assert "Invoke-WithRetry" in installer
    assert "netvault-$ReleaseVersion-py3-none-any.whl" in installer
    assert "& $UvPath tool update-shell" in installer
    assert 'Join-Path $ToolBinDir "nv.exe"' in installer

    for document in (readme, user_guide):
        assert "scripts/install.sh" in document
        assert "scripts/install.ps1" in document
        assert "powershell -NoProfile -ExecutionPolicy Bypass" in document
        assert "-OutFile" in document


def test_cli_package_contract_excludes_server_and_web_components() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/netvault"]
    dependency_names = {
        dependency.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].lower()
        for dependency in project["project"]["dependencies"]
    }
    assert dependency_names.isdisjoint(
        {
            "fastapi",
            "uvicorn",
            "sqlalchemy",
            "jinja2",
            "python-multipart",
            "netvault-server",
        }
    )
    assert not list((ROOT / "src" / "netvault" / "server").glob("*.py"))
    assert not (ROOT / "src" / "netvault" / "templates").exists()
    assert not (ROOT / "src" / "netvault" / "static").exists()
