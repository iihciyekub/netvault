from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import typer
from rich.console import Console

from netvault_server.server.models import UserRole

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

APP_DIR = Path.home() / ".config" / "netvault"
CREDENTIALS_PATH = APP_DIR / "credentials.toml"

app = typer.Typer(
    help="NetVault administrator CLI.",
    context_settings={"token_normalize_func": str.lower},
)
console = Console()


def load_credentials() -> dict[str, Any]:
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        with CREDENTIALS_PATH.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def require_credentials() -> tuple[str, str]:
    credentials = load_credentials()
    saved_server_url = credentials.get("server_url")
    token = credentials.get("token")
    if not isinstance(saved_server_url, str) or not isinstance(token, str):
        raise RuntimeError("Not logged in. Run `nv login <server-url>` first.")
    return saved_server_url, token


def auth_headers() -> dict[str, str]:
    _, token = require_credentials()
    return {"Authorization": f"Bearer {token}"}


def server_url() -> str:
    url, _ = require_credentials()
    return url


def raise_for_api_error(response: requests.Response) -> None:
    if response.ok:
        return
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    raise RuntimeError(f"{response.status_code}: {detail}")


def api_post(path: str, json: dict[str, Any] | None = None) -> Any:
    response = requests.post(f"{server_url()}{path}", headers=auth_headers(), json=json, timeout=60)
    raise_for_api_error(response)
    return response.json()


def api_delete(path: str) -> Any:
    response = requests.delete(f"{server_url()}{path}", headers=auth_headers(), timeout=60)
    raise_for_api_error(response)
    return response.json()


@app.command("create-user")
def create_user(
    username: str,
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True),
    admin: bool = typer.Option(False, "--admin", help="Create an administrator account."),
) -> None:
    role = UserRole.admin if admin else UserRole.user
    user = api_post("/admin/users", {"username": username, "password": password, "role": role})
    console.print(f"Created {user['role']} user {user['username']}.")


@app.command("reset-password")
def reset_password(
    username: str,
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True),
) -> None:
    api_post(f"/admin/users/{quote(username, safe='')}/reset-password", {"password": password})
    console.print(f"Reset password for {username}.")


@app.command("deactivate-user")
def deactivate_user(username: str) -> None:
    api_post(f"/admin/users/{quote(username, safe='')}/deactivate")
    console.print(f"Deactivated {username}.")


@app.command("delete-pdf")
def delete_pdf(identifier: str) -> None:
    if "/" in identifier or identifier.lower().startswith(("doi:", "http://", "https://")):
        response = requests.delete(
            f"{server_url()}/admin/pdfs/by-doi",
            headers=auth_headers(),
            params={"doi": identifier},
            timeout=60,
        )
        raise_for_api_error(response)
        pdf = response.json()
    else:
        pdf = api_delete(f"/admin/pdfs/{identifier}")
    console.print(f"Deleted PDF #{pdf['id']} ({pdf['original_name']}).")


@app.command("correct-doi")
def correct_doi(
    pdf_id: int,
    doi: str,
    reason: str = typer.Option(..., "--reason", help="Why this DOI identity is being corrected."),
    expected_sha256: str | None = typer.Option(
        None,
        "--expected-sha256",
        help="Abort if the canonical PDF changed before correction.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate without changing data."),
) -> None:
    payload: dict[str, Any] = {
        "doi": doi,
        "reason": reason,
        "dry_run": dry_run,
    }
    if expected_sha256:
        payload["expected_sha256"] = expected_sha256
    result = api_post(f"/admin/pdfs/{pdf_id}/correct-doi", payload)
    action = "Would correct" if result.get("dry_run") else "Corrected"
    console.print(
        f"{action} PDF #{result['pdf_id']}: {result['previous_doi']} -> {result['new_doi']}"
    )
    if result.get("title"):
        console.print(f"Crossref: {result['title']}")


def run() -> None:
    try:
        app()
    except (RuntimeError, OSError, requests.RequestException) as exc:
        console.print(f"error: {exc}", style="red", highlight=False)
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()
