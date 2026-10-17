#!/usr/bin/env python3
"""Review or explicitly repair missing NetVault PostgreSQL bibliography fields.

Default mode is read-only: fetch trusted DOI registry metadata, inspect the PDF,
and emit a JSON proposal. The --apply mode requires an operator-confirmed,
recent complete backup and fills missing fields only. It never changes a DOI,
a PDF's bytes, its SHA-256, storage path, or existing nonempty bibliography.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Callable

from pypdf import PdfReader
from sqlalchemy import create_engine, func, or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from audit_pdf_maintenance import ROOT, pdf_path, write_report
from netvault.cli.pdf_meta import read_pdf_metadata
from netvault.doi import find_dois_in_text, normalize_doi
from netvault_server.server.crossref import fetch_doi_metadata
from netvault_server.server.journal_identity import serial_identifiers
from netvault_server.server.main_helpers import title_match_score
from netvault_server.server.models import Pdf


FIELDS = ("title", "authors", "container_title", "publisher", "published_year", "crossref_url")


def open_engine():
    """Reuse the running server's database connection over private Docker IP."""
    server = json.loads(subprocess.check_output(
        ["docker", "inspect", "netvault-server-1"], timeout=15,
    ))[0]
    environment = dict(
        item.split("=", 1) for item in server["Config"]["Env"] if "=" in item
    )
    if environment.get("NETVAULT_CROSSREF_MAILTO"):
        os.environ.setdefault("NETVAULT_CROSSREF_MAILTO", environment["NETVAULT_CROSSREF_MAILTO"])
    database = json.loads(subprocess.check_output(
        ["docker", "inspect", "netvault-db-1"], timeout=15,
    ))[0]
    ip = next(iter(database["NetworkSettings"]["Networks"].values()))["IPAddress"]
    url = make_url(environment["NETVAULT_DATABASE_URL"]).set(host=ip)
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10})


def normalized_title(title) -> str:
    import re
    return " ".join(re.findall(r"[a-z0-9]+", (title or "").casefold()))


def title_similarity(first: str | None, second: str | None) -> float:
    a, b = normalized_title(first), normalized_title(second)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def verify_pdf_bibliography(path: Path, sql_doi: str, title: str, sql_title: str | None):
    """Require matching PDF identity and a corroborated registry article title."""
    info = read_pdf_metadata(path)
    if info["status"] == "conflict":
        return "review", "conflicting Document Info DOI"
    if info["status"] == "ok" and info["doi"] != sql_doi:
        return "review", "Document Info DOI differs from PostgreSQL"

    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted or not reader.pages:
        return "review", "encrypted or empty PDF"
    text = (reader.pages[0].extract_text() or "")[:20000]
    embedded_matches = info["status"] == "ok" and info["doi"] == sql_doi
    if not embedded_matches:
        # Reference-list DOIs must never justify an automatic database write.
        header = text.split("References", 1)[0][:8000]
        if sql_doi not in find_dois_in_text(header):
            return "review", "no authoritative or first-page matching DOI"
    score = title_match_score(title, text) or 0.0
    info_title_matches = title_similarity(info["document_info"].get("/Title"), title) >= 0.96
    existing_title_matches = title_similarity(sql_title, title) >= 0.96
    if score >= 0.90:
        return "verified", f"first-page-title={score:.3f}"
    if embedded_matches and (info_title_matches or existing_title_matches):
        return "verified", "embedded DOI and matching document/database title"
    return "review", "Crossref title was not corroborated by the PDF"


def current_record(pdf: Pdf) -> dict:
    return {
        "id": pdf.id, "doi": pdf.doi, "sha256": pdf.sha256,
        "size": pdf.size, "storage_path": pdf.storage_path,
        "crossref_status": pdf.crossref_status, "title": pdf.title,
        "authors": pdf.authors, "container_title": pdf.container_title,
        "publisher": pdf.publisher, "published_year": pdf.published_year,
        "crossref_url": pdf.crossref_url,
    }


def proposed_fields(before: dict, metadata) -> dict:
    """Never override any existing nonempty value."""
    result = {}
    for field in FIELDS:
        old = before.get(field)
        new = metadata.resource_url if field == "crossref_url" else getattr(metadata, field)
        if field == "published_year":
            valid = type(new) is int and 1500 <= new <= datetime.now(timezone.utc).year + 2
        else:
            valid = isinstance(new, str) and bool(new.strip())
        if valid and (old is None or old == "" or (isinstance(old, str) and not old.strip())):
            if field == "publisher":
                new = new[:255]
            result[field] = new
    return result


def validate_backup(manifest: Path) -> None:
    """Refuse writes without a verifiable recent SQL dump + PDF snapshot."""
    manifest = manifest.resolve()
    fields = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        fields[key] = value
    created = datetime.strptime(fields["created_utc"], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc
    )
    age = datetime.now(timezone.utc) - created
    if not timedelta(0) <= age <= timedelta(hours=24):
        raise ValueError("Backup must be a complete snapshot less than 24 hours old")
    dump = manifest.parent / "netvault.pg.dump"
    objects = manifest.parent / "storage" / "objects"
    if not dump.is_file() or not objects.is_dir() or dump.stat().st_size == 0:
        raise ValueError("Backup must contain both a SQL dump and PDF objects")
    if len(fields.get("database_sha256", "")) != 64:
        raise ValueError("Backup SQL dump SHA-256 is missing")
    digest = hashlib.sha256()
    with dump.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != fields["database_sha256"].lower():
        raise ValueError("Backup SQL dump checksum differs from its manifest")


def get_candidates(session: Session, *, limit: int, hours: int, include_partial: bool):
    needs = or_(
        Pdf.crossref_status != "ok", Pdf.title.is_(None),
        func.trim(Pdf.title) == "",
    )
    if include_partial:
        needs = or_(
            needs, Pdf.authors.is_(None), func.trim(Pdf.authors) == "",
            Pdf.container_title.is_(None), func.trim(Pdf.container_title) == "",
            Pdf.published_year.is_(None),
        )
    selected = session.scalars(
        select(Pdf).where(
            Pdf.is_deleted.is_(False),
            Pdf.uploaded_at < datetime.now(timezone.utc) - timedelta(hours=hours),
            needs,
        ).order_by(Pdf.uploaded_at.desc(), Pdf.id.desc()).limit(limit)
    ).all()
    return [current_record(pdf) for pdf in selected]


def propose(row: dict, root: Path, fetch=fetch_doi_metadata) -> tuple[dict, object | None]:
    receipt = {"id": row["id"], "doi": row["doi"], "sha256": row["sha256"],
               "decision": "review", "reason": ""}
    try:
        path = pdf_path(root, row)
        # A canonical content-addressed file must match the database bytes
        # before any metadata proposal or database mutation.
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for part in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(part)
        if digest.hexdigest() != row["sha256"]:
            raise ValueError("PDF file SHA-256 does not match PostgreSQL")
        metadata = fetch(row["doi"])
        if metadata.status != "ok" or metadata.provider not in ("crossref", "doi.org"):
            return dict(receipt, reason=f"registry status={metadata.status} provider={metadata.provider}"), None
        if normalize_doi(metadata.canonical_doi or "") != row["doi"] or not metadata.title:
            return dict(receipt, reason="provider DOI/title not verified"), None
        verified, reason = verify_pdf_bibliography(
            path, row["doi"], metadata.title, row.get("title")
        )
        if verified != "verified":
            return dict(receipt, reason=reason), None
        if row.get("title") and title_similarity(row["title"], metadata.title) < 0.88:
            return dict(receipt, reason="existing SQL title conflicts with registry title"), None
        changes = proposed_fields(row, metadata)
        return dict(
            receipt, decision="verified", reason=reason, provider=metadata.provider,
            fill_only=changes, set_crossref_status="ok",
        ), metadata
    except Exception as exc:
        return dict(receipt, reason=f"{type(exc).__name__}: {str(exc)[:200]}"), None


def apply_if_unchanged(session: Session, original: dict, proposal: dict, metadata) -> str:
    """Lock the current DB row; fail closed if it changed during verification."""
    pdf = session.scalar(select(Pdf).where(Pdf.id == original["id"]).with_for_update())
    if pdf is None or pdf.is_deleted or current_record(pdf) != original:
        session.rollback()
        return "skipped: concurrent DB change"
    for field, value in proposal["fill_only"].items():
        setattr(pdf, field, value)
    pdf.crossref_status = "ok"
    pdf.crossref_fetched_at = metadata.fetched_at or datetime.now(timezone.utc)
    if not pdf.work_type and metadata.work_type and len(metadata.work_type) <= 32:
        pdf.work_type = metadata.work_type
    if not pdf.journal_raw_title and metadata.container_title:
        pdf.journal_raw_title = metadata.container_title
    if not pdf.journal_issns_json and metadata.issns:
        pdf.journal_issns_json = json.dumps(serial_identifiers(metadata.issns))
    session.commit()
    return "applied: missing fields and registry status"


def run(
    engine, *, root: Path = ROOT, limit: int = 20, min_age_hours: int = 2,
    include_partial: bool = False, apply: bool = False,
    approved_proposals: list[dict] | None = None,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    if not 1 <= limit <= 100 or min_age_hours < 0:
        raise ValueError("Limit must be 1..100 and minimum age must be nonnegative")
    with Session(engine) as session:
        rows = get_candidates(
            session, limit=limit, hours=min_age_hours, include_partial=include_partial
        )
    proposals = []
    approved = {}
    if approved_proposals is not None:
        for item in approved_proposals:
            if item.get("decision") == "verified" and isinstance(item.get("fill_only"), dict):
                if item["id"] in approved:
                    raise ValueError("Duplicate approved metadata identity.")
                approved[item["id"]] = item
        rows = [row for row in rows if row["id"] in approved]
        if {row["id"] for row in rows} != set(approved):
            raise ValueError("Approved metadata candidates have changed; run a new preview.")

    def report_progress() -> None:
        if progress:
            progress({
                "mode": "metadata_apply" if apply else "metadata_preview",
                "total": len(rows), "processed": len(proposals),
                "verified": sum(p["decision"] == "verified" for p in proposals),
                "applied": sum(p.get("apply_result", "").startswith("applied:") for p in proposals),
            })

    report_progress()
    for row in rows:
        proposal, metadata = propose(row, root)
        if approved_proposals is not None:
            previous = approved[row["id"]]
            if (row["sha256"] != previous.get("sha256")
                    or row["doi"] != previous.get("doi")
                    or proposal.get("decision") != "verified"
                    or proposal.get("fill_only") != previous.get("fill_only")):
                proposal["decision"] = "review"
                proposal["reason"] = "Source changed or proposal differs from approved preview"
                metadata = None
        if apply and proposal["decision"] == "verified" and metadata is not None:
            try:
                with Session(engine) as session:
                    proposal["apply_result"] = apply_if_unchanged(
                        session, row, proposal, metadata
                    )
            except Exception as exc:
                proposal["apply_result"] = f"failed: {type(exc).__name__}: {str(exc)[:180]}"
        proposals.append(proposal)
        report_progress()
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if apply else "preview",
        "selected_rows": len(rows), "proposals": proposals,
        "verified": sum(row["decision"] == "verified" for row in proposals),
        "applied": sum(row.get("apply_result", "").startswith("applied:") for row in proposals),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--min-age-hours", type=int, default=2)
    parser.add_argument("--include-partial", action="store_true")
    parser.add_argument("--storage-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "tmp" / "pdf-metadata-proposals.json")
    parser.add_argument("--apply", action="store_true",
                        help="Apply only verified, fill-only proposals; never run from cron.")
    parser.add_argument("--confirm-apply", action="store_true")
    parser.add_argument("--backup-manifest", type=Path)
    parser.add_argument("--approved-proposals", type=Path,
                        help="Bind --apply to verified identities and fill-only fields in a saved preview.")
    parser.add_argument("--progress-output", type=Path,
                        help="Host-worker JSON counters (no DOI or document metadata)")
    args = parser.parse_args()
    if args.approved_proposals and not args.apply:
        parser.error("--approved-proposals requires --apply")
    approved_proposals = None
    if args.approved_proposals:
        try:
            if args.approved_proposals.is_symlink() or args.approved_proposals.stat().st_size > 2_000_000:
                raise ValueError("Missing or oversized approval report")
            approval = json.loads(args.approved_proposals.read_text(encoding="utf-8"))
            if approval.get("mode") != "preview" or not isinstance(approval.get("proposals"), list):
                raise ValueError("Expected a metadata preview report")
            approved_proposals = approval["proposals"]
            if len(approved_proposals) > 10:
                raise ValueError("Too many reviewed proposals")
        except (ValueError, KeyError, OSError) as exc:
            parser.error(f"invalid approved proposals: {exc}")
    if args.apply:
        if not args.confirm_apply or args.backup_manifest is None:
            parser.error("--apply requires --confirm-apply and --backup-manifest")
        try:
            validate_backup(args.backup_manifest)
        except (ValueError, KeyError, OSError) as exc:
            parser.error(f"verified backup required: {exc}")
    try:
        engine = open_engine()
        try:
            result = run(
                engine, root=args.storage_root, limit=args.limit,
                min_age_hours=args.min_age_hours,
                include_partial=args.include_partial, apply=args.apply,
                approved_proposals=approved_proposals,
                progress=(lambda snapshot: write_report(args.progress_output, snapshot))
                if args.progress_output else None,
            )
        finally:
            engine.dispose()
        write_report(args.output, result)
    except Exception as exc:
        parser.exit(1, f"bibliography maintenance failed: {exc}\n")
    print(json.dumps({
        "mode": result["mode"], "selected": result["selected_rows"],
        "verified": result["verified"], "applied": result["applied"],
        "report": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
