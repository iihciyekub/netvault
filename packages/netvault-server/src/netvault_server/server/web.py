from pathlib import Path
import logging
import secrets
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from zipstream import ZIP_STORED, ZipStream
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netvault_server import __version__
from netvault_server.server.config import get_settings
from netvault_server.server.database import get_db
from netvault_server.server.download_audit import record_completed_downloads
from netvault_server.server.doi import find_dois_in_text, normalize_doi
from netvault_server.server.journal_filters import normalize_filter_key
from netvault_server.server.main_helpers import pdf_to_read, process_upload
from netvault_server.server.models import Pdf, User, UserRole
from netvault_server.server.queries import pdf_contains_query, pdf_read_options
from netvault_server.server.security import (
    DUMMY_PASSWORD_HASH,
    clear_login_failures,
    create_access_token,
    decode_access_token_claims,
    hash_password,
    login_is_allowed,
    password_needs_rehash,
    record_login_failure,
    verify_password,
    activity_is_allowed,
)
from netvault_server.server.stats import (
    get_dashboard_stats,
)
from netvault_server.server.storage import object_path

TOKEN_COOKIE = "netvault_token"
CSRF_COOKIE = "netvault_csrf"
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()
logger = logging.getLogger(__name__)


def format_bytes(size: int) -> str:
    value = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size:,} B"


def format_number(value: int) -> str:
    return f"{value:,}"


templates.env.filters["bytes"] = format_bytes
templates.env.filters["number"] = format_number


def base_path() -> str:
    raw = get_settings().base_path.strip()
    if not raw or raw == "/":
        return ""
    return "/" + raw.strip("/")


def external_path(path: str) -> str:
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{base_path()}{suffix}"


def csrf_token(request: Request) -> str:
    token = request.cookies.get(CSRF_COOKIE)
    return token if token else secrets.token_urlsafe(32)


def set_csrf_cookie(response: HTMLResponse | RedirectResponse, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=get_settings().secure_cookies,
    )


def validate_csrf(request: Request, submitted_token: str) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE)
    if not cookie_token or not submitted_token or not secrets.compare_digest(cookie_token, submitted_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def safe_zip_name(pdf: Pdf, used: set[str]) -> str:
    raw = (pdf.doi or pdf.original_name or str(pdf.id)).replace("/", "_").replace("\\", "_")
    if not raw.lower().endswith(".pdf"):
        raw = f"{raw}.pdf"
    name = "".join(char if char.isalnum() or char in " ._-()" else "_" for char in raw).strip(" ._")
    if not name:
        name = f"{pdf.id}.pdf"
    candidate = name
    index = 2
    while candidate.lower() in used:
        base = name[:-4] if name.lower().endswith(".pdf") else name
        candidate = f"{base}-{index}.pdf"
        index += 1
    used.add(candidate.lower())
    return candidate


def render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    token = csrf_token(request)
    context = {
        **context,
        "request": request,
        "csrf_token": token,
        "path_for": external_path,
        "asset_version": f"{__version__}-ui4",
    }
    response = templates.TemplateResponse(request, name, context)
    set_csrf_cookie(response, token)
    return response


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(external_path(path), status_code=status.HTTP_303_SEE_OTHER)


def get_cookie_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(TOKEN_COOKIE)
    if not token:
        return None
    claims = decode_access_token_claims(token)
    if not claims:
        return None
    username, token_version = claims
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active or user.token_version != token_version:
        return None
    return user


def require_web_user(request: Request, db: Session) -> User | RedirectResponse:
    user = get_cookie_user(request, db)
    return user if user else redirect("/web/login")


def require_web_admin(request: Request, db: Session) -> User | RedirectResponse:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return user


def admin_context(
    request: Request,
    user: User,
    db: Session,
    *,
    error: str | None = None,
    message: str | None = None,
) -> HTMLResponse:
    users = db.scalars(select(User).order_by(User.username.asc())).all()
    return render(
        request,
        "admin.html",
        {"user": user, "users": users, "error": error, "message": message, "roles": list(UserRole)},
    )


@router.get("/", include_in_schema=False)
def root() -> Any:
    return redirect("/web")


@router.get("/web/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request) -> Any:
    return render(request, "login.html", {"error": None})


@router.post("/web/login", include_in_schema=False)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token_value: str = Form("", alias="csrf_token"),
    db: Session = Depends(get_db),
) -> Any:
    validate_csrf(request, csrf_token_value)
    client_host = request.client.host if request.client else "unknown"
    login_key = f"{client_host}:{username.casefold()}"
    if not login_is_allowed(login_key):
        return render(request, "login.html", {"error": "Too many login attempts. Try again shortly."})
    user = db.scalar(select(User).where(User.username == username))
    candidate_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_valid = verify_password(password, candidate_hash)
    if user is None or not user.is_active or not password_valid:
        record_login_failure(login_key)
        return render(request, "login.html", {"error": "Invalid username or password."})
    clear_login_failures(login_key)
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()
    response = redirect("/web")
    response.set_cookie(
        TOKEN_COOKIE,
        create_access_token(user.username, user.token_version),
        httponly=True,
        samesite="lax",
        secure=get_settings().secure_cookies,
    )
    set_csrf_cookie(response, csrf_token(request))
    return response


@router.post("/web/logout", include_in_schema=False)
def logout_submit(
    request: Request,
    csrf_token_value: str = Form("", alias="csrf_token"),
    db: Session = Depends(get_db),
) -> Any:
    validate_csrf(request, csrf_token_value)
    user = get_cookie_user(request, db)
    if user is not None:
        user.token_version += 1
        db.commit()
    response = redirect("/web/login")
    response.delete_cookie(TOKEN_COOKIE)
    return response


@router.get("/web", response_class=HTMLResponse, include_in_schema=False)
def dashboard(
    request: Request,
    filter: str = "all",
    pin: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    pins = [name.strip() for name in pin[:10] if name.strip() and len(name.strip()) <= 255]
    stats = get_dashboard_stats(db, normalize_filter_key(filter), pins)
    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            **stats,
        },
    )


@router.get("/web/info", response_class=HTMLResponse, include_in_schema=False)
def info_page(request: Request, db: Session = Depends(get_db)) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return render(request, "info.html", {"user": user, "version": __version__})


@router.get("/web/pdfs", response_class=HTMLResponse, include_in_schema=False)
def pdfs_page(
    request: Request,
    q: str = "",
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    q = q.strip()
    pdfs = []
    total = 0
    settings = get_settings()
    page_size = settings.web_page_size
    error = None
    if len(q) > settings.search_query_max_chars:
        q = q[: settings.search_query_max_chars]
        error = f"Search queries are limited to {settings.search_query_max_chars} characters."
    if q:
        conditions = [
            Pdf.is_deleted.is_(False),
            Pdf.doi.is_not(None),
        ]
        try:
            exact_doi = normalize_doi(q)
        except ValueError:
            exact_doi = None
        conditions.append(Pdf.doi == exact_doi if exact_doi else pdf_contains_query(q))
        query = (
            select(Pdf, func.count(Pdf.id).over().label("total_count"))
            .options(*pdf_read_options())
            .where(*conditions)
            .order_by(Pdf.uploaded_at.desc(), Pdf.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = db.execute(query).unique().all()
        pdfs = [row[0] for row in rows]
        total = int(rows[0].total_count) if rows else 0
    total_pages = max(1, (total + page_size - 1) // page_size)
    return render(
        request,
        "pdfs.html",
        {
            "user": user,
            "pdfs": pdfs,
            "q": q,
            "searched": bool(q),
            "page": page,
            "total": total,
            "total_pages": total_pages,
            "error": error,
        },
    )


@router.get("/web/cli", response_class=HTMLResponse, include_in_schema=False)
def cli_page(request: Request, db: Session = Depends(get_db)) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return render(request, "cli.html", {"user": user})


@router.get("/web/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page(request: Request, db: Session = Depends(get_db)) -> Any:
    user = require_web_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return admin_context(request, user, db)


@router.post("/web/admin/users/create", response_class=HTMLResponse, include_in_schema=False)
def admin_create_user(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    role: str = Form("user"),
    csrf_token_value: str = Form("", alias="csrf_token"),
    db: Session = Depends(get_db),
) -> Any:
    validate_csrf(request, csrf_token_value)
    admin = require_web_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    username = username.strip()
    password = password.strip()
    try:
        user_role = UserRole(role)
    except ValueError:
        return admin_context(request, admin, db, error="Invalid role.")
    if not username or not password:
        return admin_context(request, admin, db, error="Username and password are required.")
    if len(password) < 8 or len(password) > 128:
        return admin_context(request, admin, db, error="Password must be 8-128 characters.")
    if len(username) > 80 or not all(char.isalnum() or char in "._-" for char in username):
        return admin_context(request, admin, db, error="Username may contain letters, numbers, dot, dash, and underscore.")
    existing = db.scalar(select(User).where(User.username == username))
    if existing is not None:
        return admin_context(request, admin, db, error="Username already exists.")
    db.add(User(username=username, password_hash=hash_password(password), role=user_role))
    db.commit()
    return admin_context(request, admin, db, message=f"Created {username} ({user_role.value}).")


@router.post("/web/admin/users/reset-password", response_class=HTMLResponse, include_in_schema=False)
def admin_reset_password(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf_token_value: str = Form("", alias="csrf_token"),
    db: Session = Depends(get_db),
) -> Any:
    validate_csrf(request, csrf_token_value)
    admin = require_web_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    username = username.strip()
    password = password.strip()
    target = db.scalar(select(User).where(User.username == username))
    if target is None:
        return admin_context(request, admin, db, error="User not found.")
    if not password:
        return admin_context(request, admin, db, error="New password is required.")
    if len(password) < 8 or len(password) > 128:
        return admin_context(request, admin, db, error="Password must be 8-128 characters.")
    target.password_hash = hash_password(password)
    target.token_version += 1
    db.commit()
    return admin_context(request, admin, db, message=f"Updated password for {target.username}.")


@router.post("/web/admin/users/set-active", response_class=HTMLResponse, include_in_schema=False)
def admin_set_user_active(
    request: Request,
    username: str = Form(""),
    active: bool = Form(...),
    csrf_token_value: str = Form("", alias="csrf_token"),
    db: Session = Depends(get_db),
) -> Any:
    validate_csrf(request, csrf_token_value)
    admin = require_web_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    username = username.strip()
    target = db.scalar(select(User).where(User.username == username))
    if target is None:
        return admin_context(request, admin, db, error="User not found.")
    if target.id == admin.id and not active:
        return admin_context(request, admin, db, error="You cannot deactivate your current account.")
    target.is_active = active
    db.commit()
    verb = "Activated" if active else "Deactivated"
    return admin_context(request, admin, db, message=f"{verb} {target.username}.")


@router.get("/web/upload", response_class=HTMLResponse, include_in_schema=False)
def upload_page(request: Request, db: Session = Depends(get_db)) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return render(request, "upload.html", {"user": user, "results": [], "error": None})


@router.post("/web/upload", response_class=HTMLResponse, include_in_schema=False)
async def upload_submit(
    request: Request,
    files: list[UploadFile] = File(...),
    doi: str = Form(""),
    no_crossref: bool = Form(False),
    csrf_token_value: str = Form("", alias="csrf_token"),
    db: Session = Depends(get_db),
) -> Any:
    validate_csrf(request, csrf_token_value)
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    manual_doi = doi.strip() or None
    settings = get_settings()
    if not activity_is_allowed(
        f"upload:{user.id}", settings.upload_rate_per_hour, amount=len(files)
    ):
        return render(
            request,
            "upload.html",
            {"user": user, "results": [], "error": "Upload rate limit exceeded. Try again later."},
        )
    if len(files) > settings.max_upload_files:
        return render(
            request,
            "upload.html",
            {"user": user, "results": [], "error": f"Upload at most {settings.max_upload_files} files at once."},
        )
    known_total = sum(upload.size or 0 for upload in files)
    if known_total > settings.max_batch_bytes:
        return render(
            request,
            "upload.html",
            {"user": user, "results": [], "error": "Upload batch exceeds the configured size limit."},
        )
    if manual_doi and len(files) != 1:
        return render(
            request,
            "upload.html",
            {"user": user, "results": [], "error": "Manual DOI can only be used with one file."},
        )
    results: list[dict[str, Any]] = []
    for upload_file in files:
        try:
            result = await process_upload(upload_file, manual_doi, no_crossref, user, db)
            results.append({"filename": upload_file.filename, "ok": True, "result": result})
        except HTTPException as exc:
            results.append({"filename": upload_file.filename, "ok": False, "error": exc.detail})
        except Exception:
            db.rollback()
            logger.exception("Unexpected upload failure for %s", upload_file.filename)
            results.append(
                {
                    "filename": upload_file.filename,
                    "ok": False,
                    "error": "Unexpected server error. The file was not added.",
                }
            )
    return render(request, "upload.html", {"user": user, "results": results, "error": None})


@router.post("/web/upload/file", include_in_schema=False)
async def upload_single_file(
    request: Request,
    file: UploadFile = File(...),
    doi: str = Form(""),
    no_crossref: bool = Form(False),
    csrf_token_value: str = Form("", alias="csrf_token"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    validate_csrf(request, csrf_token_value)
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    settings = get_settings()
    if not activity_is_allowed(f"upload:{user.id}", settings.upload_rate_per_hour):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Upload rate limit exceeded")
    idempotency_key = request.headers.get("idempotency-key")
    if idempotency_key and len(idempotency_key) > 128:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency key is too long")
    result = await process_upload(
        file, doi.strip() or None, no_crossref, user, db, idempotency_key
    )
    return {
        "filename": file.filename,
        "ok": True,
        "status": "existing" if result.deduplicated else "uploaded",
        "doi": result.pdf.doi,
    }


@router.post("/web/pdfs/exists", include_in_schema=False)
async def web_existing_pdfs_by_sha256(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    validate_csrf(request, request.headers.get("x-csrf-token", ""))
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    payload = await request.json()
    submitted = payload.get("sha256", [])
    if not isinstance(submitted, list) or len(submitted) > 500:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At most 500 hashes are allowed")
    hashes = sorted(
        {
            sha.lower()
            for sha in submitted
            if isinstance(sha, str) and len(sha) == 64 and all(char in "0123456789abcdefABCDEF" for char in sha)
        }
    )
    if not hashes:
        return {"existing": {}}
    pdfs = db.scalars(select(Pdf).where(Pdf.is_deleted.is_(False), Pdf.sha256.in_(hashes))).all()
    return {"existing": {pdf.sha256: pdf_to_read(pdf).model_dump(mode="json") for pdf in pdfs}}


@router.get("/web/download", response_class=HTMLResponse, include_in_schema=False)
def download_page(request: Request, db: Session = Depends(get_db)) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return render(
        request,
        "download.html",
        {"user": user, "matches": [], "missing": [], "doi_text": "", "error": None},
    )


@router.post("/web/download", response_class=HTMLResponse, include_in_schema=False)
def download_lookup(
    request: Request,
    doi_text: str = Form(""),
    csrf_token_value: str = Form("", alias="csrf_token"),
    db: Session = Depends(get_db),
) -> Any:
    validate_csrf(request, csrf_token_value)
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    dois = find_dois_in_text(doi_text)
    if len(dois) > 500:
        return render(
            request,
            "download.html",
            {
                "user": user,
                "matches": [],
                "missing": [],
                "doi_text": doi_text,
                "error": "Submit at most 500 DOI values at once.",
            },
        )
    pdfs = db.scalars(select(Pdf).where(Pdf.doi.in_(dois), Pdf.is_deleted.is_(False))).all() if dois else []
    by_doi = {pdf.doi: pdf for pdf in pdfs}
    matches = [by_doi[doi] for doi in dois if doi in by_doi]
    missing = [doi for doi in dois if doi not in by_doi]
    return render(
        request,
        "download.html",
        {"user": user, "matches": matches, "missing": missing, "doi_text": doi_text, "error": None},
    )


@router.get("/web/pdfs/download", include_in_schema=False)
def web_pdf_download(
    request: Request,
    doi: str | None = None,
    pdf_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if pdf_id is not None:
        pdf = db.scalar(select(Pdf).where(Pdf.id == pdf_id, Pdf.is_deleted.is_(False)))
    elif doi:
        try:
            normalized_doi = normalize_doi(doi)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid DOI") from None
        pdf = db.scalar(select(Pdf).where(Pdf.doi == normalized_doi, Pdf.is_deleted.is_(False)))
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing PDF identifier")
    if pdf is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF not found")
    path = object_path(pdf.sha256)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Stored PDF missing")
    if not activity_is_allowed(f"download:{user.id}", get_settings().download_rate_per_hour):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Download rate limit exceeded")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=Path(pdf.original_name).name,
        background=BackgroundTask(record_completed_downloads, [pdf.id], user.id),
        headers={"ETag": f'"{pdf.sha256}"'},
    )


@router.post("/web/pdfs/download-all", include_in_schema=False)
def web_pdf_download_all(
    request: Request,
    pdf_ids: list[int] = Form(default=[]),
    csrf_token_value: str = Form("", alias="csrf_token"),
    db: Session = Depends(get_db),
) -> Any:
    validate_csrf(request, csrf_token_value)
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ids = list(dict.fromkeys(pdf_ids))
    if not ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No PDFs selected")
    settings = get_settings()
    if len(ids) > settings.max_zip_files:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Select at most {settings.max_zip_files} PDFs",
        )
    if not activity_is_allowed(
        f"download:{user.id}", settings.download_rate_per_hour, amount=len(ids)
    ):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Download rate limit exceeded")
    pdfs = db.scalars(select(Pdf).where(Pdf.id.in_(ids), Pdf.is_deleted.is_(False))).all()
    positions = {pdf_id: index for index, pdf_id in enumerate(ids)}
    ordered = sorted(pdfs, key=lambda pdf: positions[pdf.id])
    if not ordered:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PDFs found")
    if sum(pdf.size for pdf in ordered) > settings.max_zip_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Selected PDFs exceed the ZIP size limit",
        )

    used_names: set[str] = set()
    paths = [(pdf, object_path(pdf.sha256)) for pdf in ordered]
    missing = [pdf.doi for pdf, path in paths if not path.exists()]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{len(missing)} stored PDF files are missing; no partial ZIP was created",
        )
    archive = ZipStream(compress_type=ZIP_STORED, sized=True)
    manifest_lines = ["doi\tfilename\tsha256\tsize"]
    for pdf, path in paths:
        archive_name = safe_zip_name(pdf, used_names)
        archive.add_path(path, archive_name)
        manifest_lines.append(f"{pdf.doi}\t{archive_name}\t{pdf.sha256}\t{pdf.size}")
    archive.add("\n".join(manifest_lines) + "\n", "netvault-manifest.tsv")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote('netvault-pdfs.zip')}",
        "Content-Length": str(len(archive)),
    }
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers=headers,
        background=BackgroundTask(record_completed_downloads, [pdf.id for pdf in ordered], user.id),
    )
