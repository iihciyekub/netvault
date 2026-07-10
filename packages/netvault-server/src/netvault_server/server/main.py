from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from netvault_server import __version__
from netvault_server.server.config import get_settings
from netvault_server.server.database import Base, engine, get_db
from netvault_server.server.deps import get_current_user, require_admin
from netvault_server.server.doi import normalize_doi
from netvault_server.server.main_helpers import pdf_to_read, process_upload
from netvault_server.server.migrations import run_migrations
from netvault_server.server.models import DownloadRecord, Pdf, User, UserRole, utc_now
from netvault_server.server.schemas import (
    LoginRequest,
    PasswordResetRequest,
    PdfDetail,
    PdfRead,
    Sha256ExistsRequest,
    Sha256ExistsResponse,
    TokenResponse,
    UploadResponse,
    UserCreateRequest,
    UserRead,
)
from netvault_server.server.security import (
    DUMMY_PASSWORD_HASH,
    clear_login_failures,
    create_access_token,
    hash_password,
    login_is_allowed,
    password_needs_rehash,
    record_login_failure,
    verify_password,
)
from netvault_server.server.storage import ensure_storage_dirs, object_path
from netvault_server.server.stats import invalidate_stats_cache, router as stats_router
from netvault_server.server.web import router as web_router


def pdf_to_detail(pdf: Pdf) -> PdfDetail:
    return PdfDetail(
        **pdf_to_read(pdf).model_dump(),
        storage_path=pdf.storage_path,
        upload_count=len(pdf.uploads),
        doi_evidence=pdf.doi_evidence,
    )


def initialize_app() -> None:
    ensure_storage_dirs()
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
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


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    initialize_app()
    yield


app = FastAPI(title="NetVault", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(stats_router)
app.include_router(web_router)


@app.middleware("http")
async def add_static_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    storage_root = get_settings().storage_root
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from exc
    if not storage_root.exists() or not os.access(storage_root, os.W_OK):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage is unavailable",
        )
    return {"status": "ready"}


@app.post("/auth/login", response_model=TokenResponse)
def login(request: Request, payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    client_host = request.client.host if request.client else "unknown"
    login_key = f"{client_host}:{payload.username.casefold()}"
    if not login_is_allowed(login_key):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")
    user = db.scalar(select(User).where(User.username == payload.username))
    candidate_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_valid = verify_password(payload.password, candidate_hash)
    if user is None or not user.is_active or not password_valid:
        record_login_failure(login_key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    clear_login_failures(login_key)
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
        db.commit()
    return TokenResponse(access_token=create_access_token(user.username, user.token_version))


@app.post("/auth/logout")
def logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, str]:
    user.token_version += 1
    db.commit()
    return {"status": "ok"}


@app.get("/me", response_model=UserRead)
def me(user: User = Depends(get_current_user)) -> User:
    return user


@app.post("/pdfs/upload", response_model=UploadResponse)
async def upload_pdf(
    file: UploadFile = File(...),
    doi: str | None = Form(default=None),
    no_crossref: bool = Form(default=False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UploadResponse:
    return await process_upload(file, doi, no_crossref, user, db)


@app.get("/pdfs", response_model=list[PdfRead])
def list_pdfs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Sequence[PdfRead]:
    _ = user
    pdfs = db.scalars(
        select(Pdf)
        .where(Pdf.is_deleted.is_(False), Pdf.doi.is_not(None))
        .order_by(Pdf.uploaded_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [pdf_to_read(pdf) for pdf in pdfs]


@app.post("/pdfs/exists", response_model=Sha256ExistsResponse)
def existing_pdfs_by_sha256(
    payload: Sha256ExistsRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Sha256ExistsResponse:
    _ = user
    hashes = sorted(
        {
            sha256.lower()
            for sha256 in payload.sha256
            if len(sha256) == 64 and all(char in "0123456789abcdefABCDEF" for char in sha256)
        }
    )
    if not hashes:
        return Sha256ExistsResponse(existing={})
    pdfs = db.scalars(select(Pdf).where(Pdf.is_deleted.is_(False), Pdf.sha256.in_(hashes))).all()
    return Sha256ExistsResponse(existing={pdf.sha256: pdf_to_read(pdf) for pdf in pdfs})


@app.get("/pdfs/search", response_model=list[PdfRead])
def search_pdfs(
    q: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
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
                Pdf.title.ilike(pattern),
                Pdf.authors.ilike(pattern),
                Pdf.container_title.ilike(pattern),
                Pdf.publisher.ilike(pattern),
                Pdf.original_name.ilike(pattern),
                Pdf.sha256.ilike(pattern),
                User.username.ilike(pattern),
            )
        )
        .order_by(Pdf.uploaded_at.desc())
        .offset(offset)
        .limit(limit)
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
    user.token_version += 1
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
    invalidate_stats_cache()
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
    invalidate_stats_cache()
    db.refresh(pdf)
    return pdf_to_read(pdf)
