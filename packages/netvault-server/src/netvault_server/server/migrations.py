from collections.abc import Callable

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


MIGRATIONS: tuple[Migration, ...] = (
    _add_pdf_metadata_columns,
    _add_user_token_version,
    _add_query_indexes_and_journal_keys,
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
