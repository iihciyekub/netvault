#!/usr/bin/env python3
"""Read-only incremental audit of PostgreSQL PDFCraft and PDF metadata backlog.

This command never modifies PostgreSQL or any PDF. It writes only its JSON
report. Use its report to plan explicit, separately approved maintenance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

import psycopg

from netvault.cli.pdf_meta import read_pdf_metadata


ROOT = Path("/root/iiaide/netvault/storage")
PPI = 150
QUALITY = 75
HEX_SHA = re.compile(r"[a-f0-9]{64}\Z")


def connect_db():
    """Use the existing private Docker network, without printing credentials."""
    server = json.loads(subprocess.check_output(
        ["docker", "inspect", "netvault-server-1"], timeout=15,
    ))[0]
    environment = dict(
        item.split("=", 1) for item in server["Config"]["Env"] if "=" in item
    )
    url = environment["NETVAULT_DATABASE_URL"].replace("+psycopg", "")
    database = json.loads(subprocess.check_output(
        ["docker", "inspect", "netvault-db-1"], timeout=15,
    ))[0]
    network = next(iter(database["NetworkSettings"]["Networks"].values()))
    return psycopg.connect(
        url, host=network["IPAddress"], connect_timeout=10, autocommit=False,
        options="-c default_transaction_read_only=on -c statement_timeout=30000",
    )


def pdf_path(root: Path, row: dict) -> Path:
    """Reject directory traversal, symlinked objects, and noncanonical paths."""
    sha = row["sha256"]
    if not isinstance(sha, str) or not HEX_SHA.fullmatch(sha):
        raise ValueError("invalid SHA-256")
    expected = f"objects/{sha[:2]}/{sha}.pdf"
    if row["storage_path"] != expected:
        raise ValueError("noncanonical storage path")
    path = root / expected
    if any(component.is_symlink() for component in (path, *path.parents)
           if component != root.parent):
        raise ValueError("symlink in PDF storage path")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != row["size"]:
        raise ValueError("missing, nonregular, or size-mismatched PDF")
    return path


def inspect_document_info(root: Path, row: dict) -> dict:
    result = {"id": row["id"], "doi": row["doi"], "sha256": row["sha256"]}
    try:
        path = pdf_path(root, row)
        meta = read_pdf_metadata(path)
        embedded = meta["doi"]
        result["status"] = meta["status"]
        result["embedded_doi"] = embedded
        result["matches_sql_doi"] = (
            embedded == row["doi"] if meta["status"] == "ok" else None
        )
        result["has_title"] = bool(meta["document_info"].get("/Title"))
        result["has_author"] = bool(meta["document_info"].get("/Author"))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        result["status"] = "unavailable"
        result["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return result


def row_json(row: dict) -> dict:
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, datetime):
            result[key] = value.astimezone(timezone.utc).isoformat()
    return result


def audit(
    connection, *, root: Path = ROOT, min_age_hours: int = 2,
    recent_days: int = 7, limit: int = 100, inspect_embedded: int = 0,
) -> dict:
    """Query all backlog counts, then bounded samples; inspect only opted-in PDFs."""
    if min_age_hours < 0 or recent_days < 1 or limit < 1 or inspect_embedded < 0:
        raise ValueError("Invalid audit bounds")
    age = f"{min_age_hours} hours"
    recent = f"{recent_days} days"
    # Read-only PostgreSQL transaction: neither SQL nor Python mutates records.
    with connection.transaction():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(
                """
                WITH eligible AS (
                  SELECT *,
                    (pdfcraft_processed_sha256 IS DISTINCT FROM sha256
                     OR pdfcraft_ppi IS DISTINCT FROM %s
                     OR pdfcraft_quality IS DISTINCT FROM %s) AS needs_compression,
                    (crossref_status IS DISTINCT FROM 'ok'
                     OR NULLIF(BTRIM(title), '') IS NULL) AS needs_bibliography
                  FROM pdfs
                  WHERE is_deleted = false
                    AND uploaded_at < NOW() - %s::interval
                )
                SELECT COUNT(*) AS eligible,
                  COUNT(*) FILTER (WHERE needs_compression) AS needs_compression,
                  COUNT(*) FILTER (WHERE needs_bibliography) AS needs_bibliography,
                  COUNT(*) FILTER (WHERE needs_compression AND uploaded_at >= NOW() - %s::interval)
                    AS recent_needs_compression,
                  COUNT(*) FILTER (WHERE needs_bibliography AND uploaded_at >= NOW() - %s::interval)
                    AS recent_needs_bibliography,
                  COUNT(*) FILTER (WHERE NULLIF(BTRIM(authors), '') IS NULL)
                    AS missing_authors,
                  COUNT(*) FILTER (WHERE NULLIF(BTRIM(container_title), '') IS NULL)
                    AS missing_container_title,
                  COUNT(*) FILTER (WHERE published_year IS NULL) AS missing_year
                FROM eligible
                """,
                (PPI, QUALITY, age, recent, recent),
            )
            names = [column.name for column in cur.description]
            counts = {name: int(value or 0)
                      for name, value in zip(names, cur.fetchone())}

            columns = (
                "id, doi, sha256, size, storage_path, uploaded_at, "
                "crossref_status, title, authors, container_title, published_year"
            )
            cur.execute(
                f"""SELECT {columns} FROM pdfs
                    WHERE is_deleted = false AND uploaded_at < NOW() - %s::interval
                      AND (pdfcraft_processed_sha256 IS DISTINCT FROM sha256
                           OR pdfcraft_ppi IS DISTINCT FROM %s
                           OR pdfcraft_quality IS DISTINCT FROM %s)
                    ORDER BY uploaded_at DESC, id DESC LIMIT %s""",
                (age, PPI, QUALITY, limit),
            )
            names = [column.name for column in cur.description]
            compression = [row_json(dict(zip(names, row))) for row in cur.fetchall()]

            cur.execute(
                f"""SELECT {columns} FROM pdfs
                    WHERE is_deleted = false AND uploaded_at < NOW() - %s::interval
                      AND (crossref_status IS DISTINCT FROM 'ok'
                           OR NULLIF(BTRIM(title), '') IS NULL)
                    ORDER BY uploaded_at DESC, id DESC LIMIT %s""",
                (age, limit),
            )
            names = [column.name for column in cur.description]
            bibliography = [row_json(dict(zip(names, row))) for row in cur.fetchall()]

            if inspect_embedded:
                cur.execute(
                    f"""SELECT {columns} FROM pdfs
                        WHERE is_deleted = false
                          AND uploaded_at >= NOW() - %s::interval
                          AND uploaded_at < NOW() - %s::interval
                        ORDER BY uploaded_at DESC, id DESC LIMIT %s""",
                    (recent, age, inspect_embedded),
                )
                names = [column.name for column in cur.description]
                recent_pdf_rows = [dict(zip(names, row)) for row in cur.fetchall()]
            else:
                recent_pdf_rows = []

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "settings": {
            "min_age_hours": min_age_hours,
            "recent_days": recent_days, "limit": limit,
            "inspect_embedded": inspect_embedded, "ppi": PPI, "quality": QUALITY,
        },
        "counts": counts,
        "compression_candidates": compression,
        "bibliography_candidates": bibliography,
        "embedded_document_info": [
            inspect_document_info(root, row) for row in recent_pdf_rows
        ],
        "notes": [
            "Samples are bounded; use counts for the complete eligible backlog.",
            "A missing Document Info DOI is not permission to rewrite canonical PDF bytes.",
            "Crossref data may be missing legitimately; examine DOI/content before any repair.",
        ],
    }


def write_report(destination: Path, payload: dict) -> None:
    """Atomic, owner-only JSON report; never write into PDF object directories."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".pdf-maintenance-", suffix=".json.tmp", dir=destination.parent
    )
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-age-hours", type=int, default=2)
    parser.add_argument("--recent-days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--inspect-embedded", type=int, default=0,
        help="Read Document Info for up to N recent PDFs; default 0 (SQL only).",
    )
    parser.add_argument("--storage-root", type=Path, default=ROOT)
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "tmp" / "pdf-maintenance-audit.json",
    )
    args = parser.parse_args()
    try:
        with connect_db() as connection:
            report = audit(
                connection, root=args.storage_root,
                min_age_hours=args.min_age_hours, recent_days=args.recent_days,
                limit=args.limit, inspect_embedded=args.inspect_embedded,
            )
        write_report(args.output, report)
    except Exception as exc:
        parser.exit(1, f"audit failed without any database/PDF changes: {exc}\n")
    print(json.dumps({
        "report": str(args.output), "counts": report["counts"],
        "inspected_pdfs": len(report["embedded_document_info"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
