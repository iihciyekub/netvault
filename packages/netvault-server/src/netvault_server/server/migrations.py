from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Engine, inspect, text

from netvault_server.server.journal_filters import normalize_journal_name


Migration = Callable[[Engine], None]


def _add_pdf_metadata_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    if "pdfs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("pdfs")}
    columns = {
        "doi": "VARCHAR(255)",
        "doi_source": "VARCHAR(32)",
        "doi_evidence": "TEXT",
        "title": "TEXT",
        "authors": "TEXT",
        "container_title": "TEXT",
        "publisher": "VARCHAR(255)",
        "published_year": "INTEGER",
        "crossref_status": "VARCHAR(32) DEFAULT 'pending'",
        "crossref_url": "TEXT",
        "crossref_fetched_at": "TIMESTAMP",
    }
    with engine.begin() as connection:
        for name, sql_type in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE pdfs ADD COLUMN {name} {sql_type}"))
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_pdfs_doi ON pdfs (doi)"))


def _add_user_token_version(engine: Engine) -> None:
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("users")}
    if "token_version" not in existing:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE users ADD COLUMN token_version INTEGER DEFAULT 0 NOT NULL")
            )


def _add_query_indexes_and_journal_keys(engine: Engine) -> None:
    inspector = inspect(engine)
    if "pdfs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("pdfs")}
    upload_columns = {
        column["name"] for column in inspector.get_columns("upload_records")
    }
    with engine.begin() as connection:
        if "journal_key" not in existing:
            connection.execute(text("ALTER TABLE pdfs ADD COLUMN journal_key VARCHAR(255)"))
        if "idempotency_key" not in upload_columns:
            connection.execute(
                text("ALTER TABLE upload_records ADD COLUMN idempotency_key VARCHAR(160)")
            )
        rows = connection.execute(
            text("SELECT id, container_title FROM pdfs WHERE journal_key IS NULL")
        ).mappings()
        updates = [
            {
                "id": row["id"],
                "journal_key": normalize_journal_name(row["container_title"]) or None,
            }
            for row in rows
        ]
        if updates:
            connection.execute(
                text("UPDATE pdfs SET journal_key = :journal_key WHERE id = :id"),
                updates,
            )

        active_predicate = "is_deleted IS FALSE" if engine.dialect.name == "postgresql" else "is_deleted = 0"
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_pdfs_active_uploaded_at "
                f"ON pdfs (uploaded_at DESC, id DESC) WHERE {active_predicate}"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_pdfs_journal_year "
                "ON pdfs (journal_key, published_year)"
            )
        )
        for table, column, index in (
            ("upload_records", "pdf_id", "ix_upload_records_pdf_id"),
            ("upload_records", "user_id", "ix_upload_records_user_id"),
            ("upload_records", "uploaded_at DESC", "ix_upload_records_uploaded_at"),
            ("download_records", "pdf_id", "ix_download_records_pdf_id"),
            ("download_records", "user_id", "ix_download_records_user_id"),
            ("download_records", "downloaded_at DESC", "ix_download_records_downloaded_at"),
        ):
            connection.execute(text(f"CREATE INDEX IF NOT EXISTS {index} ON {table} ({column})"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_upload_records_idempotency_key "
                "ON upload_records (idempotency_key) WHERE idempotency_key IS NOT NULL"
            )
        )

        if engine.dialect.name == "postgresql":
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_pdfs_search_trgm ON pdfs USING gin "
                    "((lower(coalesce(doi, '') || ' ' || coalesce(title, '') || ' ' || "
                    "coalesce(authors, '') || ' ' || coalesce(container_title, '') || ' ' || "
                    "coalesce(publisher, '') || ' ' || coalesce(original_name, '') || ' ' || "
                    "coalesce(sha256, ''))) gin_trgm_ops) WHERE is_deleted IS FALSE"
                )
            )
            connection.execute(text("ANALYZE pdfs"))
            connection.execute(text("ANALYZE upload_records"))
            connection.execute(text("ANALYZE download_records"))


def _add_pdf_file_aliases(engine: Engine) -> None:
    # Use SQLAlchemy's table definition so this migration remains portable across
    # SQLite and PostgreSQL. create_all normally creates it first; checkfirst also
    # supports databases upgraded through the migration runner directly.
    from netvault_server.server.models import PdfFileAlias

    PdfFileAlias.__table__.create(bind=engine, checkfirst=True)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ix_pdf_file_aliases_sha256 ON pdf_file_aliases (sha256)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_pdf_file_aliases_pdf_id ON pdf_file_aliases (pdf_id)")
        )


def _add_pdf_doi_corrections(engine: Engine) -> None:
    from netvault_server.server.models import PdfDoiCorrection

    PdfDoiCorrection.__table__.create(bind=engine, checkfirst=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_pdf_doi_corrections_pdf_id "
                "ON pdf_doi_corrections (pdf_id)"
            )
        )


def _add_user_journal_filters(engine: Engine) -> None:
    from netvault_server.server.models import UserJournalFilter

    UserJournalFilter.__table__.create(bind=engine, checkfirst=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_user_journal_filters_user_id "
                "ON user_journal_filters (user_id)"
            )
        )


def _add_user_dashboard_journal_limit(engine: Engine) -> None:
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("users")}
    if "dashboard_journal_limit" not in existing:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE users ADD COLUMN dashboard_journal_limit "
                    "INTEGER DEFAULT 20 NOT NULL"
                )
            )


def _add_user_dashboard_filter_key(engine: Engine) -> None:
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    if "dashboard_filter_key" not in {column["name"] for column in inspector.get_columns("users")}:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE users ADD COLUMN dashboard_filter_key "
                "VARCHAR(32) NOT NULL DEFAULT 'utd24'"
            ))


def _add_shared_journal_rank_cache(engine: Engine) -> None:
    """Idempotent journal rank data and resumable-job storage."""
    from netvault_server.server.models import JournalRankBatchJob, JournalRankCache

    for table in (JournalRankCache.__table__, JournalRankBatchJob.__table__):
        table.create(bind=engine, checkfirst=True)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_journal_rank_batch_jobs_filter_key "
            "ON journal_rank_batch_jobs (filter_key)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_journal_rank_batch_jobs_status "
            "ON journal_rank_batch_jobs (status)"
        ))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_rank_batch_active_snapshot "
            "ON journal_rank_batch_jobs (snapshot_hash) "
            "WHERE status IN ('queued', 'running')"
        ))


def _add_maintenance_jobs(engine: Engine) -> None:
    """Additive maintenance queue; survives upgrades from diverged schema versions."""
    from netvault_server.server.models import MaintenanceJob

    MaintenanceJob.__table__.create(bind=engine, checkfirst=True)
    existing = {column["name"] for column in inspect(engine).get_columns("maintenance_jobs")}
    with engine.begin() as connection:
        if "cancel_requested" not in existing:
            connection.execute(text(
                "ALTER TABLE maintenance_jobs ADD COLUMN cancel_requested BOOLEAN "
                "NOT NULL DEFAULT FALSE"
            ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_maintenance_jobs_status "
            "ON maintenance_jobs (status)"
        ))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_maintenance_active_job "
            "ON maintenance_jobs (singleton_key) "
            "WHERE status IN ('queued', 'running')"
        ))


def _add_pdfcraft_tracking_columns(engine: Engine) -> None:
    """Expose existing host PDFCraft processing markers to the web dashboard."""
    inspector = inspect(engine)
    if "pdfs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("pdfs")}
    dialect = engine.dialect.name
    types = (
        ("pdfcraft_processed_at", "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "TIMESTAMP"),
        ("pdfcraft_processed_sha256", "VARCHAR(64)"),
        ("pdfcraft_ppi", "INTEGER"),
        ("pdfcraft_quality", "INTEGER"),
    )
    with engine.begin() as connection:
        for column, sql_type in types:
            if column not in existing:
                connection.execute(text(f"ALTER TABLE pdfs ADD COLUMN {column} {sql_type}"))


def _add_last_login_at(engine: Engine) -> None:
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("users")}
    if "last_login_at" not in existing:
        sql_type = "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "TIMESTAMP"
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE users ADD COLUMN last_login_at {sql_type}"))


def _add_journal_identity(engine: Engine) -> None:
    from netvault_server.server.journal_identity import journal_display_name
    from netvault_server.server.models import (
        JournalAlias, JournalCanonical, JournalDoiOverride, JournalIssn,
    )

    for table in (JournalCanonical.__table__, JournalAlias.__table__, JournalIssn.__table__,
                  JournalDoiOverride.__table__):
        table.create(bind=engine, checkfirst=True)
    existing = {column["name"] for column in inspect(engine).get_columns("pdfs")}
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SET LOCAL statement_timeout = 0"))
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        for column, sql_type in (
            ("journal_raw_title", "TEXT"), ("journal_issns_json", "TEXT"),
            ("work_type", "VARCHAR(32)"),
        ):
            if column not in existing:
                connection.execute(text(f"ALTER TABLE pdfs ADD COLUMN {column} {sql_type}"))
    # Release the brief ALTER TABLE lock before rewriting metadata. Readers can
    # continue to use the live vault during the larger backfill transaction.
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SET LOCAL statement_timeout = 0"))
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        names = connection.scalars(text(
            "SELECT DISTINCT container_title FROM pdfs WHERE container_title IS NOT NULL"
        )).all()
        updates = [{"name": name, "canonical": journal_display_name(name),
                    "key": normalize_journal_name(journal_display_name(name)) or None}
                   for name in names]
        if engine.dialect.name == "postgresql" and updates:
            # One join avoids scanning the entire PDF heap once for every title.
            params = {f"{key}_{i}": value for i, row in enumerate(updates)
                      for key, value in row.items()}
            values = ", ".join(
                f"(:name_{i}, :canonical_{i}, :key_{i})" for i in range(len(updates))
            )
            connection.execute(text(
                "UPDATE pdfs p SET journal_raw_title = coalesce(p.journal_raw_title, p.container_title), "
                "container_title = names.canonical, journal_key = names.key "
                f"FROM (VALUES {values}) AS names(source, canonical, key) "
                "WHERE p.container_title = names.source AND (p.journal_raw_title IS NULL "
                "OR p.container_title IS DISTINCT FROM names.canonical "
                "OR p.journal_key IS DISTINCT FROM names.key)"
            ), params)
        elif updates:
            connection.execute(text(
                "UPDATE pdfs SET journal_raw_title = coalesce(journal_raw_title, container_title), "
                "container_title = :canonical, journal_key = :key WHERE container_title = :name "
                "AND (journal_raw_title IS NULL OR container_title <> :canonical "
                "OR journal_key <> :key OR journal_key IS NULL)"
            ), updates)
        if engine.dialect.name == "postgresql":
            connection.execute(text((Path(__file__).parent / "data" / "html_entities.sql").read_text()))
            sql = (Path(__file__).parent / "data" / "journal_identity.sql").read_text()
            connection.execute(text(sql))
            # Catch an old-version writer that inserted a row during backfill;
            # the trigger also normalizes any writes after this transaction.
            connection.execute(text(
                "UPDATE pdfs SET container_title = container_title "
                "WHERE container_title IS DISTINCT FROM netvault_journal_display_name(container_title) "
                "OR (container_title IS NOT NULL AND journal_raw_title IS NULL)"
            ))


def _align_identity_schema(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        return
    columns = {row["name"]: row for row in inspect(engine).get_columns("pdfs")}
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout = 0"))
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        if connection.scalar(text("SELECT count(*) FROM pdfs WHERE doi IS NULL OR doi = ''")):
            raise RuntimeError("Cannot enforce DOI identity: resolve missing DOI records before upgrading")
        connection.execute(text("UPDATE pdfs SET crossref_status = 'pending' WHERE crossref_status IS NULL"))
        connection.execute(text("ALTER TABLE pdfs ALTER COLUMN crossref_status SET DEFAULT 'pending'"))
        connection.execute(text("ALTER TABLE users ALTER COLUMN token_version SET DEFAULT 0"))
        connection.execute(text("ALTER TABLE users ALTER COLUMN dashboard_journal_limit SET DEFAULT 20"))
        if columns["doi"]["nullable"]:
            connection.execute(text("ALTER TABLE pdfs ALTER COLUMN doi SET NOT NULL"))
        if columns["crossref_status"]["nullable"]:
            connection.execute(text("ALTER TABLE pdfs ALTER COLUMN crossref_status SET NOT NULL"))
        if not columns["crossref_fetched_at"]["type"].timezone:
            # Historical providers write datetime.now(timezone.utc). The old
            # PostgreSQL container uses UTC; preserve those instants explicitly.
            connection.execute(text("ALTER TABLE pdfs ALTER COLUMN crossref_fetched_at "
                                    "TYPE TIMESTAMPTZ USING crossref_fetched_at AT TIME ZONE 'UTC'"))
        for filename in ("html_entities.sql", "journal_identity.sql"):
            connection.execute(text((Path(__file__).parent / "data" / filename).read_text()))
        connection.execute(text("UPDATE pdfs SET container_title = container_title "
            "WHERE container_title IS DISTINCT FROM netvault_journal_display_name(container_title)"))
    # Build the narrower search index without blocking writers. Keep the old
    # index until the replacement has completed, and recover interrupted builds.
    expression = "lower(coalesce(doi, '') || ' ' || coalesce(title, '') || ' ' || " \
                 "coalesce(authors, '') || ' ' || coalesce(container_title, '') || ' ' || " \
                 "coalesce(publisher, '') || ' ' || coalesce(original_name, ''))"
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("SET statement_timeout = 0"))
        try:
            valid = connection.scalar(text("SELECT indisvalid FROM pg_index "
                "WHERE indexrelid = to_regclass('ix_pdfs_search_text_trgm')"))
            if valid is False:
                connection.execute(text("DROP INDEX CONCURRENTLY ix_pdfs_search_text_trgm"))
            connection.execute(text("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pdfs_search_text_trgm "
                f"ON pdfs USING gin (({expression}) gin_trgm_ops) WHERE is_deleted IS FALSE"))
            connection.execute(text("DROP INDEX CONCURRENTLY IF EXISTS ix_pdfs_search_trgm"))
        finally:
            connection.execute(text("RESET statement_timeout"))


MIGRATIONS: tuple[Migration, ...] = (
    _add_pdf_metadata_columns,
    _add_user_token_version,
    _add_query_indexes_and_journal_keys,
    _add_pdf_file_aliases,
    _add_user_journal_filters,
    _add_user_dashboard_journal_limit,
    _add_pdf_doi_corrections,
    _add_last_login_at,
    _add_journal_identity,
    _align_identity_schema,
    _add_user_dashboard_filter_key,
    _add_shared_journal_rank_cache,
)


def run_migrations(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS netvault_schema_version "
                "(id INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
            )
        )
        current = connection.execute(
            text("SELECT version FROM netvault_schema_version WHERE id = 1")
        ).scalar_one_or_none()
        if current is None:
            connection.execute(
                text("INSERT INTO netvault_schema_version (id, version) VALUES (1, 0)")
            )
            current = 0

    for version, migration in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        migration(engine)
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE netvault_schema_version SET version = :version WHERE id = 1"),
                {"version": version},
            )

    # Some long-lived installations have schema version markers that were
    # advanced by different historical deployment branches without matching
    # column changes. Reconcile this additive, idempotent preference field
    # independently of the stored migration version before startup loads User.
    _add_user_dashboard_filter_key(engine)
    _add_shared_journal_rank_cache(engine)
    _add_maintenance_jobs(engine)
    _add_pdfcraft_tracking_columns(engine)
