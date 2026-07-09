from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import inspect, or_, select, text
from sqlalchemy.orm import Session

from netvault.server.config import get_settings
from netvault.server.database import Base, engine, get_db
from netvault.server.deps import get_current_user, require_admin
from netvault.server.doi import extract_doi_from_pdf, normalize_doi
from netvault.server.models import DownloadRecord, Pdf, UploadRecord, User, UserRole, utc_now
from netvault.server.schemas import (
    LoginRequest,
    PasswordResetRequest,
    PdfDetail,
    PdfRead,
    TokenResponse,
    UploadResponse,
    UserCreateRequest,
    UserRead,
)
from netvault.server.security import create_access_token, hash_password, verify_password
from netvault.server.storage import ensure_storage_dirs, object_path, store_pdf

def pdf_to_read(pdf: Pdf) -> PdfRead:
    return PdfRead(
        id=pdf.id,
        doi=pdf.doi,
        sha256=pdf.sha256,
        original_name=pdf.original_name,
        size=pdf.size,
        uploaded_at=pdf.uploaded_at,
        uploaded_by=pdf.uploaded_by.username,
    )


def pdf_to_detail(pdf: Pdf) -> PdfDetail:
    return PdfDetail(
        **pdf_to_read(pdf).model_dump(),
        storage_path=pdf.storage_path,
        upload_count=len(pdf.uploads),
    )


def initialize_app() -> None:
    ensure_storage_dirs()
    Base.metadata.create_all(bind=engine)
    ensure_doi_column()
    settings = get_settings()
    if settings.bootstrap_admin and settings.bootstrap_admin_password:
        with Session(engine) as db:
            existing = db.scalar(select(User).where(User.username == settings.bootstrap_admin))
            if existing is None:
                db.add(
                    User(
                        username=settings.bootstrap_admin,
                        password_hash=hash_password(settings.bootstrap_admin_password),
                        role=UserRole.admin,
                    )
                )
                db.commit()


def ensure_doi_column() -> None:
    inspector = inspect(engine)
    if "pdfs" not in inspector.get_table_names():
        return
    column_names = {column["name"] for column in inspector.get_columns("pdfs")}
    with engine.begin() as connection:
        if "doi" not in column_names:
            connection.execute(text("ALTER TABLE pdfs ADD COLUMN doi VARCHAR(255)"))
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_pdfs_doi ON pdfs (doi)"))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    initialize_app()
    yield


app = FastAPI(title="NetVault", version="0.2.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.username == payload.username))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return TokenResponse(access_token=create_access_token(user.username))


@app.post("/auth/logout")
def logout(_: User = Depends(get_current_user)) -> dict[str, str]:
    return {"status": "ok"}


@app.get("/me", response_model=UserRead)
def me(user: User = Depends(get_current_user)) -> User:
    return user


@app.post("/pdfs/upload", response_model=UploadResponse)
async def upload_pdf(
    file: UploadFile = File(...),
    doi: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UploadResponse:
    sha256, size, relative_path, object_deduplicated = await store_pdf(file)
    try:
        normalized_doi = normalize_doi(doi) if doi else extract_doi_from_pdf(object_path(sha256))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not normalized_doi:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No DOI found in PDF. Pass --doi DOI when uploading.",
        )

    pdf_by_doi = db.scalar(select(Pdf).where(Pdf.doi == normalized_doi))
    pdf_by_sha = db.scalar(select(Pdf).where(Pdf.sha256 == sha256))
    if pdf_by_doi is not None and pdf_by_doi.sha256 != sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"DOI {normalized_doi} is already linked to a different PDF",
        )
    if pdf_by_sha is not None and pdf_by_sha.doi != normalized_doi:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This PDF is already linked to DOI {pdf_by_sha.doi}",
        )

    pdf = pdf_by_doi or pdf_by_sha
    created_pdf = False

    if pdf is None:
        pdf = Pdf(
            doi=normalized_doi,
            sha256=sha256,
            original_name=file.filename or f"{sha256}.pdf",
            size=size,
            storage_path=relative_path,
            uploaded_by_id=user.id,
        )
        db.add(pdf)
        db.flush()
        created_pdf = True
    elif pdf.is_deleted:
        pdf.is_deleted = False
        pdf.deleted_at = None
        pdf.deleted_by_id = None

    db.add(
        UploadRecord(
            pdf_id=pdf.id,
            user_id=user.id,
            original_name=file.filename or pdf.original_name,
            size=size,
        )
    )
    db.commit()
    db.refresh(pdf)
    return UploadResponse(pdf=pdf_to_read(pdf), deduplicated=object_deduplicated or not created_pdf)


@app.get("/pdfs", response_model=list[PdfRead])
def list_pdfs(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Sequence[PdfRead]:
    _ = user
    pdfs = db.scalars(
        select(Pdf)
        .where(Pdf.is_deleted.is_(False), Pdf.doi.is_not(None))
        .order_by(Pdf.uploaded_at.desc())
    ).all()
    return [pdf_to_read(pdf) for pdf in pdfs]


@app.get("/pdfs/search", response_model=list[PdfRead])
def search_pdfs(
    q: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Sequence[PdfRead]:
    _ = user
    pattern = f"%{q}%"
    pdfs = db.scalars(
        select(Pdf)
        .join(User, Pdf.uploaded_by_id == User.id)
        .where(Pdf.is_deleted.is_(False), Pdf.doi.is_not(None))
        .where(
            or_(
                Pdf.doi.ilike(pattern),
                Pdf.original_name.ilike(pattern),
                Pdf.sha256.ilike(pattern),
                User.username.ilike(pattern),
            )
        )
        .order_by(Pdf.uploaded_at.desc())
    ).all()
    return [pdf_to_read(pdf) for pdf in pdfs]


def find_pdf(identifier: str, db: Session) -> Pdf:
    pdf: Pdf | None = None
    if identifier.isdigit():
        pdf = db.get(Pdf, int(identifier))
    if pdf is None and len(identifier) == 64:
        pdf = db.scalar(select(Pdf).where(Pdf.sha256 == identifier))
    if pdf is None or pdf.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF not found")
    return pdf


def find_pdf_by_doi(doi: str, db: Session) -> Pdf:
    try:
        normalized_doi = normalize_doi(doi)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    pdf = db.scalar(select(Pdf).where(Pdf.doi == normalized_doi))
    if pdf is None or pdf.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF not found for DOI")
    return pdf


@app.get("/pdfs/by-doi", response_model=PdfDetail)
def get_pdf_by_doi(
    doi: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PdfDetail:
    _ = user
    return pdf_to_detail(find_pdf_by_doi(doi, db))


@app.get("/pdfs/by-doi/download")
def download_pdf_by_doi(
    doi: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    pdf = find_pdf_by_doi(doi, db)
    return send_pdf_file(pdf, user, db)


@app.get("/pdfs/{identifier}", response_model=PdfDetail)
def get_pdf(
    identifier: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PdfDetail:
    _ = user
    return pdf_to_detail(find_pdf(identifier, db))


@app.get("/pdfs/{identifier}/download")
def download_pdf(
    identifier: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    pdf = find_pdf(identifier, db)
    return send_pdf_file(pdf, user, db)


def send_pdf_file(pdf: Pdf, user: User, db: Session) -> FileResponse:
    path = object_path(pdf.sha256)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Stored PDF missing")
    db.add(DownloadRecord(pdf_id=pdf.id, user_id=user.id))
    db.commit()
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=Path(pdf.original_name).name,
    )


@app.post("/admin/users", response_model=UserRead)
def create_user(
    payload: UserCreateRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> User:
    existing = db.scalar(select(User).where(User.username == payload.username))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/admin/users/{username}/reset-password", response_model=UserRead)
def reset_password(
    username: str,
    payload: PasswordResetRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> User:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.password_hash = hash_password(payload.password)
    db.commit()
    db.refresh(user)
    return user


@app.post("/admin/users/{username}/deactivate", response_model=UserRead)
def deactivate_user(
    username: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> User:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.is_active = False
    db.commit()
    db.refresh(user)
    return user


@app.delete("/admin/pdfs/by-doi", response_model=PdfRead)
def delete_pdf_by_doi(
    doi: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> PdfRead:
    pdf = find_pdf_by_doi(doi, db)
    pdf.is_deleted = True
    pdf.deleted_at = utc_now()
    pdf.deleted_by_id = admin.id
    db.commit()
    db.refresh(pdf)
    return pdf_to_read(pdf)


@app.delete("/admin/pdfs/{identifier}", response_model=PdfRead)
def delete_pdf(
    identifier: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> PdfRead:
    pdf = find_pdf(identifier, db)
    pdf.is_deleted = True
    pdf.deleted_at = utc_now()
    pdf.deleted_by_id = admin.id
    db.commit()
    db.refresh(pdf)
    return pdf_to_read(pdf)
