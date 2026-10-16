from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
import os
import re
from pathlib import Path
import logging
from time import perf_counter
from uuid import uuid4
import shutil

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from netvault_server import __version__
from netvault_server.server.config import get_settings
from netvault_server.server.database import Base, engine, get_db
from netvault_server.server.deps import get_current_user, require_admin
from netvault_server.server.doi import normalize_doi
from netvault_server.server.main_helpers import pdf_to_read, process_upload
from netvault_server.server.migrations import run_migrations
from netvault_server.server.doi_correction import correct_pdf_doi as correct_pdf_doi_service
from netvault_server.server.models import (
    Pdf,
    UploadRecord,
    User,
    UserRole,
    utc_now,
)
from netvault_server.server.queries import pdf_read_options
from netvault_server.server.pdf_service import (existing_pdfs, search_pdfs_query,
    find_pdf as find_pdf_service, find_pdf_by_doi as find_pdf_by_doi_service,
    send_pdf_file as send_pdf_file_service, doi_correction_history)
from netvault_server.server import accounts
from netvault_server.server.schemas import (
    LoginRequest,
    PasswordResetRequest,
    PdfAliasCreateRequest,
    PdfAliasCreateResponse,
    PdfDoiCorrectionRead,
    PdfDoiCorrectionRequest,
    PdfDoiCorrectionResponse,
    PdfDetail,
    PdfRead,
    PdfDoiVerification,
    Sha256ExistsRequest,
    Sha256ExistsResponse,
    TokenResponse,
    UploadResponse,
    UserCreateRequest,
    UserRead,
)
from netvault_server.server.security import (
    create_access_token,
    hash_password,
    activity_is_allowed,
)
from netvault_server.server.storage import ensure_storage_dirs, store_pdf, object_path
from netvault_server.server.doi_writeback import verify_pdf_doi_writeback
from netvault_server.server.stats import invalidate_stats_cache, router as stats_router
from netvault_server.server.journal_export import router as journal_export_router
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
    from netvault_server.server.journal_ranks import recover_ranking_batches

    recover_ranking_batches()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    initialize_app()
    yield


app = FastAPI(title="NetVault", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(stats_router)
app.include_router(web_router)
app.include_router(journal_export_router)


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
    if request.url.path in ("/static/app.js", "/static/styles.css", "/static/theme.js"):
        # First-party UI may be hotfixed between packaged releases. Revalidate
        # rather than pinning stale handlers for a year behind an unchanged URL.
        response.headers["Cache-Control"] = "public, no-cache, must-revalidate"
    elif request.url.path.startswith("/static/"):
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
    user = accounts.authenticate(db, payload.username, payload.password,
                                 request.client.host if request.client else "unknown")
    return TokenResponse(access_token=create_access_token(user.username, user.token_version))


@app.post("/auth/logout")
def logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, str]:
    accounts.revoke_sessions(db, user)
    return {"status": "ok"}


@app.get("/me", response_model=UserRead)
def me(user: User = Depends(get_current_user)) -> User:
    return user


@app.post("/pdfs/upload", response_model=UploadResponse)
async def upload_pdf(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    doi: str | None = Form(default=None),
    doi_source: str | None = Form(default=None),
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
    expected_sha256 = request.headers.get("x-netvault-expected-sha256")
    if expected_sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise HTTPException(status_code=400, detail="Expected PDF SHA-256 must contain 64 hexadecimal characters")
    timings: dict[str, float] = {}
    result = await process_upload(
        file,
        doi,
        no_crossref,
        user,
        db,
        idempotency_key,
        force=force,
        doi_source=doi_source,
        timings=timings,
        expected_sha256=expected_sha256,
    )
    response.headers["Server-Timing"] = ", ".join(
        f"{name};dur={seconds * 1000:.2f}" for name, seconds in timings.items()
    )
    return result


@app.post("/pdfs/verify-doi", response_model=PdfDoiVerification)
async def verify_pdf_doi(
    file: UploadFile = File(...),
    expected_sha256: str = Form(..., pattern=r"^[0-9a-fA-F]{64}$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PdfDoiVerification:
    """Verify original bytes for local writeback without creating a vault record."""
    if not activity_is_allowed(f"doi-writeback:{user.id}", get_settings().upload_rate_per_hour):
        raise HTTPException(status_code=429, detail="DOI verification rate limit exceeded")
    # Release the authentication read transaction before network verification.
    db.rollback()
    sha256, _, _, _, staged_path = await store_pdf(file)
    try:
        if sha256 != expected_sha256.lower():
            raise HTTPException(status_code=409, detail="PDF hash differs from the file selected for DOI verification")
        result = await run_in_threadpool(
            verify_pdf_doi_writeback, staged_path or object_path(sha256), sha256
        )
        return PdfDoiVerification(**result)
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


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
    return existing_pdfs(db, payload.sha256, payload.doi)


@app.post("/pdfs/aliases", response_model=PdfAliasCreateResponse)
def register_pdf_file_aliases(
    payload: PdfAliasCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PdfAliasCreateResponse:
    # Compatibility endpoint: a hash-only claim cannot prove article identity.
    # New aliases are established by process_upload after examining the PDF.
    claims = {}
    for claim in payload.aliases:
        try:
            doi = normalize_doi(claim.doi)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        sha = claim.sha256.lower()
        if sha in claims and claims[sha] != doi:
            raise HTTPException(status_code=409, detail="SHA-256 was assigned conflicting DOIs")
        claims[sha] = doi
    known = existing_pdfs(db, claims).existing
    for sha, doi in claims.items():
        pdf = known.get(sha)
        if pdf is None:
            raise HTTPException(status_code=409,
                detail="Upload the PDF to verify this hash/DOI pair before registering an alias")
        if pdf.doi != doi:
            raise HTTPException(status_code=409, detail=f"SHA-256 belongs to DOI {pdf.doi}")
    return PdfAliasCreateResponse(registered={sha: known[sha] for sha in claims})


@app.get("/pdfs/search", response_model=list[PdfRead])
def search_pdfs(
    q: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Sequence[PdfRead]:
    if not q.strip():
        return []
    pdfs = db.scalars(search_pdfs_query(db, q).offset(offset).limit(limit)).all()
    return [pdf_to_read(pdf) for pdf in pdfs]


def find_pdf(identifier: str, db: Session) -> Pdf:
    return find_pdf_service(identifier, db)


def find_pdf_by_doi(doi: str, db: Session) -> Pdf:
    return find_pdf_by_doi_service(doi, db)


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
    return send_pdf_file_service(pdf, user, db)


@app.post("/admin/users", response_model=UserRead)
def create_user(
    payload: UserCreateRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> User:
    return accounts.create_user(db, payload)


@app.post("/admin/users/{username}/reset-password", response_model=UserRead)
def reset_password(
    username: str,
    payload: PasswordResetRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> User:
    return accounts.reset_password(db, username, payload)


@app.post("/admin/users/{username}/deactivate", response_model=UserRead)
def deactivate_user(
    username: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> User:
    return accounts.set_active(db, username, False, _)


@app.post(
    "/admin/pdfs/{pdf_id}/correct-doi",
    response_model=PdfDoiCorrectionResponse,
)
def correct_pdf_doi(
    pdf_id: int,
    payload: PdfDoiCorrectionRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> PdfDoiCorrectionResponse:
    result = correct_pdf_doi_service(
        db,
        admin,
        pdf_id,
        payload.doi,
        payload.reason,
        expected_sha256=payload.expected_sha256,
        expected_current_doi=payload.expected_current_doi,
        allow_title_mismatch=payload.allow_title_mismatch,
        dry_run=payload.dry_run,
    )
    return PdfDoiCorrectionResponse(**result)


@app.get(
    "/admin/pdfs/{pdf_id}/doi-corrections",
    response_model=list[PdfDoiCorrectionRead],
)
def list_pdf_doi_corrections(
    pdf_id: int,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[PdfDoiCorrectionRead]:
    return doi_correction_history(db, pdf_id)


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
