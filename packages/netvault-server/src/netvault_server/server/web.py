from pathlib import Path
import secrets
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from netvault_server import __version__
from netvault_server.server.config import get_settings
from netvault_server.server.database import get_db
from netvault_server.server.doi import find_dois_in_text, normalize_doi
from netvault_server.server.main_helpers import process_upload
from netvault_server.server.models import DownloadRecord, Pdf, User
from netvault_server.server.security import create_access_token, decode_access_token, verify_password
from netvault_server.server.stats import (
    get_by_journal,
    get_by_journal_year,
    get_by_year,
    get_summary,
)
from netvault_server.server.storage import object_path

TOKEN_COOKIE = "netvault_token"
CSRF_COOKIE = "netvault_csrf"
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()


def format_bytes(size: int) -> str:
    value = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size:,} B"


templates.env.filters["bytes"] = format_bytes


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
    response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="lax")


def validate_csrf(request: Request, submitted_token: str) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE)
    if not cookie_token or not submitted_token or not secrets.compare_digest(cookie_token, submitted_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    token = csrf_token(request)
    context = {
        **context,
        "request": request,
        "csrf_token": token,
        "path_for": external_path,
        "asset_version": __version__,
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
    username = decode_access_token(token)
    if not username:
        return None
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active:
        return None
    return user


def require_web_user(request: Request, db: Session) -> User | RedirectResponse:
    user = get_cookie_user(request, db)
    return user if user else redirect("/web/login")


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
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return render(request, "login.html", {"error": "Invalid username or password."})
    response = redirect("/web")
    response.set_cookie(
        TOKEN_COOKIE,
        create_access_token(user.username),
        httponly=True,
        samesite="lax",
    )
    set_csrf_cookie(response, csrf_token(request))
    return response


@router.post("/web/logout", include_in_schema=False)
def logout_submit(
    request: Request,
    csrf_token_value: str = Form("", alias="csrf_token"),
) -> Any:
    validate_csrf(request, csrf_token_value)
    response = redirect("/web/login")
    response.delete_cookie(TOKEN_COOKIE)
    return response


@router.get("/web", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, db: Session = Depends(get_db)) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            "summary": get_summary(db),
            "by_year": get_by_year(db),
            "by_journal": get_by_journal(db),
            "journal_year": get_by_journal_year(db),
        },
    )


@router.get("/web/pdfs", response_class=HTMLResponse, include_in_schema=False)
def pdfs_page(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    q = q.strip()
    pdfs = []
    if q:
        pattern = f"%{q}%"
        query = (
            select(Pdf)
            .where(
                Pdf.is_deleted.is_(False),
                Pdf.doi.is_not(None),
                or_(
                    Pdf.doi.ilike(pattern),
                    Pdf.title.ilike(pattern),
                    Pdf.authors.ilike(pattern),
                    Pdf.container_title.ilike(pattern),
                    Pdf.original_name.ilike(pattern),
                    Pdf.sha256.ilike(pattern),
                ),
            )
            .order_by(Pdf.uploaded_at.desc())
            .limit(100)
        )
        pdfs = db.scalars(query).all()
    return render(request, "pdfs.html", {"user": user, "pdfs": pdfs, "q": q, "searched": bool(q)})


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
    return render(request, "upload.html", {"user": user, "results": results, "error": None})


@router.get("/web/download", response_class=HTMLResponse, include_in_schema=False)
def download_page(request: Request, db: Session = Depends(get_db)) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return render(request, "download.html", {"user": user, "matches": [], "missing": [], "doi_text": ""})


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
    matches = []
    missing = []
    for doi in dois:
        pdf = db.scalar(select(Pdf).where(Pdf.doi == doi, Pdf.is_deleted.is_(False)))
        if pdf:
            matches.append(pdf)
        else:
            missing.append(doi)
    return render(
        request,
        "download.html",
        {"user": user, "matches": matches, "missing": missing, "doi_text": doi_text},
    )


@router.get("/web/pdfs/download", include_in_schema=False)
def web_pdf_download(
    request: Request,
    doi: str,
    db: Session = Depends(get_db),
) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        normalized_doi = normalize_doi(doi)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid DOI") from None
    pdf = db.scalar(select(Pdf).where(Pdf.doi == normalized_doi, Pdf.is_deleted.is_(False)))
    if pdf is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF not found")
    path = object_path(pdf.sha256)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Stored PDF missing")
    db.add(DownloadRecord(pdf_id=pdf.id, user_id=user.id))
    db.commit()
    return FileResponse(path, media_type="application/pdf", filename=Path(pdf.original_name).name)
