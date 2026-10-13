"""Verify fresh/upgraded schemas and Python/SQL normalization in an isolated test DB.

Set NETVAULT_TEST_DATABASE_URL to a database ending in _test. Never uses the
production Settings database URL; removes only the two schemas it creates.
"""

from datetime import datetime, timezone
import json
import os
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from netvault_server.identity_shared import (
    journal_display_name,
    normalize_issn,
    normalize_journal_key,
)
from netvault_server.server.database import Base
from netvault_server.server import models  # noqa: F401
from netvault_server.server.migrations import MIGRATIONS, run_migrations


def snapshot(engine, schema):
    inspector = inspect(engine)
    columns = {
        name: [
            (
                row["name"],
                str(row["type"]),
                row["nullable"],
                (row["default"] or "").replace(schema, "TEST"),
            )
            for row in inspector.get_columns(name)
        ]
        for name in inspector.get_table_names()
    }
    with engine.connect() as connection:
        indexes = connection.execute(
            text(
                "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=:schema ORDER BY indexname"
            ),
            {"schema": schema},
        ).all()
        triggers = (
            connection.execute(
                text(
                    "SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgrelid='pdfs'::regclass AND NOT tgisinternal"
                )
            )
            .scalars()
            .all()
        )
    return {
        "columns": columns,
        "indexes": [(name, sql.replace(schema, "TEST")) for name, sql in indexes],
        "triggers": [value.replace(schema, "TEST") for value in triggers],
    }


def main():
    url = make_url(os.environ["NETVAULT_TEST_DATABASE_URL"])
    if url.get_backend_name() != "postgresql" or not (url.database or "").endswith("_test"):
        raise SystemExit("Use an isolated PostgreSQL database ending in _test")
    admin = create_engine(url)
    schemas = ["nv_fresh_" + uuid4().hex[:12], "nv_upgrade_" + uuid4().hex[:12]]
    engines = []
    try:
        with admin.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public"))
            for name in schemas:
                connection.exec_driver_sql(f'CREATE SCHEMA "{name}"')
        for index, name in enumerate(schemas):
            engine = create_engine(
                url, connect_args={"options": f"-c search_path={name},public -c timezone=UTC"}
            )
            engines.append(engine)
            Base.metadata.create_all(engine)
            if index:
                for migration in MIGRATIONS[:-1]:
                    migration(engine)
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE netvault_schema_version (id INTEGER PRIMARY KEY,version INTEGER NOT NULL)"
                        )
                    )
                    connection.execute(text("INSERT INTO netvault_schema_version VALUES (1,9)"))
                    connection.execute(text("ALTER TABLE pdfs ALTER COLUMN doi DROP NOT NULL"))
                    connection.execute(
                        text("ALTER TABLE pdfs ALTER COLUMN crossref_status DROP NOT NULL")
                    )
                    connection.execute(
                        text(
                            "ALTER TABLE pdfs ALTER COLUMN crossref_fetched_at TYPE TIMESTAMP USING crossref_fetched_at AT TIME ZONE 'UTC'"
                        )
                    )
                    connection.execute(
                        text("ALTER TABLE pdfs DISABLE TRIGGER trg_netvault_journal_identity")
                    )
                    connection.execute(
                        text(
                            "INSERT INTO users (id,username,password_hash,role,is_active,created_at) VALUES (1,'fixture','fixture-only','user',true,now())"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO pdfs (id,doi,sha256,original_name,size,storage_path,uploaded_by_id,uploaded_at,is_deleted,container_title,crossref_status,crossref_fetched_at) VALUES (1,'10.1234/fixture',:sha,'fixture.pdf',20,'fixture',1,now(),false,'Journal &#8211; Test',NULL,'2026-10-13 01:02:03')"
                        ),
                        {"sha": "a" * 64},
                    )
                    connection.execute(text("DROP TABLE netvault_html_entities"))
            run_migrations(engine)
            with engine.connect() as connection:
                for value in [
                    "Journal &#8211; Test",
                    "Journal &ndash; Test",
                    "Journal &eacute; Test",
                    "ＡＢＣ\u00a0Journal",
                    "Journal &amp;amp; Test",
                    "&notit; &#128; &#0;",
                    "Journal &#x1F600; Test",
                    "Journal &#999999999999999999; Test",
                    "The Journal & NotEqualTilde; Test",
                    "Journal &unknown; Test",
                    "Straße &szlig; Journal",
                    "Journal ΐ ῳ Test",
                ]:
                    actual = connection.scalar(
                        text("SELECT netvault_journal_display_name(:value)"), {"value": value}
                    )
                    assert actual == journal_display_name(value), (
                        value,
                        actual,
                        journal_display_name(value),
                    )
                for value in ["00251909", "1526 548x", "0000-0001", "invalid", None]:
                    assert connection.scalar(
                        text("SELECT netvault_normalize_issn(:value)"), {"value": value}
                    ) == normalize_issn(value)
                assert connection.scalar(
                    text("SELECT version FROM netvault_schema_version")
                ) == len(MIGRATIONS)
                if index:
                    row = connection.execute(
                        text(
                            "SELECT container_title,journal_key,crossref_status,crossref_fetched_at FROM pdfs WHERE id=1"
                        )
                    ).one()
                    assert row[0] == "JOURNAL – TEST" and row[1] == normalize_journal_key(row[0])
                    assert row[2] == "pending" and row[3] == datetime(
                        2026, 10, 13, 1, 2, 3, tzinfo=timezone.utc
                    )
            # Direct SQL must normalize serial identifiers before resolving the registry.
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO journal_canonical_names VALUES ('MANAGEMENT SCIENCE')")
                )
                connection.execute(
                    text(
                        "INSERT INTO journal_issn_identifiers VALUES ('0025-1909','MANAGEMENT SCIENCE','{}')"
                    )
                )
                if not index:
                    connection.execute(
                        text(
                            "INSERT INTO users (id,username,password_hash,role,is_active,created_at) VALUES (1,'fixture','fixture-only','user',true,now())"
                        )
                    )
                row = connection.execute(
                    text(
                        "INSERT INTO pdfs (id,doi,sha256,original_name,size,storage_path,uploaded_by_id,uploaded_at,is_deleted,container_title,journal_issns_json) VALUES (2,'10.1234/issn',:sha,'fixture.pdf',20,'fixture',1,now(),false,'Source Name',:issns) RETURNING container_title,journal_issns_json"
                    ),
                    {"sha": "b" * 64, "issns": json.dumps(["00251909", "invalid"])},
                ).one()
                assert row[0] == "MANAGEMENT SCIENCE" and json.loads(row[1]) == ["0025-1909"]
        assert snapshot(engines[0], schemas[0]) == snapshot(engines[1], schemas[1]), (
            "Fresh/upgraded PostgreSQL schema differs"
        )
        print(
            "PostgreSQL contract OK: fresh/upgrade schema, UTC preservation, HTML entities, ISSNs and direct SQL."
        )
    finally:
        for engine in engines:
            engine.dispose()
        with admin.begin() as connection:
            for name in schemas:
                connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        admin.dispose()


if __name__ == "__main__":
    main()
