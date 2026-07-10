from collections.abc import Callable

from sqlalchemy import Engine, inspect, text


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


MIGRATIONS: tuple[Migration, ...] = (_add_pdf_metadata_columns, _add_user_token_version)


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
