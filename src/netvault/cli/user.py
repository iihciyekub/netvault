from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Iterable

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError
import requests
import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from netvault import __version__
from netvault.cli.config import (
    HASH_CACHE_PATH,
    IDENTITY_CACHE_PATH,
    clear_credentials,
    load_credentials,
    load_upload_index_settings,
    save_credentials,
)
from netvault.cli.http import (
    api_get,
    api_post,
    auth_headers,
    http_session,
    raise_for_api_error,
    server_url,
    upload_pdf,
)
from netvault.cli.update import update_from_github
from netvault.cli.upload_index import DownloadIndexMatch, DownloadIndexResolver
from netvault.doi import (
    DOI_RESOLVER_VERSION,
    DoiEvidence,
    extract_doi_evidence,
    find_dois_in_text,
    normalize_doi,
)

DEFAULT_SERVER_URL = "https://iiaide.com/nv"
HASH_CACHE_VERSION = 1
IDENTITY_CACHE_VERSION = 1
PDF_MAGIC = b"%PDF-"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DEFAULT_UPLOAD_EXCLUDED_DIRS = {
    ".cache",
    ".git",
    ".Trash",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "Library",
    "node_modules",
    "output",
}


@dataclass(frozen=True)
class DownloadPlan:
    doi: str
    original_name: str
    size: int
    destination: Path
    part_path: Path
    sha256: str
    resume_offset: int = 0

app = typer.Typer(
    help="NetVault team PDF vault CLI.",
    epilog="""\b
Examples:
  nv login https://iiaide.com/nv
  nv upload ./paper.pdf ./papers
  nv check-pdfs ./papers
  nv doi ./paper.pdf --verbose
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


def iter_pdf_paths(path: Path, excluded_dir_names: set[str] | None = None) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() != ".pdf":
            raise typer.BadParameter(f"{path} is not a PDF file")
        return [path]
    if path.is_dir():
        excluded = excluded_dir_names or set()
        pdfs: list[Path] = []
        for root, directory_names, file_names in os.walk(path):
            directory_names[:] = sorted(name for name in directory_names if name not in excluded)
            root_path = Path(root)
            pdfs.extend(
                root_path / name
                for name in file_names
                if Path(name).suffix.lower() == ".pdf"
            )
        return sorted(pdfs)
    raise typer.BadParameter(f"{path} does not exist")


def collect_pdf_paths(
    paths: Iterable[Path],
    excluded_dir_names: set[str] | None = None,
) -> list[Path]:
    pdfs: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        for pdf_path in iter_pdf_paths(path, excluded_dir_names=excluded_dir_names):
            resolved = pdf_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            pdfs.append(pdf_path)
    return pdfs


def has_pdf_header(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(len(PDF_MAGIC)) == PDF_MAGIC


def pdf_open_error(path: Path) -> str | None:
    """Return an error when pypdf cannot open the document structure."""
    try:
        with path.open("rb") as handle:
            reader = PdfReader(handle, strict=False)
            if reader.is_encrypted:
                return None
            for page in reader.pages:
                page.get_object()
    except FileNotDecryptedError:
        return None
    except Exception as exc:
        detail = str(exc).strip()
        return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return None


def is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def file_sha256(path: Path, progress_callback=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            if progress_callback:
                progress_callback(len(chunk))
    return digest.hexdigest()


def file_cache_key(path: Path) -> str:
    return str(path.resolve())


def file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def load_hash_cache() -> dict[str, dict]:
    if not HASH_CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(HASH_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("version") != HASH_CACHE_VERSION:
        return {}
    files = payload.get("files")
    return files if isinstance(files, dict) else {}


def save_hash_cache(files: dict[str, dict]) -> None:
    HASH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    HASH_CACHE_PATH.write_text(
        json.dumps({"version": HASH_CACHE_VERSION, "files": files}, ensure_ascii=False),
        encoding="utf-8",
    )
    HASH_CACHE_PATH.chmod(0o600)


def cached_file_sha256(path: Path, cache: dict[str, dict], progress_callback=None) -> tuple[str, bool]:
    size, mtime_ns = file_signature(path)
    cached = cache.get(file_cache_key(path))
    if (
        isinstance(cached, dict)
        and cached.get("size") == size
        and cached.get("mtime_ns") == mtime_ns
        and isinstance(cached.get("sha256"), str)
    ):
        if progress_callback:
            progress_callback(size)
        return cached["sha256"], True
    sha256 = file_sha256(path, progress_callback=progress_callback)
    cache[file_cache_key(path)] = {"size": size, "mtime_ns": mtime_ns, "sha256": sha256}
    return sha256, False


def load_identity_cache() -> dict[str, dict]:
    if not IDENTITY_CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(IDENTITY_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("version") != IDENTITY_CACHE_VERSION:
        return {}
    identities = payload.get("identities")
    return identities if isinstance(identities, dict) else {}


def save_identity_cache(identities: dict[str, dict]) -> None:
    IDENTITY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = IDENTITY_CACHE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {"version": IDENTITY_CACHE_VERSION, "identities": identities},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(IDENTITY_CACHE_PATH)


def cached_identity(identities: dict[str, dict], sha256: str) -> dict | None:
    identity = identities.get(sha256)
    if not isinstance(identity, dict):
        return None
    if identity.get("source") == "user" and identity.get("status") == "confirmed":
        return identity
    if identity.get("resolver_version") == DOI_RESOLVER_VERSION:
        return identity
    return None


def identity_from_evidence(evidence: DoiEvidence, *, manual: bool = False) -> dict:
    if manual:
        status = "confirmed"
        source = "user"
        resolver_version = None
    else:
        status = evidence.status
        source = evidence.source
        resolver_version = DOI_RESOLVER_VERSION
    return {
        "doi": evidence.doi,
        "status": status,
        "source": source,
        "reason": evidence.reason,
        "resolver_version": resolver_version,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def manual_identity(doi: str) -> dict:
    normalized = normalize_doi(doi)
    evidence = DoiEvidence("ok", normalized, "explicit", [], None)
    return identity_from_evidence(evidence, manual=True)


def download_index_identity(match: DownloadIndexMatch) -> dict:
    return {
        "doi": match.record.doi,
        "status": "ok",
        "source": "download-index",
        "reason": None,
        "resolver_version": DOI_RESOLVER_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "index_path": str(match.index_path),
        "index_version": match.index_version,
        "index_updated_at": match.index_updated_at,
        "source_url": match.record.source_url,
        "validation_method": match.record.validation_method,
    }


def existing_pdf_from_response(response: requests.Response) -> dict | None:
    if response.status_code == 404:
        return None
    raise_for_api_error(response)
    return response.json()


def get_existing_pdf_by_sha256(sha256: str) -> dict | None:
    response = http_session().get(
        f"{server_url()}/pdfs/{sha256}",
        headers=auth_headers(),
        timeout=30,
    )
    return existing_pdf_from_response(response)


def get_existing_pdfs_by_sha256(hashes: Iterable[str], progress_callback=None) -> dict[str, dict]:
    unique_hashes = sorted({sha256 for sha256 in hashes if sha256})
    if not unique_hashes:
        return {}
    existing: dict[str, dict] = {}
    chunk_size = 500
    try:
        for index in range(0, len(unique_hashes), chunk_size):
            response = api_post("/pdfs/exists", json={"sha256": unique_hashes[index : index + chunk_size]})
            existing_payload = response.get("existing", {})
            if isinstance(existing_payload, dict):
                existing.update(existing_payload)
            if progress_callback:
                progress_callback(min(index + chunk_size, len(unique_hashes)))
        return existing
    except (RuntimeError, requests.RequestException):
        existing = {}
        for index, sha256 in enumerate(unique_hashes, start=1):
            pdf = get_existing_pdf_by_sha256(sha256)
            if pdf:
                existing[sha256] = pdf
            if progress_callback:
                progress_callback(index)
        return existing


def get_existing_pdfs_by_doi(dois: Iterable[str], progress_callback=None) -> dict[str, dict]:
    unique_dois = sorted({doi for doi in dois if doi})
    if not unique_dois:
        return {}
    existing: dict[str, dict] = {}
    chunk_size = 500
    try:
        for index in range(0, len(unique_dois), chunk_size):
            response = api_post(
                "/pdfs/exists",
                json={"doi": unique_dois[index : index + chunk_size]},
            )
            payload = response.get("existing_doi", {})
            if isinstance(payload, dict):
                existing.update(payload)
            if progress_callback:
                progress_callback(min(index + chunk_size, len(unique_dois)))
        return existing
    except (RuntimeError, requests.RequestException):
        existing = {}
        for index, doi in enumerate(unique_dois, start=1):
            pdf = get_existing_pdf_by_doi(doi)
            if pdf:
                existing[doi] = pdf
            if progress_callback:
                progress_callback(index)
        return existing


def register_pdf_aliases(aliases: Iterable[tuple[str, str]]) -> dict[str, dict]:
    unique = {(sha256.lower(), doi) for sha256, doi in aliases if sha256 and doi}
    registered: dict[str, dict] = {}
    chunk_size = 500
    ordered = sorted(unique)
    for index in range(0, len(ordered), chunk_size):
        response = api_post(
            "/pdfs/aliases",
            json={
                "aliases": [
                    {"sha256": sha256, "doi": doi}
                    for sha256, doi in ordered[index : index + chunk_size]
                ]
            },
        )
        payload = response.get("registered", {})
        if isinstance(payload, dict):
            registered.update(payload)
    return registered


def get_existing_pdf_by_doi(doi: str) -> dict | None:
    response = http_session().get(
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


def download_part_path(destination: Path) -> Path:
    return destination.with_name(f"{destination.name}.part")


def plan_download_destination(
    directory: Path,
    filename: str,
    size: int,
    used_destinations: set[Path],
    expected_sha256: str | None = None,
) -> tuple[Path, Path, int, bool]:
    safe_name = Path(filename).name
    base = directory / safe_name
    stem = base.stem
    suffix = base.suffix
    counter = 1
    while True:
        destination = base if counter == 1 else directory / f"{stem}-{counter}{suffix}"
        part_path = download_part_path(destination)
        if destination in used_destinations:
            counter += 1
            continue
        if destination.exists():
            if destination.stat().st_size == size:
                if expected_sha256 is None or file_sha256(destination) == expected_sha256:
                    part_path.unlink(missing_ok=True)
                    used_destinations.add(destination)
                    return destination, part_path, size, True
            counter += 1
            continue
        if part_path.exists():
            part_size = part_path.stat().st_size
            if part_size == size:
                if expected_sha256 is None or file_sha256(part_path) == expected_sha256:
                    part_path.replace(destination)
                    used_destinations.add(destination)
                    return destination, part_path, size, True
                part_path.unlink(missing_ok=True)
                part_size = 0
            if part_size > size:
                part_path.unlink(missing_ok=True)
                part_size = 0
            used_destinations.add(destination)
            return destination, part_path, part_size, False
        used_destinations.add(destination)
        return destination, part_path, 0, False


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


def render_doi_evidence(path: Path, explicit_doi: str | None = None, verbose: bool = False) -> None:
    evidence = extract_doi_evidence(path, explicit_doi=explicit_doi)
    status_style = "green" if evidence.status == "ok" else "yellow" if evidence.status == "conflict" else "red"
    selected = evidence.doi or "-"
    source = evidence.source or "-"
    console.print(
        Panel.fit(
            f"[bold]File[/bold] {path.name}\n"
            f"[bold]Status[/bold] [{status_style}]{evidence.status}[/{status_style}]\n"
            f"[bold]Selected[/bold] {selected}\n"
            f"[bold]Source[/bold] {source}\n"
            f"[bold]Reason[/bold] {evidence.reason or '-'}",
            title="DOI Resolver",
        )
    )
    table = Table(title="Candidates", box=box.SIMPLE_HEAVY, show_header=True, header_style="bold")
    table.add_column("Score", justify="right")
    table.add_column("Source")
    table.add_column("DOI")
    table.add_column("Detail")
    if verbose:
        table.add_column("Context")
    candidates = sorted(
        evidence.candidates,
        key=lambda candidate: (candidate.score, candidate.source, candidate.doi),
        reverse=True,
    )
    for candidate in candidates:
        row = [
            str(candidate.score),
            candidate.source,
            candidate.doi,
            candidate.detail or "-",
        ]
        if verbose:
            row.append(candidate.context or "-")
        table.add_row(*row)
    if not candidates:
        empty_row = ["-", "-", "-", "-"]
        if verbose:
            empty_row.append("-")
        table.add_row(*empty_row)
    console.print(table)


def find_existing_pdf_before_upload(
    path: Path,
    explicit_doi: str | None,
    sha256: str,
    existing_by_sha: dict[str, dict],
) -> dict | None:
    existing_by_hash = existing_by_sha.get(sha256)
    if existing_by_hash:
        return existing_by_hash
    doi = extract_local_doi(path, explicit_doi)
    if doi:
        existing = get_existing_pdf_by_doi(doi)
        if existing:
            return existing
    return None


def save_login(server: str, username: str, password: str) -> None:
    response = http_session().post(
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
    response = http_session().get(
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
        response = http_session().post(f"{server_url()}/auth/logout", headers=auth_headers(), timeout=30)
        raise_for_api_error(response)
    finally:
        clear_credentials()
    console.print("Logged out.")


@app.command(
    "check-pdfs",
    epilog="""\b
Examples:
  nv check-pdfs
  nv check-pdfs ~/Downloads/papers
  nv check-pdfs ./papers --dry-run

\b
Notes:
  Only .pdf/.PDF files are scanned, recursively.
  PDFs that cannot be opened are moved to ./error under the current working directory.
  Encrypted PDFs are kept because encryption alone does not mean a file is damaged.
""",
)
def check_pdfs_command(
    directory: Path = typer.Argument(
        Path("."),
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        help="Directory to scan recursively (default: current directory).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report invalid PDFs without moving them."),
) -> None:
    error_directory = Path.cwd() / "error"
    pdfs = [
        path
        for path in iter_pdf_paths(directory)
        if not is_within(path, error_directory)
    ]
    if not pdfs:
        console.print("No PDF files found.")
        return

    invalid: list[tuple[Path, str]] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed:.0f}/{task.total:.0f} PDFs"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Checking PDFs", total=len(pdfs))
        for pdf_path in pdfs:
            error = pdf_open_error(pdf_path)
            if error:
                invalid.append((pdf_path, error))
            progress.advance(task)

    if not invalid:
        console.print(f"done: {len(pdfs)} PDFs checked; all files can be opened")
        return

    used_destinations: set[Path] = set()
    failures: list[tuple[Path, str]] = []
    moved = 0
    for pdf_path, error in invalid:
        destination = unique_destination(error_directory, pdf_path.name, used_destinations)
        if dry_run:
            console.print(f"invalid: {pdf_path}: {error}; would move to {destination}")
            continue
        try:
            error_directory.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pdf_path), str(destination))
        except OSError as exc:
            failures.append((pdf_path, str(exc)))
            console.print(f"failed: {pdf_path}: {exc}")
            continue
        moved += 1
        console.print(f"moved: {pdf_path} -> {destination}: {error}")

    if dry_run:
        console.print(f"dry run: {len(pdfs)} PDFs checked; {len(invalid)} invalid")
    else:
        console.print(
            f"done: {len(pdfs)} PDFs checked; {moved} invalid moved to {error_directory}; "
            f"{len(failures)} move failures"
        )
    if failures:
        raise typer.Exit(1)


@app.command(
    "upload",
    epilog="""\b
Examples:
  nv upload ./paper.pdf
  nv upload ./paper-a.pdf ./paper-b.pdf
  nv upload ~/Downloads/papers
  nv upload ./paper.pdf --doi 10.1016/j.ijpe.2018.04.006
  nv upload ./new-paper.pdf --force
  nv upload ~/Downloads/papers --no-crossref
  nv upload ~/Downloads/papers --index-file ./pdf-download-index.json

\b
Notes:
  Directories are scanned recursively for .pdf/.PDF files.
  Existing PDFs are skipped before DOI extraction/upload by checking sha256 first.
  File hashes are cached locally in ~/.config/netvault/hash-cache.json.
  A sibling pdf-download-index.json is preferred over PDF DOI extraction.
""",
)
def upload_command(
    paths: list[Path] = typer.Argument(..., exists=True, readable=True),
    doi: str | None = typer.Option(None, "--doi", help="Use this DOI instead of extracting it."),
    no_crossref: bool = typer.Option(False, "--no-crossref", help="Skip Crossref metadata lookup."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace the existing PDF for its DOI and refresh all metadata from Crossref.",
    ),
    refresh_doi: bool = typer.Option(
        False,
        "--refresh-doi",
        help="Re-run automatic DOI resolution, but keep user-confirmed identities.",
    ),
    exclude_dir: list[str] | None = typer.Option(
        None,
        "--exclude-dir",
        help="Additional directory name to skip during recursive scans; repeat as needed.",
    ),
    index_file: Path | None = typer.Option(
        None,
        "--index-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="Use this version 1 download index for every selected PDF.",
    ),
    no_index: bool = typer.Option(
        False,
        "--no-index",
        help="Ignore download index files and use cached or PDF-derived DOI identities.",
    ),
) -> None:
    ensure_logged_in()
    excluded_dir_names = DEFAULT_UPLOAD_EXCLUDED_DIRS | set(exclude_dir or [])
    pdfs = collect_pdf_paths(paths, excluded_dir_names=excluded_dir_names)
    if not pdfs:
        console.print("No PDF files found.")
        return

    if doi and len(pdfs) != 1:
        raise typer.BadParameter("--doi can only be used when uploading one PDF file")
    if force and no_crossref:
        raise typer.BadParameter("--force cannot be combined with --no-crossref")
    if index_file is not None and no_index:
        raise typer.BadParameter("--index-file cannot be combined with --no-index")
    try:
        configured_index_enabled, index_names = load_upload_index_settings()
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    index_resolver = DownloadIndexResolver(
        index_names,
        explicit_path=index_file,
        enabled=not no_index and (configured_index_enabled or index_file is not None),
    )

    uploaded = 0
    replaced = 0
    deduped = 0
    skipped = 0
    failed: list[tuple[Path, str]] = []
    latest_pdf: dict | None = None
    hash_cache = load_hash_cache()
    identity_cache = load_identity_cache()
    cache_changed = False
    identity_cache_pending = 0
    doi_cache_hits = 0
    doi_scans = 0
    download_index_hits = 0
    download_index_renames = 0
    hashes_by_path: dict[Path, str] = {}
    existing_by_sha: dict[str, dict] = {}
    unique_pdf_paths: list[Path] = []
    local_duplicate_paths = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed:.0f}/{task.total:.0f} PDFs"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Hashing PDFs", total=len(pdfs))
        for pdf_path in pdfs:
            try:
                if not has_pdf_header(pdf_path):
                    failed.append((pdf_path, "invalid PDF file: expected %PDF- header"))
                    continue
                cached_before = file_cache_key(pdf_path) in hash_cache
                sha256, from_cache = cached_file_sha256(pdf_path, hash_cache)
                hashes_by_path[pdf_path] = sha256
                cache_changed = cache_changed or not from_cache or not cached_before
            except OSError as exc:
                failed.append((pdf_path, str(exc)))
            finally:
                progress.advance(task, 1)

        if cache_changed:
            save_hash_cache(hash_cache)

        pdf_paths_by_sha: dict[str, list[Path]] = {}
        for pdf_path, sha256 in hashes_by_path.items():
            pdf_paths_by_sha.setdefault(sha256, []).append(pdf_path)
        unique_pdf_paths = [paths[0] for paths in pdf_paths_by_sha.values()]
        local_duplicate_paths = len(hashes_by_path) - len(unique_pdf_paths)

        progress.reset(
            task,
            total=len(unique_pdf_paths),
            completed=0,
            description="Checking unique PDFs on server",
        )
        existing_by_sha = get_existing_pdfs_by_sha256(
            pdf_paths_by_sha,
            progress_callback=lambda completed: progress.update(task, completed=completed),
        )

        new_pdfs: list[Path] = []
        for pdf_path in unique_pdf_paths:
            sha256 = hashes_by_path[pdf_path]
            existing_pdf = existing_by_sha.get(sha256)
            if existing_pdf and not force:
                latest_pdf = existing_pdf
                skipped += 1
            else:
                new_pdfs.append(pdf_path)

        dois_by_path: dict[Path, str] = {}
        doi_sources_by_path: dict[Path, str] = {}
        upload_candidates: list[Path] = []
        progress.reset(
            task,
            total=len(new_pdfs),
            completed=0,
            description="Resolving DOI identities",
        )
        for pdf_path in new_pdfs:
            try:
                sha256 = hashes_by_path[pdf_path]
                identity = cached_identity(identity_cache, sha256)
                if refresh_doi and identity and identity.get("source") != "user":
                    identity = None

                index_match = None
                if not doi:
                    index_match = index_resolver.resolve(
                        pdf_path,
                        sha256,
                        pdf_path.stat().st_size,
                    )

                if doi:
                    identity = manual_identity(doi)
                    identity_cache[sha256] = identity
                    identity_cache_pending += 1
                elif identity and identity.get("source") == "user":
                    if index_match and normalize_doi(identity["doi"]) != index_match.record.doi:
                        raise RuntimeError(
                            "User-confirmed DOI conflicts with download index DOI "
                            f"{index_match.record.doi}"
                        )
                    doi_cache_hits += 1
                elif index_match:
                    identity = download_index_identity(index_match)
                    identity_cache[sha256] = identity
                    identity_cache_pending += 1
                    download_index_hits += 1
                    if index_match.renamed:
                        download_index_renames += 1
                elif identity:
                    doi_cache_hits += 1
                else:
                    evidence = extract_doi_evidence(pdf_path)
                    identity = identity_from_evidence(evidence)
                    identity_cache[sha256] = identity
                    identity_cache_pending += 1
                    doi_scans += 1

                extracted = identity.get("doi")
                if identity.get("status") not in {"ok", "confirmed"} or not isinstance(extracted, str):
                    reason = identity.get("reason") or "No DOI found"
                    failed.append(
                        (
                            pdf_path,
                            f"{reason}. Confirm with: nv doi {pdf_path} --set DOI",
                        )
                    )
                    continue
                dois_by_path[pdf_path] = extracted
                doi_sources_by_path[pdf_path] = str(identity.get("source") or "explicit")
                upload_candidates.append(pdf_path)
            except (RuntimeError, OSError, ValueError) as exc:
                failed.append((pdf_path, str(exc)))
            finally:
                if identity_cache_pending >= 25:
                    save_identity_cache(identity_cache)
                    identity_cache_pending = 0
                progress.advance(task, 1)
        if identity_cache_pending:
            save_identity_cache(identity_cache)
            identity_cache_pending = 0
        progress.reset(
            task,
            total=len(dois_by_path),
            completed=0,
            description="Checking DOI duplicates",
        )
        existing_by_doi = get_existing_pdfs_by_doi(
            dois_by_path.values(),
            progress_callback=lambda completed: progress.update(task, completed=completed),
        )
        new_pdfs = []
        aliases_to_register: list[tuple[str, str]] = []
        for pdf_path in upload_candidates:
            existing_pdf = existing_by_doi.get(dois_by_path[pdf_path])
            if existing_pdf and not force:
                latest_pdf = existing_pdf
                skipped += 1
                sha256 = hashes_by_path[pdf_path]
                if existing_pdf.get("sha256") != sha256:
                    aliases_to_register.append((sha256, dois_by_path[pdf_path]))
            else:
                new_pdfs.append(pdf_path)
        if aliases_to_register:
            try:
                register_pdf_aliases(aliases_to_register)
            except (RuntimeError, requests.RequestException) as exc:
                # Alias persistence is an optimization. The DOI duplicate has
                # already been safely identified, so a legacy server or transient
                # failure must not turn a successful skip into an upload failure.
                console.print(f"warning: could not register PDF aliases: {exc}")

        if not new_pdfs:
            progress.update(
                task,
                description="Done",
                completed=len(unique_pdf_paths),
                total=len(unique_pdf_paths),
            )
        else:
            progress.reset(task, total=len(new_pdfs), completed=0, description="Indexing new PDFs")

        for pdf_path in new_pdfs:
            try:
                sha256 = hashes_by_path[pdf_path]
                result = upload_pdf(
                    pdf_path,
                    doi=dois_by_path[pdf_path],
                    doi_source=doi_sources_by_path[pdf_path],
                    no_crossref=no_crossref,
                    force=force,
                    sha256=sha256,
                )
                latest_pdf = result["pdf"]
                if result.get("replaced"):
                    replaced += 1
                elif result["deduplicated"]:
                    deduped += 1
                else:
                    uploaded += 1
            except (RuntimeError, OSError, requests.RequestException) as exc:
                failed.append((pdf_path, str(exc)))
            finally:
                progress.advance(task, 1)

    parts = [
        f"found paths: {len(pdfs)}",
        f"unique PDFs: {len(unique_pdf_paths)}",
        f"local duplicate paths: {local_duplicate_paths}",
        f"already stored: {skipped} skipped",
        f"uploaded: {uploaded}",
    ]
    if replaced:
        parts.append(f"{replaced} replaced")
    if deduped:
        parts.append(f"{deduped} deduped")
    if doi_cache_hits:
        parts.append(f"{doi_cache_hits} DOI cache hits")
    if doi_scans:
        parts.append(f"{doi_scans} DOI scans")
    if download_index_hits:
        parts.append(f"{download_index_hits} download index hits")
    if download_index_renames:
        parts.append(f"{download_index_renames} matched after rename")
    if failed:
        parts.append(f"{len(failed)} failed")
    if latest_pdf:
        title = latest_pdf.get("title") or latest_pdf["original_name"]
        parts.append(f"latest: {latest_pdf['doi']}  {title}")
    console.print("\n".join(parts))
    for pdf_path, error in failed:
        console.print(f"failed: {pdf_path}: {error}")
    if failed:
        raise typer.Exit(1)


@app.command(
    "doi",
    epilog="""\b
Examples:
  nv doi ./paper.pdf
  nv doi ./paper.pdf --verbose
  nv doi ./paper.pdf --doi 10.1016/j.ijpe.2018.04.006
  nv doi ./paper.pdf --set 10.1016/j.ijpe.2018.04.006
  nv doi ./paper.pdf --show-cache
  nv doi ./paper.pdf --remove

\b
Notes:
  This runs the same smart DOI resolver used by upload.
  Use --verbose to inspect candidate DOI values and why one was selected.
  --set saves a user-confirmed SHA-to-DOI identity that survives file renames.
""",
)
def doi_command(
    path: Path = typer.Argument(..., exists=True, readable=True, help="PDF file to inspect."),
    doi: str | None = typer.Option(None, "--doi", help="Explicit DOI to validate and display."),
    set_doi: str | None = typer.Option(
        None,
        "--set",
        help="Save a user-confirmed DOI identity for this PDF's SHA-256.",
    ),
    remove: bool = typer.Option(False, "--remove", help="Remove the cached identity for this PDF."),
    show_cache: bool = typer.Option(
        False,
        "--show-cache",
        help="Show the cached identity without parsing the PDF.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show context for each DOI candidate."),
) -> None:
    if path.suffix.lower() != ".pdf":
        raise typer.BadParameter(f"{path} is not a PDF file")
    actions = int(set_doi is not None) + int(remove) + int(show_cache)
    if actions > 1 or (doi and actions):
        raise typer.BadParameter("Use only one of --doi, --set, --remove, or --show-cache")

    if set_doi is not None or remove or show_cache:
        hash_cache = load_hash_cache()
        sha256, from_cache = cached_file_sha256(path, hash_cache)
        if not from_cache:
            save_hash_cache(hash_cache)
        identities = load_identity_cache()

        if set_doi is not None:
            identity = manual_identity(set_doi)
            identities[sha256] = identity
            save_identity_cache(identities)
            console.print(
                f"saved: {path} -> {identity['doi']} "
                f"(source=user, sha256={sha256})"
            )
            return
        if remove:
            removed = identities.pop(sha256, None)
            if removed is not None:
                save_identity_cache(identities)
                console.print(f"removed: {path} (sha256={sha256})")
            else:
                console.print(f"no cached identity: {path} (sha256={sha256})")
            return

        identity = identities.get(sha256)
        if not isinstance(identity, dict):
            console.print(f"no cached identity: {path} (sha256={sha256})")
            return
        console.print(
            Panel.fit(
                f"[bold]File[/bold] {path.name}\n"
                f"[bold]SHA-256[/bold] {sha256}\n"
                f"[bold]DOI[/bold] {identity.get('doi') or '-'}\n"
                f"[bold]Status[/bold] {identity.get('status') or '-'}\n"
                f"[bold]Source[/bold] {identity.get('source') or '-'}\n"
                f"[bold]Reason[/bold] {identity.get('reason') or '-'}",
                title="Cached DOI Identity",
            )
        )
        return
    render_doi_evidence(path, explicit_doi=doi, verbose=verbose)


@app.command(
    "list",
    epilog="""\b
Examples:
  nv list
""",
)
def list_command(
    limit: int = typer.Option(100, min=1, max=500, help="Maximum PDFs to show."),
    offset: int = typer.Option(0, min=0, help="Number of PDFs to skip."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    rows = api_get("/pdfs", limit=limit, offset=offset)
    if json_output:
        typer.echo(json.dumps(rows, ensure_ascii=False))
    else:
        render_pdf_table(rows)


@app.command(
    epilog="""\b
Examples:
  nv search supply
  nv search 10.1016
""",
)
def search(
    query: str = typer.Argument(...),
    limit: int = typer.Option(100, min=1, max=500, help="Maximum PDFs to show."),
    offset: int = typer.Option(0, min=0, help="Number of PDFs to skip."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    rows = api_get("/pdfs/search", q=query, limit=limit, offset=offset)
    if json_output:
        typer.echo(json.dumps(rows, ensure_ascii=False))
    else:
        render_pdf_table(rows)


def fetch_pdf_detail_for_download(doi: str) -> tuple[str, dict | None, str | None]:
    try:
        return doi, api_get("/pdfs/by-doi", doi=doi), None
    except (RuntimeError, requests.RequestException) as exc:
        return doi, None, str(exc)


@app.command(
    epilog="""\b
Examples:
  nv download 10.1016/j.ijpe.2018.04.006 --to ~/Downloads
  nv download 10.1016/j.ijpe.2018.04.006 10.1234/example.doi --to ./downloads
  nv download --file ./dois.txt --to ./downloads
  nv download 10.1016/j.ijpe.2018.04.006 --file ./more-dois.txt --to ./downloads
  nv download --file ./dois.txt --to ./downloads --workers 8

\b
Notes:
  --file reads any text file and extracts DOI values with NetVault's DOI regex.
  Duplicate DOI values are downloaded once.
  Downloads use parallel workers and automatically resume .part files.
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
    workers: int = typer.Option(8, "--workers", "-w", min=1, max=32, help="Parallel downloads."),
) -> None:
    ensure_logged_in()
    requested_dois = collect_dois(dois or [], doi_files)
    if not requested_dois:
        raise typer.BadParameter("Provide at least one DOI or --file PATH")

    details_by_doi: dict[str, dict] = {}
    failed: list[tuple[str, str]] = []
    worker_count = min(workers, max(1, len(requested_dois)))
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed:.0f}/{task.total:.0f} PDFs"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Checking PDFs", total=len(requested_dois))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(fetch_pdf_detail_for_download, doi) for doi in requested_dois]
            for future in as_completed(futures):
                doi, detail, error = future.result()
                if detail is None:
                    failed.append((doi, error or "PDF not found"))
                else:
                    details_by_doi[doi] = detail
                progress.advance(task, 1)

        details = [details_by_doi[doi] for doi in requested_dois if doi in details_by_doi]

        if not details:
            progress.update(task, description="Done", completed=len(requested_dois), total=len(requested_dois))
            console.print(f"done: 0/{len(requested_dois)} downloaded; {len(failed)} failed")
            for doi, error in failed:
                console.print(f"failed: {doi}: {error}")
            raise typer.Exit(1)

        to.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        resumed = 0
        skipped = 0
        used_destinations: set[Path] = set()
        plans: list[DownloadPlan] = []
        for detail in details:
            size = int(detail["size"])
            destination, part_path, resume_offset, complete = plan_download_destination(
                to,
                detail["original_name"],
                size,
                used_destinations,
                expected_sha256=detail["sha256"],
            )
            if complete:
                skipped += 1
                continue
            plans.append(
                DownloadPlan(
                    doi=detail["doi"],
                    original_name=detail["original_name"],
                    size=size,
                    destination=destination,
                    part_path=part_path,
                    sha256=detail["sha256"],
                    resume_offset=resume_offset,
                )
            )

        if not plans:
            progress.update(task, description="Done", completed=len(requested_dois), total=len(requested_dois))
            parts = [f"done: {skipped}/{len(requested_dois)} processed"]
            if skipped:
                parts.append(f"{skipped} skipped")
            if failed:
                parts.append(f"{len(failed)} failed")
            parts.append(f"to: {to}")
            console.print("; ".join(parts))
            for doi, error in failed:
                console.print(f"failed: {doi}: {error}")
            if failed:
                raise typer.Exit(1)
            return

        base_url = server_url()
        headers = auth_headers()
        worker_count = min(workers, max(1, len(plans)))
        progress.reset(task, total=len(plans), completed=0, description="Downloading PDFs")

        def run_plan(plan: DownloadPlan) -> tuple[str, str, Path]:
            request_headers = dict(headers)
            mode = "wb"
            did_resume = False
            if plan.resume_offset > 0:
                request_headers["Range"] = f"bytes={plan.resume_offset}-"
            with http_session().get(
                f"{base_url}/pdfs/by-doi/download",
                headers=request_headers,
                params={"doi": plan.doi},
                timeout=300,
                stream=True,
            ) as response:
                raise_for_api_error(response)
                if plan.resume_offset > 0 and response.status_code == 206:
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {plan.resume_offset}-"):
                        raise RuntimeError("server returned an invalid Content-Range")
                    mode = "ab"
                    did_resume = True
                elif plan.resume_offset > 0:
                    plan.part_path.unlink(missing_ok=True)
                with plan.part_path.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if chunk:
                            handle.write(chunk)
            actual_size = plan.part_path.stat().st_size
            if actual_size != plan.size:
                raise RuntimeError(f"incomplete download: expected {plan.size} bytes, got {actual_size}")
            actual_sha256 = file_sha256(plan.part_path)
            if actual_sha256 != plan.sha256:
                raise RuntimeError(
                    f"checksum mismatch: expected {plan.sha256}, got {actual_sha256}"
                )
            plan.part_path.replace(plan.destination)
            return plan.doi, "resumed" if did_resume else "downloaded", plan.destination

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(run_plan, plan): plan for plan in plans}
            for future in as_completed(futures):
                plan = futures[future]
                try:
                    _, status_name, _ = future.result()
                    if status_name == "resumed":
                        resumed += 1
                    else:
                        downloaded += 1
                except (RuntimeError, OSError, requests.RequestException) as exc:
                    failed.append((plan.doi, str(exc)))
                finally:
                    progress.advance(task, 1)

    ok_count = downloaded + resumed + skipped
    parts = [f"done: {ok_count}/{len(requested_dois)} processed"]
    if downloaded:
        parts.append(f"{downloaded} downloaded")
    if resumed:
        parts.append(f"{resumed} resumed")
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        parts.append(f"{len(failed)} failed")
    parts.append(f"to: {to}")
    console.print("; ".join(parts))
    for doi, error in failed:
        console.print(f"failed: {doi}: {error}")
    if failed:
        raise typer.Exit(1)


@app.command(
    epilog="""\b
Examples:
  nv status
""",
)
def status() -> None:
    me = api_get("/me")
    summary = api_get("/stats/summary")
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

    summary_table = Table(title="Library", box=box.SIMPLE_HEAVY, show_header=True, header_style="bold")
    summary_table.add_column("Metric")
    summary_table.add_column("Value", justify="right")
    summary_table.add_row("Active PDFs", f"{summary['active_pdfs']:,}")
    summary_table.add_row("Total Size", format_bytes(summary["total_size"]))
    summary_table.add_row("Uploaders", f"{summary['uploaders']:,}")
    year_range = (
        f"{summary['min_year']}-{summary['max_year']}"
        if summary.get("min_year") is not None
        else "-"
    )
    summary_table.add_row("Year Range", year_range)

    console.print(account_table)
    console.print(summary_table)


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


def run() -> None:
    try:
        app()
    except (RuntimeError, OSError, requests.RequestException) as exc:
        console.print(f"error: {exc}", style="red", highlight=False)
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()
