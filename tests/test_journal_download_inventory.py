import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from netvault_server.server.database import Base
from netvault_server.server.journal_export import inventory_rows_by_journal
from netvault_server.server.models import Pdf, User


def test_journal_lookup_is_exact_and_excludes_deleted():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id=1, username="tester", password_hash="test"))
        db.flush()
        db.add_all([
            Pdf(doi="10.1234/one", sha256="a" * 64, journal_key="management science",
                container_title="Management Science", original_name="one.pdf", storage_path="one.pdf", uploaded_by_id=1,
                size=10),
            Pdf(doi="10.1234/two", sha256="b" * 64, journal_key="management science review",
                container_title="Management Science Review", original_name="two.pdf",
                storage_path="two.pdf", uploaded_by_id=1, size=10),
        ])
        db.commit()
        rows = inventory_rows_by_journal(db, " Management Science ", include_aliases=False)
        assert [row[0] for row in rows] == ["10.1234/one"]
        with pytest.raises(HTTPException) as error:
            inventory_rows_by_journal(db, "Unknown Journal")
        assert error.value.status_code == 404
