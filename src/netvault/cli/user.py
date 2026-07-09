from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Iterable

import requests
import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
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
from netvault.doi import extract_doi_evidence, find_dois_in_text, normalize_doi

DEFAULT_SERVER_URL = "https://iiaide.com/nv"

app = typer.Typer(
    help="NetVault team PDF vault CLI.",
    epilog="""\b
Examples:
  nv login https://iiaide.com/nv
  nv upload ./paper.pdf ./papers
  nv download 10.1016/j.ijpe.2018.04.006 --to ./downloads
  nv download --file ./dois.txt --to ./downloads
  nv update
""",
    rich_markup_mode=None,
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
        if path.suffix.lower() != ".pdf":
            raise typer.BadParameter(f"{path} is not a PDF file")
        return [path]
    if path.is_dir():
        return sorted(
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file() and candidate.suffix.lower() == ".pdf"
        )
    raise typer.BadParameter(f"{path} does not exist")


def collect_pdf_paths(paths: Iterable[Path]) -> list[Path]:
    pdfs: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        for pdf_path in iter_pdf_paths(path):
            resolved = pdf_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            pdfs.append(pdf_path)
    return pdfs


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


def collect_dois(raw_dois: Iterable[str], doi_files: Iterable[Path]) -> list[str]:
    dois: list[str] = []

    def add(value: str) -> None:
        try:
            doi = normalize_doi(value)
        except ValueError:
            return
        if doi not in dois:
            dois.append(doi)

    for raw_doi in raw_dois:
        add(raw_doi)
    for doi_file in doi_files:
        for doi in find_dois_in_text(doi_file.read_text(encoding="utf-8", errors="ignore")):
            add(doi)
    return dois


def unique_destination(directory: Path, filename: str, used_destinations: set[Path]) -> Path:
    safe_name = Path(filename).name
    destination = directory / safe_name
    if destination not in used_destinations and not destination.exists():
        used_destinations.add(destination)
        return destination
    stem = destination.stem
    suffix = destination.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if candidate not in used_destinations and not candidate.exists():
            used_destinations.add(candidate)
            return candidate
        counter += 1


def format_bytes(size: int) -> str:
    value = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size:,} B"


def decode_token_payload(token: str) -> dict:
    try:
        _, payload, _ = token.split(".", 2)
        padded_payload = payload + "=" * (-len(payload) % 4)
        import base64

        return json.loads(base64.urlsafe_b64decode(padded_payload))
    except (ValueError, json.JSONDecodeError):
        return {}


def format_token_expiry(token: str) -> tuple[str, str]:
    payload = decode_token_payload(token)
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int):
        return "-", "-"
    expires = datetime.fromtimestamp(expires_at, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    remaining_seconds = int((expires - now).total_seconds())
    if remaining_seconds <= 0:
        return expires.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"), "expired"
    days, remainder = divmod(remaining_seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, _ = divmod(remainder, 60)
    if days:
        remaining = f"{days}d {hours}h"
    elif hours:
        remaining = f"{hours}h {minutes}m"
    else:
        remaining = f"{minutes}m"
    return expires.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"), remaining


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


@app.command(
    epilog="""\b
Examples:
  nv login https://iiaide.com/nv
  nv login
""",
)
def login(
    server: str = typer.Argument(DEFAULT_SERVER_URL, help="NetVault server URL."),
    username: str = typer.Option(..., prompt=True),
    password: str = typer.Option(..., prompt=True, hide_input=True),
) -> None:
    save_login(server, username, password)
    console.print(f"Logged in to {server.rstrip('/')} as {username}.")


@app.command(
    epilog="""\b
Examples:
  nv logout
""",
)
def logout() -> None:
    try:
        response = requests.post(f"{server_url()}/auth/logout", headers=auth_headers(), timeout=30)
        raise_for_api_error(response)
    finally:
        clear_credentials()
    console.print("Logged out.")


@app.command(
    "upload",
    epilog="""\b
Examples:
  nv upload ./paper.pdf
  nv upload ./paper-a.pdf ./paper-b.pdf
  nv upload ~/Downloads/papers
  nv upload ./paper.pdf --doi 10.1016/j.ijpe.2018.04.006
  nv upload ~/Downloads/papers --no-crossref

\b
Notes:
  Directories are scanned recursively for .pdf/.PDF files.
  Existing PDFs are skipped before upload by checking DOI and sha256.
""",
)
def upload_command(
    paths: list[Path] = typer.Argument(..., exists=True, readable=True),
    doi: str | None = typer.Option(None, "--doi", help="Use this DOI instead of extracting it."),
    no_crossref: bool = typer.Option(False, "--no-crossref", help="Skip Crossref metadata lookup."),
) -> None:
    ensure_logged_in()
    pdfs = collect_pdf_paths(paths)
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


@app.command(
    "list",
    epilog="""\b
Examples:
  nv list
""",
)
def list_command() -> None:
    render_pdf_table(api_get("/pdfs"))


@app.command(
    epilog="""\b
Examples:
  nv search supply
  nv search 10.1016
""",
)
def search(query: str = typer.Argument(...)) -> None:
    render_pdf_table(api_get("/pdfs/search", q=query))


@app.command(
    epilog="""\b
Examples:
  nv download 10.1016/j.ijpe.2018.04.006 --to ~/Downloads
  nv download 10.1016/j.ijpe.2018.04.006 10.1234/example.doi --to ./downloads
  nv download --file ./dois.txt --to ./downloads
  nv download 10.1016/j.ijpe.2018.04.006 --file ./more-dois.txt --to ./downloads

\b
Notes:
  --file reads any text file and extracts DOI values with NetVault's DOI regex.
  Duplicate DOI values are downloaded once.
""",
)
def download(
    dois: list[str] = typer.Argument(None, help="DOI values, for example 10.1145/3368089.3409742"),
    to: Path = typer.Option(Path("."), "--to", help="Destination directory"),
    doi_files: list[Path] = typer.Option(
        [],
        "--file",
        "-f",
        exists=True,
        readable=True,
        help="Read DOI values from a text file using NetVault's DOI regex.",
    ),
) -> None:
    ensure_logged_in()
    requested_dois = collect_dois(dois or [], doi_files)
    if not requested_dois:
        raise typer.BadParameter("Provide at least one DOI or --file PATH")

    details: list[dict] = []
    failed: list[tuple[str, str]] = []
    for doi in requested_dois:
        try:
            details.append(api_get("/pdfs/by-doi", doi=doi))
        except RuntimeError as exc:
            failed.append((doi, str(exc)))

    if not details:
        console.print(f"done: 0/{len(requested_dois)} downloaded; {len(failed)} failed")
        for doi, error in failed:
            console.print(f"failed: {doi}: {error}")
        return

    to.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    used_destinations: set[Path] = set()
    total_bytes = sum(int(detail["size"]) for detail in details)
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
        task = progress.add_task("Downloading PDFs", total=total_bytes)
        for detail in details:
            doi = detail["doi"]
            progress.update(task, description=f"Downloading {doi}")
            try:
                destination = unique_destination(to, detail["original_name"], used_destinations)
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
                            progress.advance(task, len(chunk))
                downloaded += 1
            except RuntimeError as exc:
                failed.append((doi, str(exc)))

    parts = [f"done: {downloaded}/{len(requested_dois)} downloaded"]
    if failed:
        parts.append(f"{len(failed)} failed")
    parts.append(f"to: {to}")
    console.print("; ".join(parts))
    for doi, error in failed:
        console.print(f"failed: {doi}: {error}")


@app.command(
    epilog="""\b
Examples:
  nv status
""",
)
def status() -> None:
    me = api_get("/me")
    pdfs = api_get("/pdfs")
    total_size = sum(row["size"] for row in pdfs)
    credentials = load_credentials()
    token = credentials.get("token", "")
    expires_at, remaining = format_token_expiry(token) if isinstance(token, str) else ("-", "-")

    console.print(Panel.fit("NetVault Status", style="bold"))

    account_table = Table(title="Connection", box=box.SIMPLE_HEAVY, show_header=True, header_style="bold")
    account_table.add_column("Field")
    account_table.add_column("Value")
    account_table.add_row("Server", server_url())
    account_table.add_row("User", str(me["username"]))
    account_table.add_row("Role", str(me["role"]))
    account_table.add_row("Account Active", "yes" if me.get("is_active") else "no")
    account_table.add_row("Token Expires", expires_at)
    account_table.add_row("Token Remaining", remaining)

    years = [row["published_year"] for row in pdfs if row.get("published_year")]
    uploaders = sorted({row["uploaded_by"] for row in pdfs if row.get("uploaded_by")})
    summary_table = Table(title="Library", box=box.SIMPLE_HEAVY, show_header=True, header_style="bold")
    summary_table.add_column("Metric")
    summary_table.add_column("Value", justify="right")
    summary_table.add_row("Active PDFs", f"{len(pdfs):,}")
    summary_table.add_row("Total Size", format_bytes(total_size))
    summary_table.add_row("Uploaders", f"{len(uploaders):,}")
    summary_table.add_row("Year Range", f"{min(years)}-{max(years)}" if years else "-")

    status_counts: dict[str, int] = {}
    for row in pdfs:
        crossref_status = row.get("crossref_status") or "unknown"
        status_counts[crossref_status] = status_counts.get(crossref_status, 0) + 1
    crossref_table = Table(title="Metadata", box=box.SIMPLE_HEAVY, show_header=True, header_style="bold")
    crossref_table.add_column("Crossref Status")
    crossref_table.add_column("PDFs", justify="right")
    for crossref_status, count in sorted(status_counts.items()):
        crossref_table.add_row(crossref_status, f"{count:,}")
    if not status_counts:
        crossref_table.add_row("-", "0")

    console.print(account_table)
    console.print(summary_table)
    console.print(crossref_table)


@app.command(
    epilog="""\b
Examples:
  nv update
  nv update --repo-url https://github.com/YOUR_NAME/YOUR_REPO.git
""",
)
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
