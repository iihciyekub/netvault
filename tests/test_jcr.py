import io
import json
import zipfile
import pytest
from netvault_server.server import jcr


def archive(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, value in entries.items():
            z.writestr(name, json.dumps(value))
    buf.seek(0)
    return buf


def row(name="TEST", year="2025", cats=None):
    return dict(
        journalName=name,
        jcrYear=year,
        issn="1234-567X",
        eissn="8765-4321",
        publisher="Publisher",
        jif2019="2.5",
        jci="1.2",
        categoryQuartiles=cats
        or [dict(category="ENGINEERING, CIVIL", edition="SCIE, SSCI", quartile="Q1")],
    )


def test_hidden_batches_duplicates_lossless_and_year_preservation(tmp_path):
    a = row("A")
    b = row("B")
    b["newFutureField"] = {"preserved": True}
    report = jcr.import_zip(
        tmp_path,
        archive(
            {
                "2025/JCR_1.json": [a],
                "2025/.wosaide-jcr/run/JCR_2.json": [b, a],
                "2025/.wosaide-jcr/task.json": {"year": 2025, "totalCount": 2},
                "__MACOSX/broken.json": None,
            }
        ),
    )
    assert report["journals"] == 2 and report["duplicates"] == 1 and report["files"] == 2
    result = jcr.query(tmp_path, year="2025", edition="SSCI", quartile="Q1")
    assert result["total"] == 2 and result["rows"][1]["newFutureField"] == {"preserved": True}
    jcr.import_zip(tmp_path, archive({"2024/JCR.json": [row("Old", "2024")]}))
    jcr.import_zip(tmp_path, archive({"2025/JCR.json": [a]}))
    assert jcr.facets(tmp_path)["years"] == ["2025", "2024"]
    assert jcr.query(tmp_path, year="2024")["total"] == 1


def test_journal_details_exact_latest_and_missing(tmp_path):
    assert jcr.journal_details(tmp_path, "Test") is None
    assert not (tmp_path / "jcr").exists()
    old = row("Test", "2024")
    current = row("Test", "2025", cats=[
        dict(category="ENGINEERING, CIVIL", edition="SCIE, SSCI", quartile="Q1"),
        dict(category="MATERIALS SCIENCE, MULTIDISCIPLINARY", edition="SCIE", quartile="Q2"),
    ])
    current["publisher"] = "Current publisher"
    jcr.import_zip(tmp_path, archive({"JCR.json": [old, current, row("Test Extended")]}))
    details = jcr.journal_details(tmp_path, "  tEsT  ")
    assert details == dict(
        journal_name="Test", year="2025", publisher="Current publisher",
        issn="1234-567X", eissn="8765-4321",
        categories=["ENGINEERING, CIVIL", "MATERIALS SCIENCE, MULTIDISCIPLINARY"],
        indexes=["SCIE", "SSCI"],
    )
    assert jcr.journal_details(tmp_path, "Tes") is None
    assert jcr.journal_details(tmp_path, "' OR 1=1 --") is None
    duplicate = {**current, "issn": "1111-1111"}
    jcr.import_zip(tmp_path, archive({"JCR.json": [current, duplicate]}))
    assert jcr.journal_details(tmp_path, "Test") is None


def test_same_category_matching_and_unranked(tmp_path):
    a = row(
        cats=[
            dict(category="SCIENCE", edition="SCIE", quartile="Q1"),
            dict(category="SOCIAL", edition="SSCI", quartile="Q3"),
        ]
    )
    b = row("Unranked", cats=[dict(category="ART", edition="AHCI", quartile="N/A")])
    jcr.import_zip(tmp_path, archive({"JCR.json": [a, b]}))
    assert jcr.query(tmp_path, year="2025", edition="SSCI", quartile="Q1")["total"] == 0
    assert jcr.query(tmp_path, year="2025", edition="SSCI", quartile="Q3")["total"] == 1
    assert jcr.query(tmp_path, year="2025", quartile="N/A")["total"] == 1
    assert jcr.query(tmp_path, year="2025", q="1234567X")["total"] == 2
    assert jcr.query(tmp_path, year="2025", q="%'; DELETE FROM journals;")["total"] == 0


def test_invalid_import_preserves_existing_year(tmp_path):
    jcr.import_zip(tmp_path, archive({"JCR.json": [row()]}))
    with pytest.raises(ValueError, match="Incomplete"):
        jcr.import_zip(
            tmp_path,
            archive({"JCR.json": [row("New")], "task.json": {"year": 2025, "totalCount": 2}}),
        )
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        changed = row()
        changed["jif2019"] = "9"
        jcr.import_zip(tmp_path, archive({"JCR1.json": [row()], "JCR2.json": [changed]}))
    assert jcr.query(tmp_path, year="2025")["rows"][0]["journalName"] == "TEST"


def test_numeric_sort_filter_and_pagination(tmp_path):
    rows = [row("A"), row("B"), row("C")]
    for r, v in zip(rows, ["10", "2", "N/A"]):
        r["jif2019"] = v
    jcr.import_zip(tmp_path, archive({"JCR.json": rows}))
    r = jcr.query(tmp_path, year="2025", sort="jif", direction="desc", size=1, page=1)
    assert r["rows"][0]["journalName"] == "A" and r["pages"] == 3
    assert jcr.query(tmp_path, year="2025", jif_min=3)["total"] == 1
    assert (
        jcr.query(tmp_path, year="2025", sort="jif", direction="desc", page=99, size=1)["rows"][0][
            "journalName"
        ]
        == "C"
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    import sys
    from fastapi.testclient import TestClient

    for k, v in dict(
        NETVAULT_DATABASE_URL=f"sqlite:///{tmp_path}/api.db",
        NETVAULT_STORAGE_ROOT=str(tmp_path / "store"),
        NETVAULT_SECRET_KEY="jcr-test-key",
        NETVAULT_BASE_PATH="",
        NETVAULT_BOOTSTRAP_ADMIN="admin",
        NETVAULT_BOOTSTRAP_ADMIN_PASSWORD="admin-pass",
    ).items():
        monkeypatch.setenv(k, v)
    for module in list(sys.modules):
        if module.startswith("netvault_server.server"):
            del sys.modules[module]
    from netvault_server.server.main import app

    with TestClient(app) as conn:
        yield conn


def test_authenticated_import_csrf_and_browser_endpoints(client):
    assert client.get("/web/journal-details", params={"name": "TEST"}).status_code == 401
    assert client.get("/web/jcr", follow_redirects=False).status_code == 303
    assert client.get("/web/jcr/facets").status_code == 401
    csrf = client.get("/web/login").cookies["netvault_csrf"]
    client.post("/web/login", data=dict(username="admin", password="admin-pass", csrf_token=csrf))
    html = client.get("/web/jcr")
    assert html.status_code == 200 and "data-no-pjax" in html.text
    payload = archive({"JCR.json": [row()]})
    assert (
        client.post(
            "/web/jcr/import",
            files={"file": ("2025.zip", payload.getvalue())},
            data={"csrf_token": "wrong"},
        ).status_code
        == 403
    )
    response = client.post(
        "/web/jcr/import",
        files={"file": ("2025.zip", payload.getvalue())},
        data={"csrf_token": csrf},
    )
    assert response.status_code == 200 and response.json()["report"]["journals"] == 1
    details = client.get("/web/journal-details", params={"name": "test"})
    assert details.status_code == 200 and details.json()["journal"]["issn"] == "1234-567X"
    assert client.get("/web/journal-details", params={"name": "absent"}).json() == {"journal": None}
    assert client.get("/web/journal-details", params={"name": "x" * 301}).status_code == 422
    assert 'data-journal-details-url="/web/journal-details"' in client.get("/web").text
    assert client.get("/web/jcr/facets").json()["total"] == 1
    assert client.get("/web/jcr/journals?year=2025&edition=SSCI&quartile=Q1").json()["total"] == 1
    assert client.get("/web/jcr/journals?year=2025&size=201").status_code == 422
    # A regular reader can browse, but cannot import shared catalogues.
    from netvault_server.server.database import SessionLocal
    from netvault_server.server.models import User, UserRole
    from sqlalchemy import select

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == "admin"))
        user.role = UserRole.user
        db.commit()
    assert client.get("/web/jcr/facets").status_code == 200
    assert (
        client.post(
            "/web/jcr/import",
            files={"file": ("2025.zip", payload.getvalue())},
            data={"csrf_token": csrf},
        ).status_code
        == 403
    )


def test_acronym_and_keywords_with_index_filter(tmp_path):
    journals = [
        row("International Journal of Intercultural Relations"),
        row("The Journal of Applied Linguistics"),
        row("Journal of Other Research"),
    ]
    jcr.import_zip(tmp_path, archive({"JCR.json": journals}))
    for q in ["IJIR", "ijir", "I.J.I.R.", "IJIR Publisher"]:
        result = jcr.query(tmp_path, year="2025", q=q, edition="SSCI")
        assert (
            result["total"] == 1 and result["rows"][0]["journalName"] == journals[0]["journalName"]
        )
    assert (
        jcr.query(tmp_path, year="2025", q="JAL")["rows"][0]["journalName"]
        == journals[1]["journalName"]
    )
    assert jcr.query(tmp_path, year="2025", q="intercultural")["total"] == 1


def test_global_sort_for_every_column_and_nulls_last(tmp_path):
    journals = []
    for i in range(230):
        r = row(f"Journal {i:03d}")
        r.update(
            issn=f"{9999 - i:04d}-1234",
            eissn=f"{i:04d}-5678",
            jif5Years=str(i),
            jciRank=f"{i + 1}/300",
            jif2019=str(i),
        )
        journals.append(r)
    missing = row("Missing")
    missing.update(jif5Years="N/A", jciRank="N/A", issn="N/A", eissn="N/A")
    journals.append(missing)
    jcr.import_zip(tmp_path, archive({"JCR.json": journals}))
    for key in jcr.SORT_COLUMNS:
        for direction in ["asc", "desc"]:
            result = jcr.query(tmp_path, year="2025", sort=key, direction=direction, size=50)
            assert result["total"] == 231 and len(result["rows"]) == 50, key
    for key in ["jif5Years", "jciRank", "jif"]:
        result = jcr.query(tmp_path, year="2025", sort=key, direction="desc", size=50)
        assert result["rows"][0]["journalName"] == "Journal 229"
        second = jcr.query(tmp_path, year="2025", sort=key, direction="desc", size=50, page=2)
        assert second["rows"][0]["journalName"] == "Journal 179"
    assert (
        jcr.query(tmp_path, year="2025", sort="jif5Years", direction="desc", size=50, page=5)[
            "rows"
        ][-1]["journalName"]
        == "Missing"
    )
    assert (
        jcr.query(tmp_path, year="2025", sort="jciRank", direction="asc")["rows"][0]["journalName"]
        == "Journal 000"
    )
    assert (
        jcr.query(tmp_path, year="2025", sort="issn", direction="asc")["rows"][0]["journalName"]
        == "Journal 229"
    )


def test_installed_catalog_backfill_preserves_raw_and_future_imports(tmp_path):
    journal = row("International Journal of Intercultural Relations")
    journal["jif5Years"] = "10.5"
    jcr.import_zip(tmp_path, archive({"JCR.json": [journal]}))
    with jcr.connect(tmp_path) as db:
        before = db.execute("SELECT id,raw FROM journals").fetchall()
        db.execute("DROP TABLE journal_lookup")
        db.execute("DELETE FROM jcr_meta")
        db.commit()
    assert jcr.query(tmp_path, year="2025", q="IJIR", sort="jif5Years")["total"] == 1
    with jcr.connect(tmp_path) as db:
        assert [tuple(r) for r in db.execute("SELECT id,raw FROM journals")] == [
            tuple(r) for r in before
        ]
    jcr.import_zip(
        tmp_path, archive({"JCR.json": [journal, row("Journal of Applied Linguistics")]})
    )
    assert jcr.query(tmp_path, year="2025", q="JAL")["total"] == 1


def timestamp_archive(year, date):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("data.json", date), json.dumps([row(year=year)]))
        # Later checkpoint timestamps must not masquerade as journal data time.
        z.writestr(
            zipfile.ZipInfo("task.json", (2099, 1, 1, 0, 0, 0)),
            json.dumps({"year": year, "totalCount": 1}),
        )
    buf.seek(0)
    return buf


def test_source_times_follow_json_and_selected_year(tmp_path):
    jcr.import_zip(tmp_path, timestamp_archive("2025", (2026, 10, 20, 20, 42, 12)))
    jcr.import_zip(tmp_path, timestamp_archive("2024", (2025, 9, 2, 13, 4, 6)))
    time = jcr.facets(tmp_path, "2025")["dataTime"]
    assert time["value"] == "2026-10-20T20:42:12+08:00" and time["kind"] == "modified"
    assert jcr.facets(tmp_path, "2024")["dataTime"]["value"] == "2025-09-02T13:04:06+08:00"
    assert jcr.facets(tmp_path, "2025")["latestImport"]["years"] == {"2025": 1}


def test_zip_extended_utc_and_ntfs_creation_times():
    import struct
    from datetime import datetime, timezone

    info = zipfile.ZipInfo("batch.json", (2026, 10, 20, 1, 0, 0))
    stamp = datetime(2026, 10, 20, 12, 42, 12, tzinfo=timezone.utc)
    info.extra = struct.pack("<HHBI", 0x5455, 5, 1, int(stamp.timestamp()))
    assert jcr.zip_file_time(info)["value"] == "2026-10-20T20:42:12+08:00"
    created = datetime(2026, 10, 19, 10, 20, 30, tzinfo=timezone.utc)
    ticks = int((created - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds()) * 10000000
    payload = struct.pack("<IHHQQQ", 0, 1, 24, ticks, ticks, ticks)
    info.extra += struct.pack("<HH", 0x000A, len(payload)) + payload
    assert jcr.zip_file_time(info) == {"value": "2026-10-19T18:20:30+08:00", "kind": "created"}


def test_unknown_zip_time_does_not_fall_back_to_import_time(tmp_path):
    report = jcr.import_zip(tmp_path, timestamp_archive("2025", (1980, 1, 1, 0, 0, 0)))
    assert report["jsonTimesByYear"] == {}
    assert jcr.facets(tmp_path, "2025")["dataTime"] is None
