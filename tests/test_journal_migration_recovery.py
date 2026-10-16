"""Regression: an old migration marker must not hide a missing user preference column."""
from sqlalchemy import create_engine, inspect, text

from netvault_server.server.migrations import run_migrations


def test_dashboard_preference_column_repairs_schema_version_drift():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(80) NOT NULL)"))
        connection.execute(text("INSERT INTO users (id, username) VALUES (1, 'admin')"))
        connection.execute(text(
            "CREATE TABLE netvault_schema_version (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        ))
        connection.execute(text("INSERT INTO netvault_schema_version (id, version) VALUES (1, 11)"))
        connection.execute(text(
            "CREATE TABLE user_journal_filters "
            "(id INTEGER PRIMARY KEY, user_id INTEGER, filter_key VARCHAR(32), journals_json TEXT)"
        ))
        connection.execute(text(
            "INSERT INTO user_journal_filters (id, user_id, filter_key, journals_json) "
            "VALUES (1, 1, 'custom_aadefa5c8ab1', "
            """'{"name":"Intercultural","journals":["SYSTEM","LANGUAGE CULTURE AND CURRICULUM"]}')"""
        ))
    for _ in range(2):
        run_migrations(engine)
        assert "dashboard_filter_key" in {
            item["name"] for item in inspect(engine).get_columns("users")
        }
        with engine.connect() as connection:
            assert connection.scalar(text(
                "SELECT dashboard_filter_key FROM users WHERE id=1"
            )) == "utd24"
            assert connection.scalar(text(
                "SELECT version FROM netvault_schema_version WHERE id=1"
            )) == 11
            assert connection.scalar(text(
                "SELECT journals_json FROM user_journal_filters WHERE id=1"
            )) == '{"name":"Intercultural","journals":["SYSTEM","LANGUAGE CULTURE AND CURRICULUM"]}'
    engine.dispose()
