import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import typer
import requests

DEFAULT_UPDATE_URL = "https://github.com/iihciyekub/netvault.git"
GITHUB_REPOSITORY_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)(?P<slug>[^/\s]+/[^/\s]+?)(?:\.git)?/?$"
)
RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def github_repository_slug(repo_url: str) -> str | None:
    normalized = repo_url.removeprefix("git+")
    match = GITHUB_REPOSITORY_RE.fullmatch(normalized)
    return match.group("slug") if match else None


def latest_release_tag(repo_url: str) -> str | None:
    slug = github_repository_slug(repo_url)
    if slug is None:
        return None
    response = requests.get(
        f"https://api.github.com/repos/{slug}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "NetVault CLI updater",
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"GitHub returned an invalid latest release response for {slug}")
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not RELEASE_TAG_RE.fullmatch(tag):
        raise RuntimeError(f"GitHub returned an invalid latest release tag for {slug}")
    return tag


def release_package_url(repo_url: str, tag: str | None) -> str:
    normalized = repo_url.removeprefix("git+")
    suffix = f"@{tag}" if tag else ""
    return f"git+{normalized}{suffix}"


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
    release_tag = latest_release_tag(update_url)
    package_url = release_package_url(update_url, release_tag)
    command = build_update_command(package_url)
    source = f"{update_url} release {release_tag}" if release_tag else update_url
    typer.echo(f"Updating NetVault from {source} ...")
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
