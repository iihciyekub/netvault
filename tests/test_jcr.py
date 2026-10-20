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
