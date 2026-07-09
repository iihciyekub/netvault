import hashlib
from pathlib import Path

import requests
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from netvault import __version__
from netvault.cli.config import clear_credentials, load_credentials, save_credentials
from netvault.cli.http import api_get, auth_headers, raise_for_api_error, server_url, upload_pdf
from netvault.cli.update import update_from_github
from netvault.server.doi import extract_doi_evidence

DEFAULT_SERVER_URL = "https://iiaide.com/nv"

app = typer.Typer(
    help="NetVault team PDF vault CLI.",
    context_settings={"token_normalize_func": str.lower},
)
console = Console()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"NetVault {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show the NetVault version and exit.",
    ),
) -> None:
    _ = version


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


def file_sha256(path: Path, progress_callback=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            if progress_callback:
                progress_callback(len(chunk))
    return digest.hexdigest()


def existing_pdf_from_response(response: requests.Response) -> dict | None:
    if response.status_code == 404:
        return None
    raise_for_api_error(response)
    return response.json()


def get_existing_pdf_by_sha256(sha256: str) -> dict | None:
    response = requests.get(
        f"{server_url()}/pdfs/{sha256}",
        headers=auth_headers(),
        timeout=30,
    )
    return existing_pdf_from_response(response)


def get_existing_pdf_by_doi(doi: str) -> dict | None:
    response = requests.get(
        f"{server_url()}/pdfs/by-doi",
        headers=auth_headers(),
        params={"doi": doi},
        timeout=30,
    )
    return existing_pdf_from_response(response)


def extract_local_doi(path: Path, explicit_doi: str | None) -> str | None:
    evidence = extract_doi_evidence(path, explicit_doi=explicit_doi)
    if evidence.status == "ok":
        return evidence.doi
    if explicit_doi or evidence.status == "conflict":
        raise RuntimeError(evidence.reason or "DOI extraction failed")
    return None


def find_existing_pdf_before_upload(path: Path, explicit_doi: str | None, on_read=None) -> dict | None:
    sha256 = file_sha256(path, progress_callback=on_read)
    doi = extract_local_doi(path, explicit_doi)
    if doi:
        existing = get_existing_pdf_by_doi(doi)
        if existing:
            return existing
    return get_existing_pdf_by_sha256(sha256)


def save_login(server: str, username: str, password: str) -> None:
    response = requests.post(
        f"{server.rstrip('/')}/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    raise_for_api_error(response)
    save_credentials(server, response.json()["access_token"])


def credentials_valid() -> bool:
    credentials = load_credentials()
    saved_server = credentials.get("server_url")
    token = credentials.get("token")
    if not isinstance(saved_server, str) or not isinstance(token, str):
        return False
    response = requests.get(
        f"{saved_server}/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if response.status_code in {401, 403}:
        clear_credentials()
        return False
    raise_for_api_error(response)
    return True


def ensure_logged_in() -> None:
    if credentials_valid():
        return
    console.print("Login required.")
    server = typer.prompt("Server", default=DEFAULT_SERVER_URL)
    username = typer.prompt("Username")
    password = typer.prompt("Password", hide_input=True)
    save_login(server, username, password)
    console.print(f"Logged in to {server.rstrip('/')} as {username}.")


@app.command()
def login(
    server: str = typer.Argument(DEFAULT_SERVER_URL, help="NetVault server URL."),
    username: str = typer.Option(..., prompt=True),
    password: str = typer.Option(..., prompt=True, hide_input=True),
) -> None:
    save_login(server, username, password)
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
    ensure_logged_in()
    pdfs = iter_pdf_paths(path)
    if not pdfs:
        console.print("No PDF files found.")
        return

    if doi and len(pdfs) != 1:
        raise typer.BadParameter("--doi can only be used when uploading one PDF file")

    uploaded = 0
    deduped = 0
    skipped = 0
    failed: list[tuple[Path, str]] = []
    latest_pdf: dict | None = None
    total_bytes = sum(pdf_path.stat().st_size for pdf_path in pdfs) * 2
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Processing PDFs", total=total_bytes)
        for pdf_path in pdfs:
            progress.update(task, description=f"Checking {pdf_path.name}")
            pdf_size = pdf_path.stat().st_size
            processed_for_file = 0

            def advance_file(delta: int) -> None:
                nonlocal processed_for_file
                if delta > 0:
                    progress.advance(task, delta)
                    processed_for_file += delta

            def show_check_progress(bytes_read: int) -> None:
                advance_file(bytes_read)

            uploaded_for_file = 0

            def show_upload_progress(bytes_sent: int, _total_for_file: int) -> None:
                nonlocal uploaded_for_file
                visible_sent = min(bytes_sent, pdf_size)
                delta = visible_sent - uploaded_for_file
                advance_file(delta)
                if delta > 0:
                    uploaded_for_file = visible_sent

            try:
                existing_pdf = find_existing_pdf_before_upload(
                    pdf_path,
                    doi,
                    on_read=show_check_progress,
                )
                if existing_pdf:
                    latest_pdf = existing_pdf
                    skipped += 1
                    advance_file(pdf_size)
                    continue

                progress.update(task, description=f"Uploading {pdf_path.name}")
                result = upload_pdf(
                    pdf_path,
                    doi=doi,
                    no_crossref=no_crossref,
                    progress_callback=show_upload_progress,
                )
                if uploaded_for_file < pdf_size:
                    advance_file(pdf_size - uploaded_for_file)
                progress.update(task, description=f"Indexed {pdf_path.name}")
                latest_pdf = result["pdf"]
                if result["deduplicated"]:
                    deduped += 1
                else:
                    uploaded += 1
            except RuntimeError as exc:
                failed.append((pdf_path, str(exc)))
            finally:
                expected_for_file = pdf_size * 2
                if processed_for_file < expected_for_file:
                    advance_file(expected_for_file - processed_for_file)

    ok_count = uploaded + deduped + skipped
    parts = [f"done: {ok_count}/{len(pdfs)} processed"]
    if uploaded:
        parts.append(f"{uploaded} uploaded")
    if deduped:
        parts.append(f"{deduped} deduped")
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        parts.append(f"{len(failed)} failed")
    if latest_pdf:
        title = latest_pdf.get("title") or latest_pdf["original_name"]
        parts.append(f"latest: {latest_pdf['doi']}  {title}")
    console.print("; ".join(parts))
    for pdf_path, error in failed:
        console.print(f"failed: {pdf_path}: {error}")


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
