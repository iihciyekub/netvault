"""Export the complete active PDF inventory for a serial identifier."""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from netvault_server.server.database import get_db
from netvault_server.server.deps import get_current_user
from netvault_server.server.journal_filters import normalize_journal_name
from netvault_server.server.journal_identity import normalize_issn
from netvault_server.server.models import JournalIssn, Pdf, PdfFileAlias, User

router = APIRouter(prefix="/pdfs", tags=["pdfs"])


def inventory_rows(db: Session, issn: str, *, include_aliases: bool = True) -> list:
    canonical = db.scalar(select(JournalIssn.canonical_name).where(JournalIssn.issn == issn))
    # Historical articles often predate per-article ISSN storage. Resolve their
    # reviewed canonical journal identity instead of exporting only DOI samples.
    matches = [Pdf.journal_issns_json.contains(f'"{issn}"')]
    if canonical:
        family = db.scalars(select(JournalIssn.issn).where(
            JournalIssn.canonical_name == canonical,
        )).all()
        matches.append(and_(
            Pdf.journal_key == normalize_journal_name(canonical),
            Pdf.container_title == canonical,
            or_(Pdf.journal_issns_json.is_(None), Pdf.journal_issns_json == "[]",
                *[Pdf.journal_issns_json.contains(f'"{value}"') for value in family]),
        ))
    conditions = (Pdf.is_deleted.is_(False), Pdf.doi.is_not(None), or_(*matches))
    query = select(Pdf.doi, Pdf.sha256).where(*conditions)
    if include_aliases:
        query = query.union(select(Pdf.doi, PdfFileAlias.sha256)
            .join(PdfFileAlias, PdfFileAlias.pdf_id == Pdf.id).where(*conditions))
    rows = db.execute(query.order_by("doi", "sha256")).all()
    if canonical is None and not rows:
        raise HTTPException(status_code=404, detail="ISSN is not registered and has no matching stored PDFs")
    return rows


def csv_chunks(rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(("doi", "sha256"))
    for index, row in enumerate(rows, start=1):
        writer.writerow(row)
        if index % 500 == 0:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
    if buffer.tell():
        yield buffer.getvalue()


@router.get("/export")
def export_journal_inventory(
    issn: str = Query(..., min_length=1, max_length=32),
    include_aliases: bool = Query(default=True, description="Include accepted file hashes for identification"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Export DOI and accepted PDF hashes for an ISSN/eISSN."""
    _ = user
    normalized = normalize_issn(issn)
    if normalized is None:
        raise HTTPException(status_code=422, detail="Invalid ISSN format or checksum")
    # Materialize two small columns in one query for a consistent row count,
    # without retaining a database cursor during a slow client download.
    rows = inventory_rows(db, normalized, include_aliases=include_aliases)
    return StreamingResponse(csv_chunks(rows), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="netvault-{normalized}.csv"',
        "X-NetVault-ISSN": normalized,
        "X-NetVault-Rows": str(len(rows)),
    })
