"""Safety and behavior tests for opt-in PDF maintenance tools (no production access)."""
from contextlib import nullcontext
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

from pypdf import PdfWriter
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from netvault_server.server.crossref import CrossrefMetadata
from netvault_server.server.database import Base
from netvault_server.server.models import Pdf, User

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
audit = importlib.import_module("audit_pdf_maintenance")
repair = importlib.import_module("repair_pdf_bibliography")


def pdf_file(root: Path, doi: str = "10.1234/example", title: str = "A Study of Rivers"):
    import hashlib
    import shutil

    source = root / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=400)
    writer.add_metadata({"/DOI": doi, "/Title": title})
    with source.open("wb") as handle:
        writer.write(handle)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    rel = f"objects/{sha[:2]}/{sha}.pdf"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return {
        "id": 1, "doi": doi, "sha256": sha, "size": source.stat().st_size,
        "storage_path": rel, "title": title, "crossref_status": "pending",
        "authors": None, "container_title": None,
        "publisher": None, "published_year": None, "crossref_url": None,
    }


def test_audit_document_info_checks_canonical_path_and_doi(tmp_path):
    row = pdf_file(tmp_path)
    assert audit.pdf_path(tmp_path, row).is_file()
    result = audit.inspect_document_info(tmp_path, row)
    assert result["status"] == "ok"
    assert result["matches_sql_doi"] is True
    assert result["has_title"] is True
    unsafe = {**row, "storage_path": "../escape.pdf"}
    with pytest.raises(ValueError, match="noncanonical"):
        audit.pdf_path(tmp_path, unsafe)


def test_read_only_report_sql_and_limits(tmp_path):
    row = pdf_file(tmp_path)
    fields = ["id", "doi", "sha256", "size", "storage_path", "uploaded_at",
              "crossref_status", "title", "authors", "container_title", "published_year"]
    sample = tuple(row.get(field) for field in fields)
    names = [
        "eligible", "needs_compression", "needs_bibliography",
        "recent_needs_compression", "recent_needs_bibliography",
        "missing_authors", "missing_container_title", "missing_year",
    ]

    class Cursor:
        def __init__(self):
            self.statements = []
            self.description = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            self.statements.append((sql, params))
            if "WITH eligible AS" in sql:
                self.description = [SimpleNamespace(name=name) for name in names]
            else:
                self.description = [SimpleNamespace(name=name) for name in fields]

        def fetchone(self):
            return (100, 1, 2, 1, 2, 5, 6, 7)

        def fetchall(self):
            return [sample]

    class Connection:
        def __init__(self):
            self.cursor_obj = Cursor()

        def transaction(self):
            return nullcontext()

        def cursor(self):
            return self.cursor_obj

    connection = Connection()
    report = audit.audit(
        connection, root=tmp_path, min_age_hours=2,
        recent_days=7, limit=5, inspect_embedded=1
    )
    assert report["read_only"] is True
    assert report["counts"]["needs_compression"] == 1
    assert report["counts"]["needs_bibliography"] == 2
    assert report["embedded_document_info"][0]["matches_sql_doi"] is True
    assert len(report["compression_candidates"]) == 1
    commands = [str(sql).strip().upper() for sql, _ in connection.cursor_obj.statements]
    assert commands[0] == "SET TRANSACTION READ ONLY"
    assert all("UPDATE PDFS" not in sql and "DELETE FROM" not in sql for sql in commands)


def test_repair_proposals_are_fill_only_and_need_title_identity(tmp_path):
    row = pdf_file(tmp_path)
    metadata = CrossrefMetadata(
        status="ok", canonical_doi=row["doi"],
        title="A Study of Rivers", authors="N. Scholar",
        publisher="Sample Publisher", container_title="Journal of Water",
        published_year=2021, provider="crossref",
    )
    proposals = repair.proposed_fields(row, metadata)
    assert "title" not in proposals  # Preserve the existing nonempty database value.
    assert proposals["authors"] == "N. Scholar"
    assert proposals["published_year"] == 2021

    receipt, candidate = repair.propose(row, tmp_path, fetch=lambda doi: metadata)
    assert candidate is metadata
    assert receipt["decision"] == "verified"
    assert receipt["fill_only"]["authors"] == "N. Scholar"

    wrong = CrossrefMetadata(
        status="ok", canonical_doi="10.1234/different",
        title="A Study of Rivers", provider="crossref",
    )
    blocked, candidate = repair.propose(row, tmp_path, fetch=lambda doi: wrong)
    assert blocked["decision"] == "review"
    assert candidate is None


def test_missing_doctype_title_requires_manual_review(tmp_path):
    row = pdf_file(tmp_path, title="Different paper")
    metadata = CrossrefMetadata(
        status="ok", canonical_doi=row["doi"], title="No Connection To This Paper",
        provider="crossref",
    )
    decision, _ = repair.propose(row, tmp_path, fetch=lambda doi: metadata)
    assert decision["decision"] == "review"


def test_apply_rejects_modified_database_rows_and_preserves_existing_fields(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = User(username="operator", password_hash="x")
        session.add(user)
        session.flush()
        pdf = Pdf(
            doi="10.1234/example", sha256="b" * 64,
            original_name="test.pdf", size=23,
            storage_path="objects/bb/" + "b" * 64 + ".pdf",
            uploaded_by_id=user.id, crossref_status="pending",
            title="Trusted SQL title", authors=None,
            container_title=None, published_year=None,
            is_deleted=False,
        )
        session.add(pdf)
        session.commit()
        row_id = pdf.id
        before = repair.current_record(pdf)

    metadata = CrossrefMetadata(
        status="ok", canonical_doi=before["doi"], title="Different provider title",
        authors="A. Person", container_title="Water Journal",
        published_year=2021, provider="crossref",
    )
    proposal = {"fill_only": {
        "authors": "A. Person", "container_title": "Water Journal",
        "published_year": 2021,
    }}
    with Session(engine) as session:
        result = repair.apply_if_unchanged(session, before, proposal, metadata)
        assert result.startswith("applied")
    with Session(engine) as session:
        pdf = session.scalar(select(Pdf).where(Pdf.id == row_id))
        assert pdf.title == "Trusted SQL title"
        assert pdf.authors == "A. Person"
        assert pdf.crossref_status == "ok"
        assert pdf.published_year == 2021
        assert pdf.journal_key
        existing = repair.current_record(pdf)

    with Session(engine) as session:
        result = repair.apply_if_unchanged(session, before, proposal, metadata)
        assert result == "skipped: concurrent DB change"
    assert existing["authors"] == "A. Person"
    engine.dispose()


def test_apply_requires_real_backup_before_any_database_connection(tmp_path, monkeypatch):
    with pytest.raises((OSError, KeyError, ValueError)):
        repair.validate_backup(tmp_path / "nonexistent" / "manifest.txt")
