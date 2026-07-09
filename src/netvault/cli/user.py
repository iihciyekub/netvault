from pathlib import Path

import requests
import typer
from rich.console import Console
from rich.table import Table

from netvault.cli.config import clear_credentials, save_credentials
from netvault.cli.http import api_get, auth_headers, raise_for_api_error, server_url, upload_pdf
from netvault.cli.update import update_from_github

app = typer.Typer(
    help="NetVault team PDF vault CLI.",
    context_settings={"token_normalize_func": str.lower},
)
console = Console()


def render_pdf_table(rows: list[dict]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("DOI")
    table.add_column("Year", justify="right")
    table.add_column("Title")
    table.add_column("Venue")
    table.add_column("Meta")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["doi"],
            str(row["published_year"] or "-"),
            row["title"] or row["original_name"],
            row["container_title"] or "-",
            row["crossref_status"],
        )
    console.print(table)


def iter_pdf_paths(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(candidate for candidate in path.rglob("*.pdf") if candidate.is_file())
    raise typer.BadParameter(f"{path} does not exist")


@app.command()
def login(
    server: str = typer.Argument(..., help="NetVault server URL, for example http://127.0.0.1:8000"),
    username: str = typer.Option(..., prompt=True),
    password: str = typer.Option(..., prompt=True, hide_input=True),
) -> None:
    response = requests.post(
        f"{server.rstrip('/')}/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    raise_for_api_error(response)
    save_credentials(server, response.json()["access_token"])
    console.print(f"Logged in to {server.rstrip('/')} as {username}.")


@app.command()
def logout() -> None:
    try:
        response = requests.post(f"{server_url()}/auth/logout", headers=auth_headers(), timeout=30)
        raise_for_api_error(response)
    finally:
        clear_credentials()
    console.print("Logged out.")


@app.command("upload")
def upload_command(
    path: Path = typer.Argument(..., exists=True, readable=True),
    doi: str | None = typer.Option(None, "--doi", help="Use this DOI instead of extracting it."),
    no_crossref: bool = typer.Option(False, "--no-crossref", help="Skip Crossref metadata lookup."),
) -> None:
    pdfs = iter_pdf_paths(path)
    if not pdfs:
        console.print("No PDF files found.")
        return

    uploaded = 0
    if doi and len(pdfs) != 1:
        raise typer.BadParameter("--doi can only be used when uploading one PDF file")
    for pdf_path in pdfs:
        try:
            result = upload_pdf(pdf_path, doi=doi, no_crossref=no_crossref)
            marker = "deduped" if result["deduplicated"] else "uploaded"
            title = result["pdf"].get("title") or result["pdf"]["original_name"]
            console.print(f"{marker}: {pdf_path} -> {result['pdf']['doi']}  {title}")
            uploaded += 1
        except RuntimeError as exc:
            console.print(f"failed: {pdf_path}: {exc}")
    console.print(f"Processed {uploaded}/{len(pdfs)} PDF file(s).")


@app.command("list")
def list_command() -> None:
    render_pdf_table(api_get("/pdfs"))


@app.command()
def search(query: str = typer.Argument(...)) -> None:
    render_pdf_table(api_get("/pdfs/search", q=query))


@app.command()
def download(
    doi: str = typer.Argument(..., help="DOI, for example 10.1145/3368089.3409742"),
    to: Path = typer.Option(Path("."), "--to", help="Destination directory"),
) -> None:
    detail = api_get("/pdfs/by-doi", doi=doi)
    to.mkdir(parents=True, exist_ok=True)
    destination = to / detail["original_name"]
    response = requests.get(
        f"{server_url()}/pdfs/by-doi/download",
        headers=auth_headers(),
        params={"doi": doi},
        timeout=300,
        stream=True,
    )
    raise_for_api_error(response)
    with destination.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)
    console.print(f"Downloaded {detail['doi']} to {destination}.")


@app.command()
def status() -> None:
    me = api_get("/me")
    pdfs = api_get("/pdfs")
    total_size = sum(row["size"] for row in pdfs)
    console.print(f"Server: {server_url()}")
    console.print(f"User: {me['username']} ({me['role']})")
    console.print(f"PDFs: {len(pdfs)}")
    console.print(f"Total bytes: {total_size:,}")


@app.command()
def update(
    repo_url: str | None = typer.Option(
        None,
        "--repo-url",
        help="GitHub repository URL. Defaults to https://github.com/iihciyekub/netvault.git.",
    ),
) -> None:
    update_from_github(repo_url)


if __name__ == "__main__":
    app()
