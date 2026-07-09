import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer

DEFAULT_UPDATE_URL = "https://github.com/iihciyekub/netvault.git"


def is_uv_tool_python(executable: str | Path) -> bool:
    parts = Path(executable).parts
    return "uv" in parts and "tools" in parts and "netvault" in parts


def is_pipx_python(executable: str | Path) -> bool:
    parts = Path(executable).parts
    return "pipx" in parts and "venvs" in parts and "netvault" in parts


def build_update_command(package_url: str, executable: str | Path | None = None) -> list[str]:
    current_python = Path(executable or sys.executable)
    uv = shutil.which("uv")
    pipx = shutil.which("pipx")

    if is_uv_tool_python(current_python) and uv:
        return [uv, "tool", "install", "--force", package_url]
    if is_pipx_python(current_python) and pipx:
        return [pipx, "install", "--force", package_url]
    if uv:
        return [uv, "tool", "install", "--force", package_url]
    if pipx:
        return [pipx, "install", "--force", package_url]
    return [str(current_python), "-m", "pip", "install", "--upgrade", "--force-reinstall", package_url]


def update_from_github(repo_url: str | None = None) -> None:
    update_url = repo_url or os.environ.get("NETVAULT_UPDATE_URL") or DEFAULT_UPDATE_URL
    package_url = f"git+{update_url}" if not update_url.startswith("git+") else update_url
    command = build_update_command(package_url)
    typer.echo(f"Updating NetVault from {update_url} ...")
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        typer.echo("NetVault update failed. Try reinstalling with the install script:", err=True)
        typer.echo(
            "curl -fsSL https://raw.githubusercontent.com/iihciyekub/netvault/main/scripts/install.sh | bash",
            err=True,
        )
        raise typer.Exit(exc.returncode) from exc
    typer.echo("NetVault updated.")
