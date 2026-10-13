import json

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from netvault_server.server.database import Base
from netvault_server.server.journal_identity import journal_display_name, normalize_issn
from netvault_server.server.migrations import run_migrations
from netvault_server.server.models import JournalAlias, JournalCanonical, JournalIssn, Pdf, User


def test_name_formatting_and_issn_validation():
    assert journal_display_name("  Psychology &amp;\n Marketing ") == "PSYCHOLOGY & MARKETING"
    assert journal_display_name("ＡＢＣ\u00a0Journal") == "ABC JOURNAL"
    assert normalize_issn("1526 548x") == "1526-548X"
    assert normalize_issn("1526-5480") is None
    assert normalize_issn("0000-0001") is None


def test_metadata_refresh_aliases_filters_and_conflicting_identifiers(tmp_path):
    from netvault_server.server.crossref import CrossrefMetadata
    from netvault_server.server.main_helpers import apply_crossref_metadata
    from netvault_server.server.journal_filters import allowed_journals_for_filter
    from netvault_server.server.stats import get_by_journal_year

    engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    Base.metadata.create_all(engine)
    run_migrations(engine)
    with Session(engine) as db:
        user = User(username="test", password_hash="unused")
        db.add(user)
        db.add_all([JournalCanonical(canonical_name=name) for name in (
            "MANAGEMENT SCIENCE", "MARKETING SCIENCE", "NAVAL RESEARCH LOGISTICS",
        )])
        db.flush()
        db.add_all([
            JournalAlias(alias_name="MANAGEMENT SCIENCES", canonical_name="MANAGEMENT SCIENCE", evidence_json="{}"),
            JournalAlias(alias_name="MANAGEMENT SCIENCE", canonical_name="MANAGEMENT SCIENCE", evidence_json="{}"),
            JournalAlias(alias_name="NAVAL RESEARCH LOGISTICS (NRL)", canonical_name="NAVAL RESEARCH LOGISTICS", evidence_json="{}"),
            JournalIssn(issn="0025-1909", canonical_name="MANAGEMENT SCIENCE", evidence_json="{}"),
            JournalIssn(issn="1526-5501", canonical_name="MANAGEMENT SCIENCE", evidence_json="{}"),
            JournalIssn(issn="0732-2399", canonical_name="MARKETING SCIENCE", evidence_json="{}"),
        ])
        db.flush()

        def add_pdf(number, name, issns=()):
            pdf = Pdf(doi=f"10.1234/identity.{number}", sha256=f"{number:064x}",
                      original_name="test.pdf", size=10, storage_path="unused",
                      uploaded_by_id=user.id, container_title=name, published_year=2024,
                      journal_issns_json=json.dumps(issns))
            db.add(pdf)
            db.flush()
            return pdf

        first = add_pdf(1, "Management Sciences")
        second = add_pdf(2, "An unseen name", ["1526-5501"])
        conflict = add_pdf(3, "Management Sciences", ["0025-1909", "0732-2399"])
        unregistered = add_pdf(4, "Management Sciences", ["1932-6203"])
        historical = add_pdf(5, "Naval Research Logistics Quarterly")
        add_pdf(6, "Naval Research Logistics (NRL)")
        add_pdf(7, "Proceedings of the 2020 CHI Conference")
        add_pdf(8, "Proceedings of the 2023 CHI Conference")
        assert first.container_title == second.container_title == "MANAGEMENT SCIENCE"
        assert first.journal_raw_title == "Management Sciences"
        assert conflict.container_title == unregistered.container_title == "MANAGEMENT SCIENCES"
        assert historical.container_title == "NAVAL RESEARCH LOGISTICS QUARTERLY"
        # Refreshing metadata retains the registry identity and its raw evidence.
        apply_crossref_metadata(first, CrossrefMetadata(
            status="ok", container_title="Management Sciences", issns=("0025-1909",),
            work_type="journal-article"), overwrite=False)
        db.flush()
        assert first.container_title == "MANAGEMENT SCIENCE"
        assert first.journal_raw_title == "Management Sciences"
        assert json.loads(first.journal_issns_json) == ["0025-1909"]
        assert "management science" in allowed_journals_for_filter("utd24", db)
        rows = get_by_journal_year(db, limit=100)["rows"]
        assert next(r for r in rows if r["journal"] == "MANAGEMENT SCIENCE")["total"] == 2
        assert len([r for r in rows if "CHI CONFERENCE" in r["journal"]]) == 2
        db.commit()
    # Rerunning migrations must preserve original titles and the approved map.
    run_migrations(engine)
    with Session(engine) as db:
        assert db.get(Pdf, 1).journal_raw_title == "Management Sciences"
        assert db.scalar(select(JournalAlias.canonical_name).where(
            JournalAlias.alias_name == "MANAGEMENT SCIENCES")) == "MANAGEMENT SCIENCE"
        assert db.scalar(text("SELECT version FROM netvault_schema_version")) == 10
