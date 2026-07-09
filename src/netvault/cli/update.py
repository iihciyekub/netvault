import os
import subprocess
import sys

import typer

DEFAULT_UPDATE_URL = "https://github.com/iihciyekub/netvault.git"


def update_from_github(repo_url: str | None = None) -> None:
    update_url = repo_url or os.environ.get("NETVAULT_UPDATE_URL") or DEFAULT_UPDATE_URL
    package_url = f"git+{update_url}" if not update_url.startswith("git+") else update_url
    command = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", package_url]
    typer.echo(f"Updating NetVault from {update_url} ...")
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise typer.Exit(exc.returncode) from exc
    typer.echo("NetVault updated.")
