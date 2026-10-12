"""Download and atomically save a validated DOI/SHA-256 inventory CSV."""

import csv
import os
from pathlib import Path
import re
import tempfile

from netvault.cli.http import auth_headers, http_session, raise_for_api_error, server_url


def normalize_export_issn(value: str) -> str:
    digits = re.sub(r"[\s-]", "", value).upper()
    if re.fullmatch(r"[0-9]{7}[0-9X]", digits):
        check = 10 if digits[-1] == "X" else int(digits[-1])
        if (sum(int(v) * w for v, w in zip(digits[:7], range(8, 1, -1))) + check) % 11 == 0:
            return digits[:4] + "-" + digits[4:]
    raise ValueError("Invalid ISSN format or checksum")


def save_issn_export(issn: str, destination: Path, *, force: bool = False) -> int:
    destination = destination.expanduser().resolve()
    if destination.exists() and not force:
        raise FileExistsError(f"Output already exists: {destination}. Use --force to replace it.")
    if destination.is_dir():
        raise IsADirectoryError(f"Output is a directory: {destination}")
    part = None
    try:
        with http_session().get(
            f"{server_url()}/pdfs/export", headers=auth_headers(), params={"issn": issn},
            stream=True, timeout=(10, 60),
        ) as response:
            raise_for_api_error(response)
            if response.headers.get("Content-Type", "").split(";", 1)[0].lower() != "text/csv":
                raise RuntimeError("Server did not return an inventory CSV")
            try:
                expected_rows = int(response.headers["X-NetVault-Rows"])
                if expected_rows < 0:
                    raise ValueError
            except (KeyError, ValueError):
                raise RuntimeError("Server did not provide a valid export row count") from None
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=".nv-export-", suffix=".part", delete=False,
            ) as handle:
                part = Path(handle.name)
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
            try:
                with part.open(encoding="utf-8", newline="") as handle:
                    reader = csv.reader(handle, strict=True)
                    if next(reader, None) != ["doi", "sha256"]:
                        raise RuntimeError("Unexpected export CSV columns")
                    count = 0
                    for row in reader:
                        if len(row) != 2 or not row[0] or not re.fullmatch(r"[0-9a-f]{64}", row[1]):
                            raise RuntimeError("Incomplete or invalid DOI/SHA-256 row in export")
                        count += 1
            except (UnicodeError, csv.Error) as exc:
                raise RuntimeError(f"Invalid inventory CSV: {exc}") from exc
            if count != expected_rows:
                raise RuntimeError(f"Incomplete export: expected {expected_rows} rows, received {count}")
            if force:
                os.replace(part, destination)
            else:
                # Do not overwrite a file created by another process while the
                # CSV was downloading. The temporary file uses the same volume.
                os.link(part, destination)
                part.unlink()
            part = None
            return count
    finally:
        if part is not None:
            part.unlink(missing_ok=True)
