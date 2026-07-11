from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, event
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
    dashboard_journal_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    uploads: Mapped[list["UploadRecord"]] = relationship(back_populates="user")
    journal_filters: Mapped[list["UserJournalFilter"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class UserJournalFilter(Base):
    __tablename__ = "user_journal_filters"
    __table_args__ = (UniqueConstraint("user_id", "filter_key", name="uq_user_journal_filter"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    filter_key: Mapped[str] = mapped_column(String(32), nullable=False)
    journals_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    user: Mapped[User] = relationship(back_populates="journal_filters")


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
    file_aliases: Mapped[list["PdfFileAlias"]] = relationship(back_populates="pdf")


class PdfFileAlias(Base):
    __tablename__ = "pdf_file_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    pdf_id: Mapped[int] = mapped_column(ForeignKey("pdfs.id"), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    asserted_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    asserted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    pdf: Mapped[Pdf] = relationship(back_populates="file_aliases")
    asserted_by: Mapped[User] = relationship()


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
