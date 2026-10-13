"""Read-only production audit of stored primary PDF titles versus indexed titles.

Run from within the server container. Writes a JSON report without mutating the
database, aliases, or PDF storage. Suspect matches need manual confirmation.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from netvault_server.server.article_title import page_title, primary_title_score
from netvault_server.server.database import SessionLocal
from netvault_server.server.models import Pdf, PdfFileAlias
from netvault_server.server.storage import object_path


def audit(limit: int, alias_limit: int) -> dict:
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "pdfs_checked": 0,
        "aliases_checked": 0,
        "title_mismatches": [],
        "alias_mismatches": [],
        "unreadable_pdfs": 0,
        "unreadable_aliases": 0,
        "aliases_without_retained_object": 0,
    }
    with SessionLocal() as session:
        pdfs = session.scalars(
            select(Pdf).where(Pdf.is_deleted.is_(False)).order_by(Pdf.id.desc()).limit(limit)
        ).all()
        for pdf in pdfs:
            result["pdfs_checked"] += 1
            title = page_title(object_path(pdf.sha256))
            if title is None:
                result["unreadable_pdfs"] += 1
            elif pdf.title and (score := primary_title_score(pdf.title, title)) < 0.75:
                result["title_mismatches"].append({
                    "pdf_id": pdf.id, "doi": pdf.doi, "sha256": pdf.sha256,
                    "stored_title": pdf.title, "first_page_title": title,
                    "score": round(score, 3), "doi_source": pdf.doi_source,
                })
        aliases = session.execute(
            select(PdfFileAlias, Pdf)
            .join(Pdf, PdfFileAlias.pdf_id == Pdf.id)
            .where(Pdf.is_deleted.is_(False))
            .order_by(PdfFileAlias.id.desc()).limit(alias_limit)
        ).all()
        for alias, pdf in aliases:
            result["aliases_checked"] += 1
            if not object_path(alias.sha256).is_file():
                # A file alias records identity, not another retained object.
                result["aliases_without_retained_object"] += 1
                continue
            title = page_title(object_path(alias.sha256))
            if title is None:
                result["unreadable_aliases"] += 1
            elif pdf.title and (score := primary_title_score(pdf.title, title)) < 0.75:
                result["alias_mismatches"].append({
                    "alias_id": alias.id, "pdf_id": pdf.id, "doi": pdf.doi,
                    "alias_sha256": alias.sha256, "stored_title": pdf.title,
                    "alias_first_page_title": title, "score": round(score, 3),
                })
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--alias-limit", type=int, default=300)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = audit(max(0, args.limit), max(0, args.alias_limit))
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: len(value) if isinstance(value, list) else value
                      for key, value in data.items() if key != "generated_at"}))
