from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
import os
from pathlib import Path
import logging
from time import perf_counter
from uuid import uuid4
import shutil

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from netvault_server import __version__
from netvault_server.server.config import get_settings
from netvault_server.server.database import Base, engine, get_db
from netvault_server.server.deps import get_current_user, require_admin
from netvault_server.server.download_audit import record_completed_downloads
from netvault_server.server.doi import normalize_doi
from netvault_server.server.main_helpers import pdf_to_read, process_upload
from netvault_server.server.migrations import run_migrations
from netvault_server.server.models import Pdf, PdfFileAlias, UploadRecord, User, UserRole, utc_now
from netvault_server.server.queries import pdf_contains_query, pdf_read_options
from netvault_server.server.schemas import (
    LoginRequest,
    PasswordResetRequest,
    PdfAliasCreateRequest,
    PdfAliasCreateResponse,
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
    activity_is_allowed,
)
from netvault_server.server.storage import ensure_storage_dirs, object_path
from netvault_server.server.stats import invalidate_stats_cache, router as stats_router
from netvault_server.server.web import router as web_router

logger = logging.getLogger("netvault.requests")


def pdf_to_detail(pdf: Pdf, db: Session) -> PdfDetail:
    return PdfDetail(
        **pdf_to_read(pdf).model_dump(),
        storage_path=pdf.storage_path,
        upload_count=int(
            db.scalar(select(func.count()).select_from(UploadRecord).where(UploadRecord.pdf_id == pdf.id))
            or 0
        ),
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
    started = perf_counter()
    supplied_request_id = request.headers.get("x-request-id", "")
    request_id = (
        supplied_request_id
        if 0 < len(supplied_request_id) <= 128
        and all(char.isalnum() or char in "-_." for char in supplied_request_id)
        else uuid4().hex
    )
    response = await call_next(request)
    duration_ms = (perf_counter() - started) * 1000
    response.headers.setdefault("X-Request-ID", request_id)
    response.headers.setdefault("Server-Timing", f"app;dur={duration_ms:.1f}")
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response.headers.setdefault("Cache-Control", "private, no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if get_settings().secure_cookies:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "script-src 'self' https://static.cloudflareinsights.com; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' https://cloudflareinsights.com; object-src 'none'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    logger.info(
        "request method=%s path=%s status=%s duration_ms=%.1f request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        request_id,
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
    if shutil.disk_usage(storage_root).free < get_settings().min_storage_free_bytes:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage free space is below the safety threshold",
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
    request: Request,
    file: UploadFile = File(...),
    doi: str | None = Form(default=None),
    no_crossref: bool = Form(default=False),
    force: bool = Form(default=False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UploadResponse:
    if not activity_is_allowed(
        f"upload:{user.id}", get_settings().upload_rate_per_hour
    ):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Upload rate limit exceeded")
    idempotency_key = request.headers.get("idempotency-key")
    if idempotency_key and len(idempotency_key) > 128:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency key is too long")
    return await process_upload(file, doi, no_crossref, user, db, idempotency_key, force=force)


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
        .options(*pdf_read_options())
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
    dois: list[str] = []
    for raw_doi in payload.doi:
        try:
            normalized = normalize_doi(raw_doi)
        except ValueError:
            continue
        if normalized not in dois:
            dois.append(normalized)
    hash_pdfs = (
        db.scalars(
            select(Pdf)
            .options(*pdf_read_options())
            .where(Pdf.is_deleted.is_(False), Pdf.sha256.in_(hashes))
        ).all()
        if hashes
        else []
    )
    alias_rows = (
        db.execute(
            select(PdfFileAlias.sha256, Pdf)
            .join(Pdf, Pdf.id == PdfFileAlias.pdf_id)
            .options(*pdf_read_options())
            .where(Pdf.is_deleted.is_(False), PdfFileAlias.sha256.in_(hashes))
        ).all()
        if hashes
        else []
    )
    doi_pdfs = (
        db.scalars(
            select(Pdf)
            .options(*pdf_read_options())
            .where(Pdf.is_deleted.is_(False), Pdf.doi.in_(dois))
        ).all()
        if dois
        else []
    )
    existing = {pdf.sha256: pdf_to_read(pdf) for pdf in hash_pdfs}
    existing.update({sha256: pdf_to_read(pdf) for sha256, pdf in alias_rows})
    return Sha256ExistsResponse(
        existing=existing,
        existing_doi={pdf.doi: pdf_to_read(pdf) for pdf in doi_pdfs},
    )


@app.post("/pdfs/aliases", response_model=PdfAliasCreateResponse)
def register_pdf_file_aliases(
    payload: PdfAliasCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PdfAliasCreateResponse:
    claims: dict[str, str] = {}
    for claim in payload.aliases:
        sha256 = claim.sha256.lower()
        try:
            doi = normalize_doi(claim.doi)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        previous = claims.get(sha256)
        if previous is not None and previous != doi:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"SHA-256 {sha256} was assigned conflicting DOIs",
            )
        claims[sha256] = doi

    pdfs = db.scalars(
        select(Pdf)
        .options(*pdf_read_options())
        .where(
            Pdf.is_deleted.is_(False),
            (Pdf.doi.in_(set(claims.values()))) | (Pdf.sha256.in_(set(claims))),
        )
    ).all()
    pdf_by_doi = {pdf.doi: pdf for pdf in pdfs}
    canonical_by_sha = {pdf.sha256: pdf for pdf in pdfs}
    aliases = db.scalars(select(PdfFileAlias).where(PdfFileAlias.sha256.in_(set(claims)))).all()
    alias_by_sha = {alias.sha256: alias for alias in aliases}

    registered: dict[str, PdfRead] = {}
    for sha256, doi in claims.items():
        pdf = pdf_by_doi.get(doi)
        if pdf is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No active PDF found for DOI {doi}",
            )
        canonical = canonical_by_sha.get(sha256)
        if canonical is not None and canonical.id != pdf.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"SHA-256 {sha256} belongs to another PDF",
            )
        alias = alias_by_sha.get(sha256)
        if alias is not None and alias.pdf_id != pdf.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"SHA-256 alias {sha256} belongs to another PDF",
            )
        if canonical is None and alias is None:
            db.add(
                PdfFileAlias(
                    sha256=sha256,
                    pdf_id=pdf.id,
                    source="client-doi-confirmed",
                    asserted_by_id=user.id,
                )
            )
        registered[sha256] = pdf_to_read(pdf)
    db.commit()
    return PdfAliasCreateResponse(registered=registered)


@app.get("/pdfs/search", response_model=list[PdfRead])
def search_pdfs(
    q: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Sequence[PdfRead]:
    _ = user
    if len(q.strip()) > get_settings().search_query_max_chars:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Search query is too long")
    cleaned_query = q.strip()
    if not cleaned_query:
        return []
    try:
        exact_doi = normalize_doi(cleaned_query)
    except ValueError:
        exact_doi = None
    query = select(Pdf).options(*pdf_read_options()).where(
        Pdf.is_deleted.is_(False), Pdf.doi.is_not(None)
    )
    if exact_doi:
        query = query.where(Pdf.doi == exact_doi)
    else:
        query = query.where(pdf_contains_query(cleaned_query))
    pdfs = db.scalars(
        query.order_by(Pdf.uploaded_at.desc(), Pdf.id.desc()).offset(offset).limit(limit)
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
    return pdf_to_detail(find_pdf_by_doi(doi, db), db)


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
    return pdf_to_detail(find_pdf(identifier, db), db)


@app.get("/pdfs/{identifier}/download")
def download_pdf(
    identifier: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    pdf = find_pdf(identifier, db)
    return send_pdf_file(pdf, user, db)


def send_pdf_file(pdf: Pdf, user: User, db: Session) -> FileResponse:
    if not activity_is_allowed(
        f"download:{user.id}", get_settings().download_rate_per_hour
    ):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Download rate limit exceeded")
    path = object_path(pdf.sha256)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Stored PDF missing")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=Path(pdf.original_name).name,
        background=BackgroundTask(record_completed_downloads, [pdf.id], user.id),
        headers={"ETag": f'"{pdf.sha256}"'},
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
