import requests
import typer
from rich.console import Console

from netvault.cli.http import api_delete, api_post, auth_headers, raise_for_api_error, server_url
from netvault.server.models import UserRole

app = typer.Typer(
    help="NetVault administrator CLI.",
    context_settings={"token_normalize_func": str.lower},
)
console = Console()


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
    api_post(f"/admin/users/{username}/reset-password", {"password": password})
    console.print(f"Reset password for {username}.")


@app.command("deactivate-user")
def deactivate_user(username: str) -> None:
    api_post(f"/admin/users/{username}/deactivate")
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


if __name__ == "__main__":
    app()
