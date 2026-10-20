from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import csv
import io
import json
import os
from pathlib import Path
import re
import shutil
from threading import Event
from time import monotonic, sleep, time_ns
from typing import Iterable

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PyPdfError
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
    ApiError,
    api_get,
    api_post,
    auth_headers,
    http_session,
    raise_for_api_error,
    server_url,
    upload_pdf,
    verify_pdf_doi,
)
from netvault.cli.pdf_meta import (
    check_pdf_writeback_safety,
    read_pdf_metadata,
    write_pdf_metadata,
    write_verified_pdf_doi,
)
from netvault.cli.cache import CacheWriter, load_cache, save_cache
from netvault.cli.update import update_from_github
from netvault.cli.export import default_export_destination, normalize_export_issn, save_issn_export
from netvault.cli.upload_pipeline import UploadPipeline, UploadRateColumn, UploadStats
from netvault.cli.upload_index import DownloadIndexMatch, DownloadIndexRecord, DownloadIndexResolver
from netvault.cli.trusted_csv import TrustedDoiCsv
from netvault.cli.upload_fast import FastIdentity, probe_pdf, confirms_existing, check_new_titles
from netvault.cli.upload_report import UploadReport, failed_paths
from netvault.cli.history import History, run_root
from netvault.cli.upload_state import UploadState, RETRY_STATUSES, stable_context
from netvault.doi import (
    DOI_RESOLVER_VERSION,
    DoiEvidence,
    extract_document_info_evidence,
    extract_doi_evidence,
    find_dois_in_text,
    find_filename_doi_from_name,
    normalize_doi,
)

DEFAULT_SERVER_URL = "https://iiaide.com/nv"
HASH_CACHE_VERSION = 1
IDENTITY_CACHE_VERSION = 1
PDF_MAGIC = b"%PDF-"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DOWNLOAD_LOOKUP_BATCH_SIZE = 200
# The server /pdfs/exists endpoint accepts up to 500 DOI values per request.
# Use large DOI preflight batches for ordinary uploads, but retain small
# streaming batches for force/writeback workflows that must transfer files.
UPLOAD_PREPARE_BATCH_SIZE = 500
UPLOAD_STREAM_BATCH_SIZE = 12
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
    # Offline tidy collection folders are never bulk-uploaded.
    "duplicates",
    "unidentifys",
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
  nv tidy ./papers --apply
  nv doi ./paper.pdf --verbose
  nv pdf-meta read ./paper.pdf
  nv pdf-meta write ./paper.pdf --doi 10.1234/example
  nv download 10.1016/j.ijpe.2018.04.006 --to ./downloads
  nv download --file ./dois.txt --to ./downloads
  nv export --issn 0025-1909
  nv update
""",
    rich_markup_mode=None,
    context_settings={"token_normalize_func": str.lower},
)
console = Console()



pdf_meta_app = typer.Typer(
    help="Read or write PDF Document Info metadata (offline).",
)
app.add_typer(pdf_meta_app, name="pdf-meta")


@pdf_meta_app.command("read")
def pdf_meta_read(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    json_output: bool = typer.Option(False, "--json", help="Output metadata as JSON."),
) -> None:
    """Read PDF metadata without extracting or scanning page text."""
    try:
        details = read_pdf_metadata(path)
    except (ValueError, OSError, PyPdfError) as exc:
        raise RuntimeError(str(exc)) from exc
    if json_output:
        typer.echo(json.dumps(details, ensure_ascii=False, indent=2))
        return
    console.print(f"File: {details['file']}", markup=False)
    console.print(f"DOI: {details['doi'] or '-'}  (status: {details['status']})", markup=False)
    console.print("Document Info:", markup=False)
    for key, value in sorted(details["document_info"].items()):
        console.print(f"  {key}: {value}", markup=False)
    for item in details["doi_sources"]:
        console.print(f"  DOI {item['doi']} from {item['location']}", markup=False)


@pdf_meta_app.command("write")
def pdf_meta_write(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    doi: str = typer.Option(..., "--doi", help="DOI to embed into the PDF."),
    output: Path | None = typer.Option(None, "--output", help="Save to a new path instead of updating the input."),
    replace: bool = typer.Option(False, "--replace", help="Replace conflicting PDF Document Info DOI values."),
    title: str | None = typer.Option(None, "--title", help="Set PDF /Title."),
    author: str | None = typer.Option(None, "--author", help="Set PDF /Author."),
    subject: str | None = typer.Option(None, "--subject", help="Set PDF /Subject."),
    keywords: str | None = typer.Option(None, "--keywords", help="Set PDF /Keywords."),
) -> None:
    """Atomically write DOI and optional Info fields using an incremental PDF update."""
    try:
        result = write_pdf_metadata(
            path, doi, output=output, replace=replace, title=title,
            author=author, subject=subject, keywords=keywords,
        )
    except (ValueError, OSError, PyPdfError) as exc:
        raise RuntimeError(str(exc)) from exc
    action = "Updated" if result["changed"] else "Already current"
    console.print(f"{action}: {result['file']} (DOI: {result['doi']})", markup=False)
    if result["changed"]:
        console.print("PDF bytes/SHA-256 changed; re-upload if this PDF is already in NetVault.")


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
        excluded = {name.casefold() for name in (excluded_dir_names or set())}
        pdfs: list[Path] = []
        for root, directory_names, file_names in os.walk(path):
            directory_names[:] = sorted(name for name in directory_names if name.casefold() not in excluded)
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


def doi_scan_workers(file_count: int) -> int:
    """Choose modest PDF extraction concurrency from this machine's CPU count.

    On Linux prefer the current process affinity (including containers);
    on macOS/Windows fall back to logical CPU count. Never create more than
    six workers, and avoid extra threads for a one-file upload.
    """
    try:
        logical_cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        logical_cpus = os.cpu_count() or 1
    return max(1, min(file_count, 6, (logical_cpus + 1) // 2))


def iter_scan_upload_dois(
    paths: list[Path], *, for_tidy: bool = False,
):
    """Yield (original index, probe result) as each PDF finishes.

    Bound pending work to twice the worker count, so a slow first PDF
    cannot freeze the progress bar or queue thousands of PDFs at once.
    """
    def inspect(path: Path) -> tuple[FastIdentity | None, str | None]:
        try:
            if for_tidy and (path.is_symlink() or not path.is_file()):
                raise ValueError("Symlink or non-regular PDF is not modified")
            if not has_pdf_header(path):
                if for_tidy:
                    raise ValueError("Invalid PDF header")
                return None, None
            if for_tidy:
                return probe_pdf(path, allow_filename=False, strict=True), None
            return probe_pdf(path), None
        except (OSError, ValueError) as exc:
            return None, str(exc)

    workers = doi_scan_workers(len(paths))
    if workers == 1:
        for index, path in enumerate(paths):
            yield index, inspect(path)
        return

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nv-doi") as executor:
        pending = {}
        next_index = 0
        while next_index < min(len(paths), workers * 2):
            pending[executor.submit(inspect, paths[next_index])] = next_index
            next_index += 1
        while pending:
            ready, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in ready:
                index = pending.pop(future)
                yield index, future.result()
                if next_index < len(paths):
                    pending[executor.submit(inspect, paths[next_index])] = next_index
                    next_index += 1


def scan_upload_dois(
    paths: list[Path], *, for_tidy: bool = False,
) -> list[tuple[FastIdentity | None, str | None]]:
    """Return ordered probe results for dry-run/tidy callers.

    Live uploads consume iter_scan_upload_dois directly to show immediate
    progress without buffering every first-page text extraction.
    """
    results = [(None, None)] * len(paths)
    for index, outcome in iter_scan_upload_dois(paths, for_tidy=for_tidy):
        results[index] = outcome
    return results


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
    return load_cache(HASH_CACHE_PATH, "files", HASH_CACHE_VERSION)


def save_hash_cache(files: dict[str, dict]) -> None:
    save_cache(HASH_CACHE_PATH, "files", HASH_CACHE_VERSION, files)


def cached_file_sha256(path: Path, cache: dict[str, dict], progress_callback=None) -> tuple[str, bool]:
    stat = path.stat()
    signature = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                 "ctime_ns": stat.st_ctime_ns, "device": stat.st_dev, "inode": stat.st_ino}
    size = stat.st_size
    cached = cache.get(file_cache_key(path))
    if (
        isinstance(cached, dict)
        and all(cached.get(key) == value for key, value in signature.items())
        and isinstance(cached.get("sha256"), str)
    ):
        if progress_callback:
            progress_callback(size)
        return cached["sha256"], True
    sha256 = file_sha256(path, progress_callback=progress_callback)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_dev, after.st_ino) != (
        stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino
    ):
        raise RuntimeError("File changed while hashing; retry after it stops changing")
    cache[file_cache_key(path)] = {**signature, "sha256": sha256, "cache_updated_ns": time_ns()}
    return sha256, False


def load_identity_cache() -> dict[str, dict]:
    return load_cache(IDENTITY_CACHE_PATH, "identities", IDENTITY_CACHE_VERSION)


def save_identity_cache(identities: dict[str, dict], *, removed=()) -> None:
    save_cache(IDENTITY_CACHE_PATH, "identities", IDENTITY_CACHE_VERSION, identities, removed=removed)


def cached_identity(identities: dict[str, dict], sha256: str) -> dict | None:
    identity = identities.get(sha256)
    if not isinstance(identity, dict):
        return None
    # CSV trust belongs to an explicit upload invocation, never a cached identity.
    if identity.get("source") == "csv-trusted":
        return None
    if identity.get("source") == "user" and identity.get("status") == "confirmed":
        return identity
    if (
        identity.get("resolver_version") == DOI_RESOLVER_VERSION
        and identity.get("status") in {"ok", "no-doi", "conflict"}
        and (identity.get("status") != "ok" or isinstance(identity.get("doi"), str))
    ):
        return identity
    return None


def store_identity(identities: dict[str, dict], sha256: str, identity: dict) -> None:
    previous = identities.get(sha256, {})
    identities[sha256] = dict(identity)
    if isinstance(previous, dict) and isinstance(previous.get("upload_rejections"), dict):
        identities[sha256]["upload_rejections"] = previous["upload_rejections"]


def is_identity_rejection(error: Exception) -> bool:
    # Do not remember generic 422 request validation, authentication or outages.
    if not isinstance(error, ApiError):
        return False
    if error.status_code == 400:
        return "No DOI found" in str(error)
    return error.status_code == 422 and any(
        reason in str(error) for reason in (
            "No DOI candidate could be verified against metadata and the PDF title",
            "DOI candidates are ambiguous",
            "Multiple metadata DOI values",
            "Multiple Document Info DOI values",
            "Multiple PDF content DOI values have similar confidence",
        )
    )


def upload_identity_context(server: str, doi: str | None, source: str | None,
                            no_crossref: bool, server_version: str | None = None,
                            filename_doi: str | None = None) -> dict:
    return {
        "server": server.rstrip("/"), "client_version": __version__,
        "resolver_version": DOI_RESOLVER_VERSION, "doi": doi,
        "source": source, "no_crossref": no_crossref,
        "server_version": server_version,
        "filename_doi": filename_doi,
    }


def cached_upload_rejection(identity: dict | None, context: dict) -> dict | None:
    rejections = (identity or {}).get("upload_rejections")
    if not isinstance(rejections, dict):
        return None
    rejection = rejections.get(context["server"])
    if (isinstance(rejection, dict) and isinstance(rejection.get("context"), dict)
            and stable_context(rejection["context"]) == stable_context(context)
            and isinstance(rejection.get("error"), str)):
        return rejection
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
        "source": match.source,
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


def get_existing_pdfs_by_sha256(
    hashes: Iterable[str], progress_callback=None, *, fallback: bool = True,
    server_version_callback=None,
) -> dict[str, dict]:
    unique_hashes = sorted({sha256 for sha256 in hashes if sha256})
    if not unique_hashes:
        return {}
    existing: dict[str, dict] = {}
    chunk_size = 500
    try:
        for index in range(0, len(unique_hashes), chunk_size):
            response = query_existing_pdfs_batch(
                sha256=unique_hashes[index : index + chunk_size]
            )
            if server_version_callback and isinstance(response.get("server_version"), str):
                server_version_callback(response["server_version"])
            existing_payload = response.get("existing", {})
            if isinstance(existing_payload, dict):
                existing.update(existing_payload)
            if progress_callback:
                progress_callback(min(index + chunk_size, len(unique_hashes)))
        return existing
    except ApiError as exc:
        if not fallback or exc.status_code not in {404, 405, 501}:
            raise
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
            response = query_existing_pdfs_batch(doi=unique_dois[index : index + chunk_size])
            payload = response.get("existing_doi", {})
            if isinstance(payload, dict):
                existing.update(payload)
            if progress_callback:
                progress_callback(min(index + chunk_size, len(unique_dois)))
        return existing
    except ApiError as exc:
        if exc.status_code not in {404, 405, 501}:
            raise
        existing = {}
        for index, doi in enumerate(unique_dois, start=1):
            pdf = get_existing_pdf_by_doi(doi)
            if pdf:
                existing[doi] = pdf
            if progress_callback:
                progress_callback(index)
        return existing


def query_existing_pdfs_batch(**payload) -> dict:
    """Retry only the read-only duplicate-lookup endpoint on transient failures.

    All other API POST methods retain their own stricter retry policy. A
    failed preflight must not be mistaken for an empty database result.
    """
    for attempt in range(3):
        try:
            return api_post("/pdfs/exists", json=payload)
        except ApiError as exc:
            if exc.status_code not in {429, 502, 503, 504} or attempt == 2:
                raise
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
        sleep(0.5 * (2 ** attempt))
    raise RuntimeError("Duplicate lookup failed after retries")


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
    seen: set[str] = set()

    def add(value: str) -> None:
        try:
            doi = normalize_doi(value)
        except ValueError:
            return
        if doi not in seen:
            seen.add(doi)
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


def verified_first_page_doi_and_title(path: Path, doi: str, server_title: str | None) -> bool:
    """Conservatively confirm a content-derived DOI without sending PDF bytes.

    A DOI in the references of an unrelated PDF must never silently suppress
    its upload. Require exactly one first-page DOI and the full stored paper
    title in the *beginning* of the first page. Ambiguities fall back to the
    existing upload/server verification, without changing any stored object.
    """
    if not isinstance(server_title, str):
        return False

    def words(value: str) -> list[str]:
        return re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)

    title_words = words(server_title)
    if len(title_words) < 4 or len("".join(title_words)) < 20:
        return False
    try:
        with path.open("rb") as handle:
            reader = PdfReader(handle, strict=False)
            if reader.is_encrypted or not reader.pages:
                return False
            first_page = (reader.pages[0].extract_text() or "")[:12000]
        header = re.split(
            r"(?im)^\s*(?:references|bibliography)\b", first_page, maxsplit=1
        )[0]
        printed_dois = set(find_dois_in_text(header[:8000]))
        if printed_dois != {normalize_doi(doi)}:
            return False
        # Restrict title corroboration to the article heading, not a title in
        # body citations or a later reference list.
        return " ".join(title_words) in " ".join(words(header[:2800]))
    except Exception:
        # Fast-path failures must never turn a potentially valid upload into
        # a hard failure. The normal server-side verification remains available.
        return False


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
  nv login https://iiaide.com/nv --username example-user --password example-password
  nv login https://iiaide.com/nv
  nv login

Replace example-user and example-password with your own credentials.
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
    "tidy",
    epilog="""\b
Examples:
  nv tidy .
  nv tidy ./papers --apply

\b
Notes:
  Offline, recursive and read-only by default. Use --apply to remove only
  same-parent confirmed duplicates and move DOI-less PDFs into each parent's
  unidentifys/ folder. Distinct page content is retained and reported.
  No files are renamed, no server is queried and no PDF is uploaded.
""",
)
def tidy_command(
    directory: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, dir_okay=True, readable=True,
        help="Parent directory to organize recursively (default: current directory).",
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Remove confirmed duplicate PDFs and move unidentified PDFs.",
    ),
) -> None:
    """Organize PDFs offline, removing only confirmed same-directory duplicates."""
    from netvault.cli.tidy import apply_tidy, plan_tidy, preview_counts

    directory = directory.expanduser().resolve()
    try:
        rows = plan_tidy(
            directory,
            lambda paths: scan_upload_dois(paths, for_tidy=True),
            file_sha256,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(f"tidy failed: {exc}", highlight=False, markup=False)
        raise typer.Exit(1) from exc
    if not rows:
        console.print("No PDFs found outside collection folders.")
        return

    summary = preview_counts(rows)
    for row in rows:
        if row.action == "duplicate":
            console.print(
                f"duplicate: {row.path} (keep {row.keeper})",
                highlight=False, markup=False,
            )
        elif row.action == "unidentified":
            console.print(
                f"unidentified: {row.path} -> {row.path.parent / 'unidentifys'}",
                highlight=False, markup=False,
            )
        elif row.action in {"conflict", "error"}:
            console.print(
                f"{row.action}: {row.path}: {row.error}",
                highlight=False, markup=False,
            )
    console.print(
        f"Scanned {len(rows)} PDFs; confirmed duplicates {summary.get('duplicate', 0)}; "
        f"unidentified {summary.get('unidentified', 0)}; "
        f"conflicts {summary.get('conflict', 0)}; errors {summary.get('error', 0)}",
        highlight=False,
    )
    if not apply:
        console.print("Preview only. Run nv tidy . --apply to remove and move these files.")
        return

    try:
        report, counts = apply_tidy(directory, rows, file_sha256)
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(f"tidy failed: {exc}", highlight=False, markup=False)
        raise typer.Exit(1) from exc
    console.print(
        f"done: {counts.get('removed_duplicate', 0)} duplicates removed; "
        f"{counts.get('moved_unidentified', 0)} PDFs moved to unidentifys/; "
        f"{counts.get('conflict', 0)} retained conflicts; "
        f"{counts.get('error', 0)} errors",
        highlight=False,
    )
    console.print(f"History: {report}", highlight=False, markup=False)
    if counts.get("error"):
        raise typer.Exit(1)


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
  nv upload ~/Downloads/papers --retry-failed
  nv upload ~/Downloads/papers --refresh-doi
  nv upload ./paper.pdf --doi 10.1016/j.ijpe.2018.04.006
  nv upload ./new-paper.pdf --force
  nv upload ~/Downloads/papers --no-crossref
  nv upload ~/Downloads/papers --write-doi
  nv upload ~/Downloads/papers --write-doi --dry-run
  nv upload ~/Downloads/papers --index-file ./pdf-download-index.json
  nv upload ~/Downloads/papers --csv ./cli-downloads.csv
  nv upload ~/Downloads/papers --csv ./cli-downloads.csv --dry-run

\b
Notes:
  Directories are scanned recursively for .pdf/.PDF files.
  Ordinary uploads check DOI presence before full-file hashing or transfer.
  SHA-256 protects uploaded bytes and storage; it does not define paper identity.
  A unique PDF Document Info DOI is trusted in automatic mode.
  --doi and explicit --csv always override PDF DOI/title evidence.
  Conflicting automatic Document Info DOI values require review.
  Without a Document Info DOI, existing DOI/index/cache extraction rules apply.
  --write-doi independently verifies missing DOI against Crossref before local writeback.
  It backs up each changed original, writes incrementally, and uploads using the new hash.
  Unverified or unsafe files are left unchanged and are not uploaded in this mode.
  Existing unique Info DOI values are retained; this mode never corrects them automatically.
  A sibling cli-downloads.csv is also recognized; --csv works across subdirectories.
  Explicit --csv trusts matched DOI/PDF pairs and bypasses embedded DOI/content checks.
  Simple file,doi mappings match relative paths or unique filenames, ignoring byte changes.
  Old download CSV files may also match renamed PDFs by their legacy index.
  Automatic CSV discovery still requires server title verification.
  Metadata lookup continues; ordinary CSV uploads do not require metadata availability.
  Changes and failures append to .netvault/history.jsonl at the scan root.
  No new SQLite or per-command report is created; old records remain readable.
  --retry-failed selects failures/review from history; --report exports optional legacy JSON.
  Confirmed DOI identities are also cached; --refresh-doi rechecks rejected identities.
  --verbose shows individual errors and detailed timings.
  Recursive uploads skip unidentifys/ and duplicates/; pass an explicit PDF path to upload from them.
""",
)
def upload_command(
    paths: list[Path] = typer.Argument(..., exists=True, readable=True),
    doi: str | None = typer.Option(None, "--doi", help="Trust this DOI for one PDF; skip article identity/title validation."),
    no_crossref: bool = typer.Option(False, "--no-crossref", help="Skip online DOI/title verification and metadata lookup."),
    write_doi: bool = typer.Option(
        False, "--write-doi",
        help="Verify a missing DOI independently with Crossref, back up and write it locally before uploading.",
    ),
    jobs: int = typer.Option(3, "--jobs", min=1, max=3, help="Concurrent uploads (default: 3)."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace the existing PDF for its DOI and refresh all metadata from Crossref.",
    ),
    skip_existing_doi: bool = typer.Option(
        True,
        "--skip-existing-doi/--no-skip-existing-doi",
        help="Skip when a server DOI already has a trusted embedded/user-confirmed "
             "identity, or uniquely matching first-page DOI and article title. "
             "Keep the stored PDF unchanged.",
    ),
    refresh_doi: bool = typer.Option(
        False,
        "--refresh-doi",
        help="Re-run automatic DOI resolution and retry rejected identities; keep user-confirmed fallbacks.",
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
    csv_file: Path | None = typer.Option(
        None, "--csv", exists=True, file_okay=True, dir_okay=False, readable=True,
        help="Trust file,doi CSV mappings by relative path or unique filename (legacy index fallback).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview local DOI matching without login, uploads or writes.",
    ),
    no_index: bool = typer.Option(
        False,
        "--no-index",
        help="Ignore download index files and use cached or PDF-derived DOI identities.",
    ),
    report: Path | None = typer.Option(None, "--report", help="Optional legacy JSON export; normal operations use .netvault/history.jsonl."),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="Retry failed/review entries from the local history, with legacy report fallback."),
    verbose: bool = typer.Option(False, "--verbose", help="Show individual errors, cache statistics and upload timings."),
) -> None:
    if write_doi and (csv_file is not None or no_crossref or doi is not None):
        raise typer.BadParameter("--write-doi cannot be combined with --csv, --no-crossref or --doi")
    excluded_dir_names = DEFAULT_UPLOAD_EXCLUDED_DIRS | set(exclude_dir or [])
    pdfs = collect_pdf_paths(paths, excluded_dir_names=excluded_dir_names)
    history_root = run_root(paths)
    if retry_failed and not (write_doi and dry_run):
        retry_server = str(load_credentials().get("server_url") or DEFAULT_SERVER_URL).rstrip("/")
        with UploadState(root=history_root, record_events=False) as state:
            statuses = {path: state.path_status(path, retry_server) for path in pdfs}
        retry_paths = set()
        if report is not None and report.is_file() and any(value is None for value in statuses.values()):
            try:
                retry_paths = failed_paths(report)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise typer.BadParameter(f"Could not read upload report: {exc}") from exc
        elif not any(value is not None for value in statuses.values()):
            # Read the previous default JSON for compatibility, without creating
            # or extending one. New executions never write per-command reports.
            previous_report = history_root / "nv-upload-report.json"
            if previous_report.is_file():
                try:
                    retry_paths = failed_paths(previous_report)
                except (OSError, ValueError, KeyError, TypeError):
                    retry_paths = set()
            if not retry_paths:
                raise typer.BadParameter("No failed/review upload state in history or legacy reports")
        selected_statuses = RETRY_STATUSES
        pdfs = [path for path in pdfs if statuses[path] in selected_statuses
                or (statuses[path] is None and path.resolve() in retry_paths)]
    if not pdfs:
        console.print("No PDF files found.")
        return

    if doi and len(pdfs) != 1:
        raise typer.BadParameter("--doi can only be used when uploading one PDF file")
    if force and no_crossref and not (doi or csv_file):
        raise typer.BadParameter("--force with automatic DOI requires Crossref verification")
    if index_file is not None and no_index:
        raise typer.BadParameter("--index-file cannot be combined with --no-index")
    if csv_file is not None and (index_file is not None or no_index):
        raise typer.BadParameter("--csv cannot be combined with --index-file or --no-index")
    if csv_file is not None and doi:
        raise typer.BadParameter("--csv cannot be combined with --doi")
    try:
        configured_index_enabled, index_names = load_upload_index_settings()
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    index_warnings: list[str] = []

    def record_index_warning(message: str) -> None:
        index_warnings.append(message)
        if verbose or dry_run:
            console.print(f"warning: {message}", style="yellow", highlight=False, markup=False)

    trusted_csv = TrustedDoiCsv(csv_file) if csv_file is not None else None
    csv_roots = [
        selected.resolve() if selected.is_dir() else selected.resolve().parent
        for selected in paths
    ]
    if csv_file is not None:
        csv_roots.append(csv_file.resolve().parent)
    trusted_by_path: dict[Path, str] = {}
    trusted_csv_errors: dict[Path, str] = {}
    if trusted_csv is not None:
        for pdf_path in pdfs:
            try:
                mapped = trusted_csv.resolve(pdf_path, csv_roots, pdfs)
                if mapped:
                    trusted_by_path[pdf_path] = mapped
            except ValueError as exc:
                trusted_csv_errors[pdf_path] = str(exc)

    def trusted_match(pdf_path: Path) -> DownloadIndexMatch | None:
        mapped = trusted_by_path.get(pdf_path)
        if not mapped:
            return None
        record = DownloadIndexRecord(
            doi=mapped, filename=pdf_path.name,
            size=pdf_path.stat().st_size, last_modified=0,
            sha256="", downloaded_at="", source_url="",
            validation_status="valid", validation_checked_at="",
            validation_method="download-record-filename", validation_reason=None,
        )
        return DownloadIndexMatch(
            index_path=csv_file, index_version=0, index_updated_at="",
            record=record, renamed=False, source="csv-trusted",
        )

    index_resolver = DownloadIndexResolver(
        index_names,
        explicit_path=index_file,
        explicit_csv=csv_file,
        csv_roots=tuple(path.resolve() for path in paths if path.is_dir()),
        enabled=not no_index and (configured_index_enabled or index_file is not None or csv_file is not None),
        warning_callback=record_index_warning,
    )

    if dry_run:
        counts = {"sha256": 0, "filename-size": 0, "title": 0, "pdf-metadata": 0,
                  "json-index": 0, "pdf-extraction": 0, "needs-review": 0}
        # Ordinary preview and live upload use the same DOI probe. Keep the
        # legacy index/writeback preview for their explicit special modes.
        fast_preview = (
            dict(zip(pdfs, scan_upload_dois(pdfs)))
            if not (doi or csv_file is not None or index_file is not None or write_doi)
            else {}
        )
        console.print("Dry run: local DOI matching only; no login, server requests or files written.")
        if write_doi:
            console.print("DOI writeback preview: online verification was not performed; "
                          "local DOI candidates are not approved for writeback.")
            if retry_failed:
                console.print("Dry run previews all selected PDFs without updating retry state.")
        for pdf_path in pdfs:
            try:
                if pdf_path in fast_preview:
                    fast_identity, fast_error = fast_preview[pdf_path]
                    if fast_error is not None:
                        raise ValueError(fast_error)
                    if fast_identity is not None:
                        kind = ("pdf-metadata" if fast_identity.source == "pdf-metadata"
                                else "pdf-extraction")
                        counts[kind] += 1
                        console.print(
                            f"{pdf_path}: {fast_identity.doi} [{fast_identity.source}]",
                            highlight=False, markup=False,
                        )
                        continue
                if not has_pdf_header(pdf_path):
                    raise ValueError("invalid PDF header")
                sha256 = None if doi or pdf_path in trusted_by_path else file_sha256(pdf_path)
                embedded = None if csv_file is not None or doi else extract_document_info_evidence(pdf_path)
                match = trusted_match(pdf_path) if csv_file is not None else None
                if match is None and not doi and embedded is None:
                    match = index_resolver.resolve(
                        pdf_path, sha256 or file_sha256(pdf_path), pdf_path.stat().st_size
                    )
                if embedded is not None:
                    resolved = embedded.doi
                    kind = "pdf-metadata" if embedded.status == "ok" else "needs-review"
                    counts[kind] += 1
                elif doi:
                    resolved, kind = normalize_doi(doi), "user DOI"
                elif match:
                    resolved = match.record.doi
                    kind = match.record.validation_method if match.index_version == 0 else "json-index"
                    if match.index_version == 0:
                        method = kind.removeprefix("download-record-")
                        counts[method] = counts.get(method, 0) + 1
                    else:
                        counts["json-index"] += 1
                elif csv_file is not None:
                    raise ValueError("No unambiguous file/DOI match in the specified CSV")
                else:
                    evidence = extract_doi_evidence(pdf_path)
                    resolved = evidence.doi if evidence.status == "ok" else None
                    kind = "pdf-extraction" if resolved else "needs-review"
                    counts[kind] += 1
                renamed = " (renamed)" if match and match.renamed else ""
                if csv_file is not None and match:
                    kind += "; CSV trusted, PDF content verification skipped"
                console.print(f"{pdf_path}: {resolved or 'unresolved'} [{kind}]{renamed}",
                              highlight=False, markup=False)
            except (OSError, ValueError, RuntimeError) as exc:
                counts["needs-review"] += 1
                console.print(f"{pdf_path}: needs review ({exc})", highlight=False)
        console.print("Summary: " + ", ".join(f"{key} {value}" for key, value in counts.items()))
        return

    ensure_logged_in()
    total_pdf_count = len(pdfs)
    upload_report = UploadReport(
        report, pdfs, history=History(history_root, "upload"), warnings=index_warnings,
    )
    if write_doi:
        for pdf_path in pdfs:
            upload_report.record_metadata_writeback(pdf_path, {
                "status": "pending", "doi": None, "original_sha256": None,
                "sha256": None, "backup": None,
            })
        upload_report.checkpoint(force=True)
    uploaded = 0
    replaced = 0
    skipped = 0
    skipped_by_sha = 0
    skipped_by_doi = 0
    skipped_by_server = 0
    needs_review = 0
    review_skipped = 0
    failed: list[tuple[Path, str]] = []
    latest_pdf: dict | None = None
    hash_cache = load_hash_cache()
    identity_cache = load_identity_cache()
    upload_server = str(load_credentials().get("server_url") or DEFAULT_SERVER_URL).rstrip("/")
    upload_report.server = upload_server
    upload_server_version = None
    upload_contexts: dict[Path, dict] = {}
    cache_changed = False
    identity_cache_pending = 0
    doi_cache_hits = 0
    doi_scans = 0
    download_index_hits = 0
    download_index_renames = 0
    hashes_by_path: dict[Path, str] = {}
    csv_matches_by_path: dict[Path, DownloadIndexMatch] = {}
    existing_by_sha: dict[str, dict] = {}
    unique_pdf_paths: list[Path] = []
    local_duplicate_paths = 0
    stats = UploadStats()
    preparation_seconds = 0.0
    prepared_count = 0
    preparing_total = 0
    doi_precheck_seconds = 0.0
    doi_precheck_batches = 0
    doi_precheck_candidates = 0
    hash_seconds = 0.0
    sha_lookup_seconds = 0.0
    pdf_paths_by_sha: dict[str, list[Path]] = {}
    started = monotonic()

    progress_columns = [
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed:.0f}/{task.total:.0f}"),
        TextColumn("{task.fields[outcomes]}"),
        TimeElapsedColumn(),
    ]
    if verbose:
        progress_columns.append(UploadRateColumn(stats))
    identity_writer = CacheWriter(lambda: save_identity_cache(identity_cache))
    with UploadState(root=history_root) as upload_state, upload_report, identity_writer, Progress(
        *progress_columns,
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("DOI preflight", total=len(pdfs), outcomes="")

        def state_paths(path, sha):
            return {sibling.resolve().parent: sibling
                    for sibling in pdf_paths_by_sha.get(sha, [path])}.values()

        def record_failure(path, error, *, review=None):
            nonlocal needs_review
            rejection = is_identity_rejection(error) if review is None else review
            if rejection:
                needs_review += 1
            failed.append((path, str(error)))
            upload_report.record(path, "needs-review" if rejection else "failed",
                                 sha256=hashes_by_path.get(path), error=str(error))
            sha = hashes_by_path.get(path)
            if sha is None and write_doi:
                metadata_result = upload_report.rows[str(path.resolve())].get("metadata_writeback", {})
                sha = metadata_result.get("sha256") or metadata_result.get("original_sha256")
                if sha:
                    # A writeback failure supersedes an older ordinary-upload
                    # success for these bytes, so --retry-failed can select it.
                    upload_state.record(path, sha, upload_server,
                                        "needs-review" if rejection else "failed",
                                        error=str(error), attempted=True)
            elif sha:
                for sibling in state_paths(path, sha):
                    upload_state.record(sibling, sha, upload_server,
                                        "needs-review" if rejection else "failed",
                                        context=upload_contexts.get(path), error=str(error), attempted=True)
            if verbose:
                console.print(f"failed: {path.name}: {error}", highlight=False, markup=False)

        def update_counts():
            counts = [f"new {uploaded}"]
            if preparing_total:
                counts.insert(0, f"prepared {prepared_count}/{preparing_total}")
            if skipped:
                counts.append(f"already stored {skipped}")
            if needs_review or review_skipped:
                counts.append(f"review {needs_review + review_skipped}")
            if len(failed) > needs_review:
                counts.append(f"failed {len(failed) - needs_review}")
            if replaced:
                counts.append(f"replaced {replaced}")
            progress.update(task, outcomes=" | ".join(counts))

        def remember_server_version(version):
            nonlocal upload_server_version
            upload_server_version = version

        # Determine DOI and query the server before the first full-file hash.
        # Unknown/ambiguous identities keep the existing full resolver.
        fast_identities: dict[Path, FastIdentity] = {}
        fast_skips: list[tuple[Path, dict]] = []
        fast_reviews: list[tuple[Path, str]] = []
        if (skip_existing_doi and not (force or write_doi or retry_failed or
                refresh_doi or no_crossref or index_file is not None)):
            found: dict[Path, FastIdentity] = {}
            fallbacks: set[Path] = set()
            if doi or csv_file is not None:
                # Explicit identities are cheap and require no PDF text scan.
                for pdf_path in pdfs:
                    try:
                        if pdf_path in trusted_csv_errors:
                            raise ValueError(trusted_csv_errors[pdf_path])
                        if doi:
                            identity = FastIdentity(normalize_doi(doi), "user")
                        else:
                            mapped = trusted_by_path.get(pdf_path)
                            if not mapped:
                                if trusted_csv is not None and trusted_csv.has_legacy_columns:
                                    fallbacks.add(pdf_path)
                                    continue
                                raise ValueError("No file/DOI entry in the specified CSV")
                            identity = FastIdentity(mapped, "csv-trusted")
                        found[pdf_path] = identity
                    except (OSError, ValueError) as exc:
                        fast_reviews.append((pdf_path, str(exc)))
                    finally:
                        progress.advance(task)
            else:
                # pypdf and pdftotext are independent for each PDF; bounded
                # threading overlaps their I/O and process startup costs.
                workers = doi_scan_workers(len(pdfs))
                if verbose:
                    console.print(
                        f"DOI scan: {len(pdfs)} PDFs, {workers} auto-selected workers",
                        highlight=False,
                    )
                for index, (identity, error) in iter_scan_upload_dois(pdfs):
                    pdf_path = pdfs[index]
                    progress.advance(task)
                    if error is not None:
                        fast_reviews.append((pdf_path, error))
                    elif identity is None:
                        fallbacks.add(pdf_path)
                    else:
                        found[pdf_path] = identity

            # A lookup outage must never be treated as a missing DOI.
            # Concurrent probes finish out of order; retain the original
            # selection priority when several PDFs resolve to the same DOI.
            found = {path: found[path] for path in pdfs if path in found}
            unique_dois = sorted({identity.doi for identity in found.values()})
            by_doi = {}
            if unique_dois:
                progress.reset(
                    task, total=len(unique_dois), completed=0,
                    description="Checking stored DOIs on server",
                )
                # Same 500-DOI API batches as before, but visible to users.
                for offset in range(0, len(unique_dois), 500):
                    chunk = unique_dois[offset : offset + 500]
                    by_doi.update(get_existing_pdfs_by_doi(chunk))
                    progress.advance(task, len(chunk))
            progress.reset(
                task, total=len(found), completed=0,
                description="Classifying DOI matches",
            )
            new_automatic: dict[Path, FastIdentity] = {}
            for pdf_path, identity in found.items():
                existing_pdf = by_doi.get(identity.doi)
                if existing_pdf is not None:
                    if identity.source in {"user", "csv-trusted"} or confirms_existing(
                        identity, existing_pdf
                    ):
                        fast_skips.append((pdf_path, existing_pdf))
                    else:
                        fallbacks.add(pdf_path)
                elif identity.source in {"user", "csv-trusted", "pdf-metadata"}:
                    # An unambiguous Document Info DOI is authoritative; a
                    # provider outage must not block this existing workflow.
                    fast_identities[pdf_path] = identity
                else:
                    new_automatic[pdf_path] = identity
                progress.advance(task)
            if new_automatic:
                progress.reset(
                    task, total=len({identity.doi for identity in new_automatic.values()}),
                    completed=0, description="Looking up DOI titles",
                )
                last_title_stage = "Looking up DOI titles"

                def title_progress(stage: str, completed: int, total: int) -> None:
                    nonlocal last_title_stage
                    if stage != last_title_stage:
                        progress.reset(task, total=total, completed=0, description=stage)
                        last_title_stage = stage
                    progress.update(task, completed=completed)

                accepted = check_new_titles(
                    new_automatic, progress_callback=title_progress,
                )
            else:
                accepted = {}
            for pdf_path, identity in new_automatic.items():
                if accepted[pdf_path]:
                    fast_identities[pdf_path] = identity
                else:
                    fast_reviews.append((
                        pdf_path,
                        "CSL title unavailable or absent from PDF pages 1-2; "
                        "confirm with --doi DOI or --csv file.csv",
                    ))
            # One upload task per DOI per invocation. Identical papers with
            # different compressed PDF hashes must not be sent repeatedly.
            progress.reset(
                task,
                total=len(fast_identities) + len(fast_skips) + len(fast_reviews),
                completed=0, description="Recording DOI decisions",
            )
            seen_dois: set[str] = set()
            for pdf_path, identity in list(fast_identities.items()):
                if identity.doi in seen_dois:
                    upload_report.record(pdf_path, "local-duplicate")
                    local_duplicate_paths += 1
                    del fast_identities[pdf_path]
                else:
                    seen_dois.add(identity.doi)
                progress.advance(task)
            for pdf_path, existing_pdf in fast_skips:
                skipped += 1
                skipped_by_doi += 1
                latest_pdf = existing_pdf
                upload_report.record(pdf_path, "existing", pdf=existing_pdf)
                progress.advance(task)
            for pdf_path, error in fast_reviews:
                record_failure(pdf_path, error, review=True)
                progress.advance(task)
            pdfs = [
                pdf_path for pdf_path in pdfs
                if pdf_path in fast_identities or pdf_path in fallbacks
            ]
            if verbose:
                console.print(
                    f"DOI preflight: {len(fast_skips)} existing, "
                    f"{len(fast_identities)} selected, {len(fast_reviews)} review, "
                    f"{len(fallbacks)} legacy fallback",
                    highlight=False,
                )
        progress.reset(task, total=len(pdfs), completed=0, description="Hashing upload candidates")

        hash_started = monotonic()
        for pdf_path in pdfs:
            writeback = None
            if write_doi:
                writeback = {"status": "pending", "doi": None, "original_sha256": None,
                             "sha256": None, "backup": None}
                upload_report.record_metadata_writeback(pdf_path, writeback)
            try:
                if writeback is not None:
                    original_sha256 = file_sha256(pdf_path)
                    writeback.update(original_sha256=original_sha256, sha256=original_sha256)
                    upload_report.record_metadata_writeback(pdf_path, writeback)
                if not has_pdf_header(pdf_path):
                    if writeback is not None:
                        writeback.update(status="blocked", reason="invalid PDF header")
                        upload_report.record_metadata_writeback(pdf_path, writeback)
                    record_failure(pdf_path, "invalid PDF file: expected %PDF- header")
                    continue
                if writeback is not None:
                    metadata = read_pdf_metadata(pdf_path)
                    for key, value in metadata["document_info"].items():
                        if str(key).casefold() == "/doi" and str(value).strip():
                            try:
                                normalize_doi(str(value))
                            except ValueError as exc:
                                raise ValueError("Nonempty invalid Document Info DOI requires manual review") from exc
                    if metadata["status"] == "conflict":
                        raise ValueError("Multiple Document Info DOI values require manual review")
                    if metadata["status"] == "ok" and metadata.get("doi"):
                        writeback.update(status="unchanged", doi=metadata["doi"],
                                         original_sha256=original_sha256,
                                         sha256=original_sha256,
                                         reason="Existing unique Document Info DOI retained; no automatic correction")
                    elif metadata["status"] == "no-doi":
                        check_pdf_writeback_safety(pdf_path)
                        verification = verify_pdf_doi(pdf_path, original_sha256)
                        writeback.update(verification=verification, doi=verification.get("doi"))
                        if verification.get("decision") != "verified":
                            reason = str(verification.get("reason") or "DOI could not be independently verified")
                            writeback.update(status=str(verification.get("decision") or "unverified"),
                                             reason=reason, sha256=original_sha256)
                            upload_report.record_metadata_writeback(pdf_path, writeback)
                            record_failure(pdf_path, reason,
                                           review=verification.get("decision") != "unavailable")
                            continue
                        result = write_verified_pdf_doi(pdf_path, verification)
                        writeback.update(result)
                        writeback["status"] = "written" if result["changed"] else "unchanged"
                    else:
                        raise ValueError("Unsupported PDF metadata status; manual review required")
                    # Both caches must observe the final bytes. Force the directory
                    # state to recompute even if a previous signature still matches.
                    hash_cache.pop(file_cache_key(pdf_path), None)
                    sha256, from_cache = upload_state.hash_file(
                        pdf_path, lambda path: cached_file_sha256(path, hash_cache)[0], force=True)
                    if writeback.get("sha256") and writeback["sha256"] != sha256:
                        raise RuntimeError("PDF changed after DOI writeback; file was not uploaded")
                    writeback["sha256"] = sha256
                    upload_report.record_metadata_writeback(pdf_path, writeback)
                else:
                    sha256, from_cache = upload_state.hash_file(
                        pdf_path, lambda path: cached_file_sha256(path, hash_cache)[0])
                cache_changed = cache_changed or not from_cache
                if csv_file is not None:
                    match = trusted_match(pdf_path) or index_resolver.resolve(
                        pdf_path, sha256, pdf_path.stat().st_size
                    )
                    if match is None:
                        raise ValueError("No unambiguous file/DOI match in the specified CSV")
                    csv_matches_by_path[pdf_path] = match
                hashes_by_path[pdf_path] = sha256
                upload_state.pending(pdf_path, sha256, upload_server)
            except (OSError, ValueError, RuntimeError, PyPdfError, requests.RequestException) as exc:
                if writeback is not None:
                    if getattr(exc, "pdf_writeback_backup", None):
                        writeback["backup"] = exc.pdf_writeback_backup
                    review = isinstance(exc, ValueError) and writeback.get("verification", {}).get("decision") != "verified"
                    writeback.update(status="blocked" if review else "failed", reason=str(exc))
                    upload_report.record_metadata_writeback(pdf_path, writeback)
                    record_failure(pdf_path, exc, review=review)
                else:
                    record_failure(pdf_path, exc)
            finally:
                progress.advance(task, 1)

        hash_seconds = monotonic() - hash_started
        if cache_changed:
            save_hash_cache(hash_cache)

        pdf_paths_by_sha: dict[str, list[Path]] = {}
        for pdf_path, sha256 in hashes_by_path.items():
            pdf_paths_by_sha.setdefault(sha256, []).append(pdf_path)
        if csv_file is not None:
            for sha256, same_files in list(pdf_paths_by_sha.items()):
                if len({csv_matches_by_path[path].record.doi for path in same_files}) > 1:
                    for path in same_files:
                        record_failure(path, "Specified CSV assigns different DOIs to identical PDFs")
                    del pdf_paths_by_sha[sha256]
        unique_pdf_paths = [paths[0] for paths in pdf_paths_by_sha.values()]
        local_duplicate_paths += sum(len(paths) - 1 for paths in pdf_paths_by_sha.values())
        for sha, paths_for_hash in pdf_paths_by_sha.items():
            for duplicate_path in paths_for_hash[1:]:
                upload_report.record(duplicate_path, "local-duplicate", sha256=sha)

        sha_lookup_started = monotonic()
        # The fast DOI path never uses SHA-256 to decide whether an article
        # already exists. The legacy index/fallback path retains its original
        # compatibility lookup and integrity conflict handling.
        legacy_hashes = {
            sha for path, sha in hashes_by_path.items()
            if path not in fast_identities
        }
        if legacy_hashes:
            progress.reset(
                task, total=len(legacy_hashes), completed=0,
                description="Checking legacy PDF hashes",
            )
        existing_by_sha = get_existing_pdfs_by_sha256(
            legacy_hashes,
            progress_callback=lambda completed: progress.update(task, completed=completed),
            server_version_callback=remember_server_version,
        )
        sha_lookup_seconds = monotonic() - sha_lookup_started
        progress.reset(
            task, total=len(unique_pdf_paths), completed=0,
            description="Preparing unique PDFs",
        )

        new_pdfs: list[Path] = []
        embedded_by_path: dict[Path, DoiEvidence | None] = {}
        for pdf_path in unique_pdf_paths:
            sha256 = hashes_by_path[pdf_path]
            existing_pdf = existing_by_sha.get(sha256)
            if existing_pdf and not force and csv_file is None:
                try:
                    # The server has already identified these exact PDF bytes.
                    # Do not open and parse a potentially large PDF just to
                    # discover a DOI that the existing SHA lookup already knows.
                    asserted_doi = normalize_doi(doi) if doi else None
                    cached = cached_identity(identity_cache, sha256)
                    if not asserted_doi and cached and cached.get("source") == "user":
                        asserted_doi = normalize_doi(cached["doi"])
                    if index_file is not None:
                        match = index_resolver.resolve(pdf_path, sha256, pdf_path.stat().st_size)
                        if match is not None:
                            if asserted_doi and asserted_doi != match.record.doi:
                                raise ApiError(409, "User-confirmed DOI conflicts with download index DOI")
                            asserted_doi = match.record.doi
                    if asserted_doi and asserted_doi != existing_pdf["doi"]:
                        raise ApiError(409, f"This PDF is already linked to DOI {existing_pdf['doi']}. "
                            "Ask an administrator to correct a wrong DOI identity.")
                except (ValueError, RuntimeError, OSError) as exc:
                    record_failure(pdf_path, exc)
                    progress.advance(task)
                    continue
                latest_pdf = existing_pdf
                skipped += 1
                skipped_by_sha += 1
                upload_report.record(pdf_path, "existing", sha256=sha256, pdf=existing_pdf)
                for sibling in state_paths(pdf_path, sha256):
                    upload_state.record(sibling, sha256, upload_server, "existing", pdf=existing_pdf)
                progress.advance(task)
            else:
                embedded = None if csv_file is not None or doi else extract_document_info_evidence(pdf_path)
                embedded_by_path[pdf_path] = embedded
                if embedded is not None and embedded.status == "conflict":
                    record_failure(
                        pdf_path, ApiError(422, embedded.reason or "Multiple Document Info DOI values")
                    )
                    progress.advance(task)
                    continue
                new_pdfs.append(pdf_path)
                progress.advance(task)

        initial_skips = []
        if skipped:
            initial_skips.append(f"{skipped} existing")
        if local_duplicate_paths:
            initial_skips.append(f"{local_duplicate_paths} local duplicates")
        if initial_skips:
            console.print("Skipped: " + ", ".join(initial_skips))
        preparing_total = len(new_pdfs)
        update_counts()
        progress.reset(task, total=len(new_pdfs), completed=0, description="Checking / uploading PDFs")

        def record_outcomes(outcomes) -> None:
            nonlocal latest_pdf, replaced, skipped, uploaded, skipped_by_server
            for outcome in outcomes:
                if outcome.error is not None:
                    if is_identity_rejection(outcome.error):
                        context = upload_contexts[outcome.path]
                        entry = identity_cache.setdefault(hashes_by_path[outcome.path], {})
                        if not isinstance(entry.get("upload_rejections"), dict):
                            entry["upload_rejections"] = {}
                        entry.setdefault("upload_rejections", {})[upload_server] = {
                            "context": context, "error": str(outcome.error),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
                        identity_writer.checkpoint()
                    record_failure(outcome.path, outcome.error)
                else:
                    result = outcome.result
                    latest_pdf = result["pdf"]
                    if result.get("replaced"):
                        replaced += 1
                    elif result["deduplicated"]:
                        skipped += 1
                        skipped_by_server += 1
                    else:
                        uploaded += 1
                    status = "replaced" if result.get("replaced") else "existing" if result["deduplicated"] else "uploaded"
                    upload_report.record(outcome.path, status, sha256=hashes_by_path[outcome.path], pdf=latest_pdf)
                    for sibling in state_paths(outcome.path, hashes_by_path[outcome.path]):
                        upload_state.record(sibling, hashes_by_path[outcome.path], upload_server,
                                            status, context=upload_contexts.get(outcome.path),
                                            pdf=latest_pdf, attempted=True)
                    entry = identity_cache.get(hashes_by_path[outcome.path], {})
                    rejections = entry.get("upload_rejections") if isinstance(entry, dict) else None
                    if isinstance(rejections, dict) and rejections.pop(upload_server, None) is not None:
                        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
                        identity_writer.checkpoint()
                progress.advance(task, 1)
                update_counts()

        # Preserve input order for force replacements. Only the coordinator
        # touches caches. DOI prechecks operate in server-sized batches instead
        # of a request per 12 PDFs; force/writeback preserves streaming overlap.
        pending_paths = new_pdfs
        prepare_batch_size = (
            UPLOAD_PREPARE_BATCH_SIZE
            if skip_existing_doi and not (force or write_doi or csv_file is not None)
            else min(UPLOAD_PREPARE_BATCH_SIZE, UPLOAD_STREAM_BATCH_SIZE)
        )
        with UploadPipeline(upload_pdf, 1 if force else jobs, stats) as pipeline:
            for offset in range(0, len(pending_paths), prepare_batch_size):
                preparation_batch = pending_paths[offset : offset + prepare_batch_size]
                prepare_started = monotonic()
                dois_by_path: dict[Path, str | None] = {}
                doi_sources_by_path: dict[Path, str] = {}
                upload_candidates: list[Path] = []
                for pdf_path in preparation_batch:
                    try:
                        sha256 = hashes_by_path[pdf_path]
                        embedded = embedded_by_path[pdf_path]
                        identity = cached_identity(identity_cache, sha256)
                        if not identity:
                            local_identity = upload_state.identity(pdf_path, sha256)
                            identity = cached_identity({sha256: local_identity}, sha256)
                        if (identity and identity.get("source") != "user" and (
                            refresh_doi or retry_failed
                            or (identity.get("status") == "conflict" and not no_crossref)
                        )):
                            identity = None

                        index_match = None
                        if csv_file is not None:
                            index_match = csv_matches_by_path[pdf_path]
                        elif not doi and embedded is None:
                            index_match = index_resolver.resolve(
                                pdf_path,
                                sha256,
                                pdf_path.stat().st_size,
                            )

                        if embedded is not None:
                            identity = identity_from_evidence(embedded)
                            store_identity(identity_cache, sha256, identity)
                            identity_cache_pending += 1
                        elif doi:
                            identity = manual_identity(doi)
                            # Keep the existing user-confirmation cache schema.
                            # "manual-trusted" is a transport assertion only.
                            store_identity(identity_cache, sha256, identity)
                            identity_cache_pending += 1
                        elif csv_file is not None:
                            if identity and identity.get("source") == "user" and normalize_doi(identity["doi"]) != index_match.record.doi:
                                raise RuntimeError("User-confirmed DOI conflicts with specified CSV DOI " + index_match.record.doi)
                            identity = download_index_identity(index_match)
                            download_index_hits += 1
                            if index_match.renamed:
                                download_index_renames += 1
                        elif identity and identity.get("source") == "user":
                            if index_match and normalize_doi(identity["doi"]) != index_match.record.doi:
                                raise RuntimeError(
                                    "User-confirmed DOI conflicts with download index DOI "
                                    f"{index_match.record.doi}"
                                )
                            doi_cache_hits += 1
                        elif index_match:
                            identity = download_index_identity(index_match)
                            store_identity(identity_cache, sha256, identity)
                            identity_cache_pending += 1
                            download_index_hits += 1
                            if index_match.renamed:
                                download_index_renames += 1
                        elif pdf_path in fast_identities:
                            fast = fast_identities[pdf_path]
                            identity = identity_from_evidence(DoiEvidence(
                                "ok", fast.doi, fast.source, []
                            ))
                            store_identity(identity_cache, sha256, identity)
                            identity_cache_pending += 1
                        elif identity:
                            doi_cache_hits += 1
                        else:
                            evidence = extract_doi_evidence(pdf_path)
                            if (evidence.status == "conflict" and evidence.candidates and not no_crossref
                                    and not any(item.detail.startswith("document-info:")
                                                for item in evidence.candidates)):
                                # Automatic identities are only upload hints. The server owns
                                # Crossref verification and can safely fall back to another
                                # candidate from the PDF.
                                best = max(
                                    evidence.candidates,
                                    key=lambda candidate: (candidate.score, candidate.source, candidate.doi),
                                )
                                evidence = replace(
                                    evidence,
                                    status="ok",
                                    doi=best.doi,
                                    source=best.source,
                                )
                            identity = identity_from_evidence(evidence)
                            store_identity(identity_cache, sha256, identity)
                            identity_cache_pending += 1
                            doi_scans += 1

                        extracted = identity.get("doi")
                        if identity.get("status") == "no-doi":
                            # The server has its own parsers. A failed local scan must not
                            # prevent it from examining the actual document.
                            dois_by_path[pdf_path] = None
                            doi_sources_by_path[pdf_path] = "pdf-content"
                        elif identity.get("status") not in {"ok", "confirmed"} or not isinstance(extracted, str):
                            reason = identity.get("reason") or "No DOI found"
                            record_failure(pdf_path, f"{reason}. Confirm with: nv doi {pdf_path} --set DOI")
                            progress.advance(task, 1)
                            continue
                        else:
                            dois_by_path[pdf_path] = extracted
                            # The running NetVault server already supports
                            # csv-trusted as an explicit user assertion. Reuse
                            # that wire value for --doi until newer servers
                            # expose the separate manual-trusted identity.
                            doi_sources_by_path[pdf_path] = (
                                "csv-trusted" if doi else str(identity.get("source") or "explicit")
                            )
                        filename_hint = find_filename_doi_from_name(pdf_path.name)
                        context = upload_identity_context(
                            upload_server, dois_by_path[pdf_path],
                            # Preserve distinct local identities for manual DOI
                            # and CSV even while the older server accepts the
                            # same csv-trusted transport assertion for both.
                            ("user" if doi else doi_sources_by_path[pdf_path])
                            if dois_by_path[pdf_path] else None,
                            no_crossref,
                            upload_server_version,
                            filename_hint.doi if filename_hint else None,
                        )
                        upload_contexts[pdf_path] = context
                        for sibling in state_paths(pdf_path, sha256):
                            upload_state.save_identity(sibling, sha256, identity)
                        previous = upload_state.get(pdf_path, sha256, upload_server)
                        cached_rejection = cached_upload_rejection(identity_cache.get(sha256), context)
                        if cached_rejection and previous and previous["status"] == "pending" and not previous["attempts"]:
                            upload_state.record(pdf_path, sha256, upload_server, "needs-review",
                                                context=cached_rejection["context"], error=cached_rejection["error"],
                                                attempted=True)
                        rejection = upload_state.rejection(pdf_path, sha256, upload_server, context)
                        if rejection and not (refresh_doi or retry_failed or force):
                            review_skipped += 1
                            upload_report.record(pdf_path, "review-skipped", sha256=sha256,
                                                 error=rejection["error"])
                            progress.advance(task, 1)
                            continue
                        upload_candidates.append(pdf_path)
                    except (RuntimeError, OSError, ValueError) as exc:
                        record_failure(pdf_path, exc)
                        progress.advance(task, 1)
                    finally:
                        prepared_count += 1
                        if identity_cache_pending >= 25:
                            identity_writer.checkpoint()
                            identity_cache_pending = 0
                        update_counts()
                # Do not send full PDF bytes for an existing DOI when evidence
                # is authoritative, or the first-page DOI AND title independently
                # match the stored document. Download-index/automatic CSV values
                # are hints, not authoritative identities by themselves.
                # This never registers an alias or replaces the canonical PDF.
                # Preserve CSV-trusted uploads and --write-doi's explicit flow.
                ready_paths = upload_candidates
                preparation_seconds += monotonic() - prepare_started
                if skip_existing_doi and not (force or write_doi or csv_file is not None):
                    eligible_sources = {
                        "pdf-metadata", "pdf-content", "explicit", "user",
                        "download-index",
                    }
                    eligible_paths = [
                        path for path in ready_paths
                        if dois_by_path.get(path)
                        and path not in fast_identities
                        and doi_sources_by_path.get(path) in eligible_sources
                    ]
                    if eligible_paths:
                        precheck_started = monotonic()
                        doi_precheck_batches += 1
                        doi_precheck_candidates += len(eligible_paths)
                        by_doi = get_existing_pdfs_by_doi(
                            dois_by_path[path] for path in eligible_paths
                        )
                        retained_paths = []
                        for pdf_path in ready_paths:
                            pdf = (
                                by_doi.get(dois_by_path[pdf_path])
                                if pdf_path in eligible_paths else None
                            )
                            if pdf is not None:
                                embedded = embedded_by_path.get(pdf_path)
                                source = doi_sources_by_path.get(pdf_path)
                                trusted = source in {"explicit", "user"} or (
                                    source == "pdf-metadata" and embedded is not None
                                    and embedded.status == "ok"
                                    and embedded.doi == dois_by_path[pdf_path]
                                )
                                if not trusted and not verified_first_page_doi_and_title(
                                    pdf_path, dois_by_path[pdf_path], pdf.get("title")
                                ):
                                    # An annotation or first-page reference is
                                    # not authoritative. Let the server verify
                                    # the candidate DOI and entire PDF.
                                    pdf = None
                            if pdf is None:
                                retained_paths.append(pdf_path)
                                continue
                            skipped += 1
                            skipped_by_doi += 1
                            latest_pdf = pdf
                            upload_report.record(
                                pdf_path, "existing", sha256=hashes_by_path[pdf_path], pdf=pdf
                            )
                            for sibling in state_paths(pdf_path, hashes_by_path[pdf_path]):
                                upload_state.record(
                                    sibling, hashes_by_path[pdf_path], upload_server,
                                    "existing", context=upload_contexts.get(pdf_path), pdf=pdf
                                )
                            progress.advance(task, 1)
                            update_counts()
                        ready_paths = retained_paths
                        doi_precheck_seconds += monotonic() - precheck_started

                record_outcomes(pipeline.collect())
                for pdf_path in ready_paths:
                    upload_report.record_pending(
                        pdf_path, doi=dois_by_path.get(pdf_path),
                        sha256=hashes_by_path[pdf_path],
                    )
                    record_outcomes(pipeline.submit(
                        pdf_path,
                        doi=dois_by_path[pdf_path],
                        doi_source=doi_sources_by_path[pdf_path] if dois_by_path[pdf_path] else None,
                        no_crossref=no_crossref,
                        force=force,
                        sha256=hashes_by_path[pdf_path],
                    ))
            record_outcomes(pipeline.drain())
        if identity_cache_pending:
            identity_writer.checkpoint()
        upload_report.state_files = sorted(upload_state.paths)


    upload_report.checkpoint(force=True, finished=True)
    # Upload state is a small replayable queue, not an ever-growing list of
    # successful transfers. Compact once per finished run, never per PDF.
    try:
        upload_report.history.compact_uploads()
    except OSError as exc:
        console.print(f"warning: upload history compaction failed: {exc}", style="yellow")
    parts = [f"done: {total_pdf_count}/{total_pdf_count} processed in {monotonic() - started:.1f}s",
             f"uploaded: {uploaded}; already stored: {skipped} skipped"]
    if local_duplicate_paths:
        parts.append(f"local duplicate paths: {local_duplicate_paths}")
    if needs_review:
        parts.append(f"needs review: {needs_review}")
    if review_skipped:
        parts.append(f"previously rejected: {review_skipped} skipped (needs review)")
    if len(failed) > needs_review:
        parts.append(f"failed: {len(failed) - needs_review}")
    if index_warnings:
        parts.append(f"index warnings: {len(index_warnings)} (see history)")
    if replaced:
        parts.append(f"{replaced} replaced")
    parts.append(f"history: {upload_report.history.path}")
    if verbose:
        parts.extend([f"found paths: {len(pdfs)}", f"unique PDFs: {len(unique_pdf_paths)}",
                      f"hashing/local checks: {hash_seconds:.1f}s; "
                      f"SHA lookup: {sha_lookup_seconds:.1f}s",
                      f"DOI preparation: {preparation_seconds:.1f}s",
                      f"DOI precheck: {doi_precheck_seconds:.1f}s; "
                      f"checked {doi_precheck_candidates} candidates in "
                      f"{doi_precheck_batches} batches",
                      f"quick skips: SHA {skipped_by_sha}, DOI {skipped_by_doi}; "
                      f"server deduplication {skipped_by_server}",
                      f"trusted DOI skips: {skipped_by_doi}"])
    if verbose and stats.started is not None:
        parts.append(stats.display())
    if verbose and stats.measured:
        parts.append(
            f"average request timing: send {stats.transfer_seconds / stats.measured:.2f}s; "
            f"response wait {stats.response_seconds / stats.measured:.2f}s "
            "(includes network and server processing)"
        )
    if verbose and stats.server_timings:
        parts.append("average server timing: " + "; ".join(
            f"{name} {seconds / count:.2f}s"
            for name, (seconds, count) in stats.server_timings.items()
        ))
    if verbose and doi_cache_hits:
        parts.append(f"{doi_cache_hits} DOI cache hits")
    if verbose and doi_scans:
        parts.append(f"{doi_scans} DOI scans")
    if verbose and download_index_hits:
        parts.append(f"{download_index_hits} download index hits")
    if verbose and download_index_renames:
        parts.append(f"{download_index_renames} matched after rename")
    if verbose and latest_pdf:
        title = latest_pdf.get("title") or latest_pdf["original_name"]
        parts.append(f"latest: {latest_pdf['doi']}  {title}")
    console.print("\n".join(parts))
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
    doi: str | None = typer.Option(None, "--doi", help="Fallback DOI to validate/display when Document Info has no DOI."),
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
            with UploadState() as state:
                state.save_identity(path, sha256, identity)
            console.print(
                f"saved: {path} -> {identity['doi']} "
                f"(source=user, sha256={sha256})"
            )
            return
        if remove:
            removed = identities.pop(sha256, None)
            with UploadState() as state:
                removed_local = state.remove_identity(path, sha256)
            if removed is not None or removed_local:
                save_identity_cache(identities, removed=(sha256,))
                console.print(f"removed: {path} (sha256={sha256})")
            else:
                console.print(f"no cached identity: {path} (sha256={sha256})")
            return

        with UploadState() as state:
            identity = identities.get(sha256) or state.identity(path, sha256)
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


def resolve_remote_pdf(identifier: str) -> dict:
    value = identifier.strip()
    if value.isdigit() or (
        len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)
    ):
        return api_get(f"/pdfs/{value}")
    try:
        return api_get("/pdfs/by-doi", doi=value)
    except RuntimeError as direct_error:
        rows = api_get("/pdfs/search", q=value, limit=2, offset=0)
        if len(rows) == 1:
            return api_get(f"/pdfs/{rows[0]['id']}")
        if not rows:
            raise direct_error
        raise RuntimeError(
            "DOI identifier matched multiple PDFs; use the PDF id or SHA-256 instead"
        ) from direct_error


def render_doi_correction_preview(result: dict) -> None:
    match = result.get("title_match_score")
    match_text = "unavailable" if match is None else f"{float(match) * 100:.0f}%"
    console.print(
        Panel.fit(
            f"[bold]PDF[/bold] #{result['pdf_id']}\n"
            f"[bold]Current DOI[/bold] {result['previous_doi']}\n"
            f"[bold]Requested DOI[/bold] {result['requested_doi']}\n"
            f"[bold]Canonical DOI[/bold] {result['new_doi']}\n"
            f"[bold]SHA-256[/bold] {result['sha256']}\n\n"
            f"[bold]Metadata[/bold] {result.get('metadata_provider') or '-'}\n"
            f"[bold]Title[/bold] {result.get('title') or '-'}\n"
            f"[bold]Journal[/bold] {result.get('container_title') or '-'}\n"
            f"[bold]Year[/bold] {result.get('published_year') or '-'}\n"
            f"[bold]PDF title match[/bold] {match_text}",
            title="DOI Correction Preview",
        )
    )


@app.command(
    "correct-doi",
    epilog="""\b
Examples:
  nv correct-doi 56240 10.1287/msom.2021.1032 --reason "Incorrect PDF DOI extraction"
  nv correct-doi 10.1287/msom.2021.1032%29%3E%3E/border 10.1287/msom.2021.1032 --reason "Trim PDF object syntax"
  nv correct-doi 56240 10.1287/msom.2021.1032 --reason "Verified manually" --dry-run
""",
)
def correct_doi_command(
    identifier: str = typer.Argument(..., help="Current DOI, PDF id, or SHA-256."),
    new_doi: str = typer.Argument(..., help="Correct DOI for the existing PDF."),
    reason: str = typer.Option(..., "--reason", help="Why the DOI identity is being corrected."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without changing the server."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without an interactive confirmation."),
    allow_title_mismatch: bool = typer.Option(
        False,
        "--allow-title-mismatch",
        help="Explicitly allow a verified DOI whose metadata title does not match PDF text.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    me = api_get("/me")
    if me.get("role") != "admin":
        raise RuntimeError("Admin role required")

    pdf = resolve_remote_pdf(identifier)
    payload = {
        "doi": new_doi,
        "reason": reason,
        "expected_sha256": pdf["sha256"],
        "expected_current_doi": pdf["doi"],
        "allow_title_mismatch": allow_title_mismatch,
        "dry_run": True,
    }
    preview = api_post(f"/admin/pdfs/{pdf['id']}/correct-doi", payload)

    if json_output:
        typer.echo(json.dumps(preview, ensure_ascii=False))
    else:
        render_doi_correction_preview(preview)

    if dry_run:
        return

    if preview.get("requires_title_override") and not allow_title_mismatch:
        raise RuntimeError(
            "Metadata title does not match the PDF. Review the preview and rerun with "
            "--allow-title-mismatch only when the DOI was independently verified."
        )

    if not yes and not typer.confirm(
        "Apply DOI correction? The PDF file, SHA-256, aliases and history stay unchanged",
        default=False,
    ):
        console.print("Cancelled.")
        return

    payload.update(
        {
            "expected_sha256": preview["sha256"],
            "expected_current_doi": preview["previous_doi"],
            "dry_run": False,
        }
    )
    result = api_post(f"/admin/pdfs/{pdf['id']}/correct-doi", payload)
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False))
    else:
        console.print(
            f"Corrected PDF #{result['pdf_id']}: "
            f"{result['previous_doi']} -> {result['new_doi']}"
        )
        if result.get("title"):
            console.print(f"Metadata: {result['title']}")


@app.command(
    "doi-audit",
    epilog="""\b
Examples:
  nv doi-audit
  nv doi-audit --limit 500 --json
""",
)
def doi_audit_command(
    limit: int = typer.Option(500, min=1, max=500, help="Maximum active PDFs to inspect."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    me = api_get("/me")
    if me.get("role") != "admin":
        raise RuntimeError("Admin role required")
    rows = api_get("/pdfs", limit=limit, offset=0)
    suspicious = []
    tokens = (">>", "<<", "/border", "/uri", "/annot")
    for row in rows:
        doi = str(row.get("doi") or "")
        reasons = []
        if any(token in doi.lower() for token in tokens):
            reasons.append("PDF syntax in DOI")
        if row.get("crossref_status") != "ok":
            reasons.append(f"metadata status={row.get('crossref_status')}")
        try:
            normalized = normalize_doi(doi)
        except ValueError:
            reasons.append("invalid under resolver v4")
        else:
            if normalized != doi.lower():
                reasons.append(f"normalizes to {normalized}")
        if reasons:
            suspicious.append({**row, "audit_reasons": reasons})

    if json_output:
        typer.echo(json.dumps(suspicious, ensure_ascii=False))
        return
    if not suspicious:
        console.print("No suspicious DOI records found in the inspected rows.")
        return
    table = Table(title="DOI Audit", box=box.SIMPLE_HEAVY)
    table.add_column("ID", justify="right")
    table.add_column("DOI")
    table.add_column("Reason")
    for row in suspicious:
        table.add_row(str(row["id"]), row["doi"], "; ".join(row["audit_reasons"]))
    console.print(table)


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


@app.command("export", epilog="""\b
Examples:
  nv export --issn 0025-1909
  nv export --issn 1526-5501 --out ./management-science.csv
  nv export --issn 0025-1909 --out ./management-science.csv --force
""")
def export_command(
    issn: str = typer.Option(..., "--issn", "-i", help="Print ISSN or electronic ISSN of the journal."),
    output: Path | None = typer.Option(
        None, "--out", "--output", "-o",
        help="CSV path. Defaults to netvault-<ISSN>.csv; adds a number if it exists.",
    ),
    primary_only: bool = typer.Option(False, "--primary-only", help="Export only retained PDF hashes, excluding aliases."),
    force: bool = typer.Option(False, "--force", help="Replace an existing output CSV."),
) -> None:
    """Export all active journal DOI and PDF SHA-256 pairs to CSV."""
    try:
        normalized = normalize_export_issn(issn)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--issn") from exc
    destination = (
        output.expanduser().resolve() if output is not None
        else default_export_destination(normalized, force=force)
    )
    count = save_issn_export(normalized, destination, force=force, **({"primary_only": True} if primary_only else {}))
    console.print(f"Exported {count:,} DOI/SHA-256 rows for ISSN {normalized} to {destination}",
                  markup=False)


def fetch_download_batch(dois: list[str]) -> dict[str, dict]:
    # This POST is a read-only lookup, so transient failures are safe to retry.
    for attempt in range(3):
        try:
            return get_existing_pdfs_by_doi(dois)
        except ApiError as exc:
            if exc.status_code not in {429, 502, 503, 504} or attempt == 2:
                raise
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
        sleep(0.4 * 2**attempt)
    raise RuntimeError("DOI lookup failed")



def fetch_journal_download_dois(journal: str) -> list[str]:
    """Read the complete server inventory before creating download plans."""
    name = journal.strip()
    if not name:
        raise typer.BadParameter("Journal name must not be blank", param_hint="--journal")
    with http_session().get(
        f"{server_url()}/pdfs/export", headers=auth_headers(),
        params={"journal": name, "include_aliases": "false"},
        timeout=(15, 120),
    ) as response:
        raise_for_api_error(response)
        if response.headers.get("Content-Type", "").split(";", 1)[0].lower() != "text/csv":
            raise RuntimeError("Server did not return a journal inventory CSV")
        try:
            expected = int(response.headers["X-NetVault-Rows"])
            if expected < 0:
                raise ValueError
        except (KeyError, ValueError):
            raise RuntimeError("Server returned an invalid journal inventory row count") from None
        reader = csv.reader(io.StringIO(response.content.decode("utf-8-sig")), strict=True)
        if next(reader, None) != ["doi", "sha256"]:
            raise RuntimeError("Invalid journal inventory columns")
        dois = []
        count = 0
        for row in reader:
            if len(row) != 2 or not row[0] or not re.fullmatch(r"[0-9a-f]{64}", row[1]):
                raise RuntimeError("Invalid journal inventory row")
            count += 1
            dois.append(row[0])
        if count != expected:
            raise RuntimeError(f"Incomplete journal inventory: expected {expected}, got {count}")
    return list(dict.fromkeys(dois))

def run_download_plan(
    plan: DownloadPlan, base_url: str, headers: dict[str, str], stop: Event | None = None,
) -> str:
    def check_stopped() -> None:
        if stop is not None and stop.is_set():
            raise InterruptedError("Download interrupted; partial file kept")

    check_stopped()
    request_headers = dict(headers)
    mode = "wb"
    did_resume = False
    digest = hashlib.sha256()
    if plan.resume_offset > 0:
        request_headers["Range"] = f"bytes={plan.resume_offset}-"
    with http_session().get(
        f"{base_url}/pdfs/by-doi/download",
        headers=request_headers,
        params={"doi": plan.doi},
        timeout=(15, 30),
        stream=True,
    ) as response:
        raise_for_api_error(response)
        if plan.resume_offset > 0 and response.status_code == 206:
            content_range = response.headers.get("Content-Range", "")
            if not content_range.startswith(f"bytes {plan.resume_offset}-"):
                raise RuntimeError("server returned an invalid Content-Range")
            # Hash only the existing prefix, then update while writing new bytes.
            with plan.part_path.open("rb") as prefix:
                for chunk in iter(lambda: prefix.read(DOWNLOAD_CHUNK_SIZE), b""):
                    check_stopped()
                    digest.update(chunk)
            mode = "ab"
            did_resume = True
        elif plan.resume_offset > 0:
            plan.part_path.unlink(missing_ok=True)
        with plan.part_path.open(mode) as handle:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                check_stopped()
                if chunk:
                    handle.write(chunk)
                    digest.update(chunk)
    actual_size = plan.part_path.stat().st_size
    if actual_size != plan.size:
        raise RuntimeError(f"incomplete download: expected {plan.size} bytes, got {actual_size}")
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != plan.sha256:
        raise RuntimeError(f"checksum mismatch: expected {plan.sha256}, got {actual_sha256}")
    check_stopped()
    plan.part_path.replace(plan.destination)
    return "resumed" if did_resume else "downloaded"


@app.command(
    epilog="""\b
Examples:
  nv download 10.1016/j.ijpe.2018.04.006 --to ~/Downloads
  nv download 10.1016/j.ijpe.2018.04.006 10.1234/example.doi --to ./downloads
  nv download --file ./dois.txt --to ./downloads
  nv download 10.1016/j.ijpe.2018.04.006 --file ./more-dois.txt --to ./downloads
  nv download --file ./dois.txt --to ./downloads --workers 8
  nv download --journal "Management Science" --to ./downloads

\b
Notes:
  --file reads any text file and extracts DOI values with NetVault's DOI regex.
  --journal matches an exact normalized journal name in the server library.
  Only PDFs already stored on the NetVault server can be downloaded.
  Duplicate DOI values are downloaded once.
  Server DOI matches are fetched in batches before downloads start.
  Missing DOI values are counted quietly and excluded from download progress.
  Downloads use parallel workers and automatically resume .part files.
  SHA-256 is verified while writing; existing matching files are skipped.
  After interruption or a rate limit, run the same command with the same --to.
  Successful transfers and failures append to .netvault/history.jsonl in the destination.
  Downloads can resume from existing PDF and .part files; no report JSON is created.
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
    journal: str | None = typer.Option(None, "--journal", "-j", help="Download all stored PDFs for an exact journal name."),
) -> None:
    ensure_logged_in()
    requested_dois = collect_dois(dois or [], doi_files)
    if journal is not None:
        requested_dois = list(dict.fromkeys([*requested_dois, *fetch_journal_download_dois(journal)]))
    if not requested_dois:
        raise typer.BadParameter("Provide at least one DOI, --file PATH, or --journal NAME")

    to = to.expanduser().resolve()
    to.mkdir(parents=True, exist_ok=True)
    download_history = History(to, "download")
    failed: list[tuple[str, str]] = []
    unavailable = skipped = downloaded = resumed = 0
    plans: list[DownloadPlan] = []
    used_destinations: set[Path] = set()
    stop = Event()
    base_url = server_url()
    headers = auth_headers()
    pending = {}
    deferred: list[str] = []
    rate_limited = False
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed:.0f}/{task.total:.0f} {task.fields[unit]}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Matching server DOI list", total=len(requested_dois), unit="DOIs")
        try:
            # Match the full input against the server's indexed SQL lookup first.
            # Only available PDFs still needed locally enter the download queue.
            for index in range(0, len(requested_dois), DOWNLOAD_LOOKUP_BATCH_SIZE):
                batch = requested_dois[index:index + DOWNLOAD_LOOKUP_BATCH_SIZE]
                try:
                    details = fetch_download_batch(batch)
                except (RuntimeError, OSError, requests.RequestException) as exc:
                    failed.extend((doi, str(exc)) for doi in batch)
                    progress.advance(task, len(batch))
                    continue
                for doi in batch:
                    detail = details.get(doi)
                    if detail is None:
                        unavailable += 1
                        continue
                    try:
                        size = int(detail["size"])
                        destination, part_path, resume_offset, complete = plan_download_destination(
                            to, detail["original_name"], size, used_destinations,
                            expected_sha256=detail["sha256"],
                        )
                    except OSError as exc:
                        failed.append((doi, str(exc)))
                        continue
                    if complete:
                        skipped += 1
                    else:
                        plans.append(DownloadPlan(
                            doi=doi, original_name=detail["original_name"], size=size,
                            destination=destination, part_path=part_path,
                            sha256=detail["sha256"], resume_offset=resume_offset,
                        ))
                progress.advance(task, len(batch))
            progress.update(task, total=len(plans), completed=0,
                            description="Downloading PDFs", unit="PDFs")
            worker_count = min(workers, max(1, len(plans)))

            def finish_downloads() -> None:
                nonlocal downloaded, resumed, rate_limited
                done = wait(pending, return_when=FIRST_COMPLETED).done
                for future in done:
                    plan = pending.pop(future)
                    if future.cancelled():
                        deferred.append(plan.doi)
                        continue
                    try:
                        status_name = future.result()
                        if status_name == "resumed":
                            resumed += 1
                        else:
                            downloaded += 1
                        download_history.write(
                            "download", file=plan.destination, doi=plan.doi,
                            result="ok", reason="resumed" if status_name == "resumed" else None,
                        )
                    except InterruptedError:
                        deferred.append(plan.doi)
                        continue
                    except ApiError as exc:
                        if exc.status_code == 429:
                            rate_limited = True
                            stop.set()
                            deferred.append(plan.doi)
                            for queued in pending:
                                queued.cancel()
                            continue
                        failed.append((plan.doi, str(exc)))
                    except (RuntimeError, OSError, requests.RequestException) as exc:
                        failed.append((plan.doi, str(exc)))
                    progress.advance(task)

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                try:
                    for plan in plans:
                        while len(pending) >= worker_count * 2 and not stop.is_set():
                            finish_downloads()
                        if stop.is_set():
                            deferred.append(plan.doi)
                            continue
                        pending[executor.submit(run_download_plan, plan, base_url, headers, stop)] = plan
                    while pending:
                        finish_downloads()
                except KeyboardInterrupt:
                    stop.set()
                    for future in pending:
                        future.cancel()
                    raise
        except KeyboardInterrupt:
            console.print(
                f"Interrupted. Partial files kept in {to}. "
                "Run the same download command to continue.", markup=False,
            )
            raise typer.Exit(130) from None

    parts = [f"done: {downloaded + resumed}/{len(plans)} PDFs"]
    if downloaded:
        parts.append(f"{downloaded} downloaded")
    if resumed:
        parts.append(f"{resumed} resumed")
    if skipped:
        parts.append(f"{skipped} already saved")
    if unavailable:
        parts.append(f"{unavailable} unavailable on server")
    if failed:
        parts.append(f"{len(failed)} failed")
    if deferred:
        parts.append(f"{len(deferred)} pending")
    parts.append(f"to: {to}")
    console.print("; ".join(parts), markup=False)
    if rate_limited:
        console.print("Server download limit reached. Run the same command later to continue.")
    for doi, error in failed:
        download_history.write("error", doi=doi, result="failed", reason=error)
    for doi in deferred:
        download_history.write("error", doi=doi, result="pending",
                               reason="Interrupted or deferred after download limit")
    download_history.write(
        "summary", result="partial" if (failed or deferred) else "ok",
        counts=dict(downloaded=downloaded, resumed=resumed, existing=skipped,
                    unavailable=unavailable, failed=len(failed), pending=len(deferred)),
    )
    console.print(f"History: {download_history.path}", markup=False)
    if failed or deferred:
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
