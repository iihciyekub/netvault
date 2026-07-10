from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from netvault_server.server.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UserRole(StrEnum):
    admin = "admin"
    user = "user"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.user, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    uploads: Mapped[list["UploadRecord"]] = relationship(back_populates="user")


class Pdf(Base):
    __tablename__ = "pdfs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doi: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    doi_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    doi_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    authors: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    journal_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crossref_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    crossref_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    crossref_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    uploaded_by: Mapped[User] = relationship(foreign_keys=[uploaded_by_id])
    uploads: Mapped[list["UploadRecord"]] = relationship(back_populates="pdf")


class UploadRecord(Base):
    __tablename__ = "upload_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pdf_id: Mapped[int] = mapped_column(ForeignKey("pdfs.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    pdf: Mapped[Pdf] = relationship(back_populates="uploads")
    user: Mapped[User] = relationship(back_populates="uploads")


class DownloadRecord(Base):
    __tablename__ = "download_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pdf_id: Mapped[int] = mapped_column(ForeignKey("pdfs.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@event.listens_for(Pdf, "before_insert")
@event.listens_for(Pdf, "before_update")
def set_pdf_journal_key(_mapper, _connection, target: Pdf) -> None:
    from netvault_server.server.journal_filters import normalize_journal_name

    target.journal_key = normalize_journal_name(target.container_title) or None
