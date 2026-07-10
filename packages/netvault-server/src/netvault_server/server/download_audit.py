from sqlalchemy.orm import Session

from netvault_server.server.database import engine
from netvault_server.server.models import DownloadRecord


def record_completed_downloads(pdf_ids: list[int], user_id: int) -> None:
    if not pdf_ids:
        return
    with Session(engine) as db:
        db.add_all(DownloadRecord(pdf_id=pdf_id, user_id=user_id) for pdf_id in pdf_ids)
        db.commit()
