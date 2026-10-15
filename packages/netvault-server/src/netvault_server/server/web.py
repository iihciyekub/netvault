from pathlib import Path
from html.parser import HTMLParser
import logging
import secrets
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.background import BackgroundTask
from zipstream import ZIP_STORED, ZipStream
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netvault_server import __version__
from netvault_server.server.config import get_settings
from netvault_server.server.database import get_db
from netvault_server.server.download_audit import record_completed_downloads
from netvault_server.server.doi import find_dois_in_text
from netvault_server.server.journal_filters import (
    create_user_journal_filter,
    delete_user_journal_filter,
    is_custom_filter_key,
    journal_filter_state,
    normalize_filter_key,
    reset_user_journal_filter,
    save_user_journal_filter,
)
from netvault_server.server.doi_correction import correct_pdf_doi as correct_pdf_doi_service
from netvault_server.server.main_helpers import process_upload
from netvault_server.server.models import Pdf, User, UserRole
from netvault_server.server.pdf_service import (existing_pdfs, search_pdfs_query,
    search_pdfs_by_dois_query, resolve_pdf_file, send_pdf_file, doi_correction_history)
from netvault_server.server.bulk_doi_search import create_doi_batch, get_doi_batch
from netvault_server.server import accounts
from netvault_server.server.schemas import PasswordResetRequest, UserCreateRequest, Sha256ExistsRequest
from pydantic import BaseModel, Field, ValidationError
from netvault_server.server.security import (
    create_access_token,
    decode_access_token_claims,
    activity_is_allowed,
)
from netvault_server.server.stats import (
    get_dashboard_stats,
    get_dashboard_revision,
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


class _MetadataMarkupSanitizer(HTMLParser):
    _allowed_tags = {
        "b": "strong",
        "em": "em",
        "i": "em",
        "jats:bold": "strong",
        "jats:italic": "em",
        "jats:sub": "sub",
        "jats:sup": "sup",
        "strong": "strong",
        "sub": "sub",
        "sup": "sup",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if safe_tag := self._allowed_tags.get(tag):
            self.parts.append(f"<{safe_tag}>")

    def handle_endtag(self, tag: str) -> None:
        if safe_tag := self._allowed_tags.get(tag):
            self.parts.append(f"</{safe_tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(str(escape(data)))


def render_metadata(value: str | None) -> Markup:
    """Render Crossref inline markup while dropping tags and attributes we do not trust."""
    if not value:
        return Markup("")
    parser = _MetadataMarkupSanitizer()
    parser.feed(value)
    parser.close()
    return Markup("".join(parser.parts))


templates.env.filters["bytes"] = format_bytes
templates.env.filters["number"] = format_number
templates.env.filters["metadata"] = render_metadata


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
        max_age=get_settings().web_session_days * 86400,
    )


def set_login_cookie(response: HTMLResponse | RedirectResponse, user: User) -> None:
    settings = get_settings()
    response.set_cookie(
        TOKEN_COOKIE,
        create_access_token(
            user.username, user.token_version, expires_minutes=settings.web_session_days * 1440
        ),
        max_age=settings.web_session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
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
        "asset_version": __version__,
        "app_version": __version__,
        "upload_limits": get_settings(),
    }
    response = templates.TemplateResponse(request, name, context)
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    if user := context.get("user"):
        set_login_cookie(response, user)
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
def login_page(request: Request, db: Session = Depends(get_db)) -> Any:
    if user := get_cookie_user(request, db):
        response = redirect("/web")
        set_login_cookie(response, user)
        set_csrf_cookie(response, csrf_token(request))
        return response
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
    try:
        user = accounts.authenticate(db, username, password,
                                     request.client.host if request.client else "unknown")
    except HTTPException as exc:
        message = "Too many login attempts. Try again shortly." if exc.status_code == 429 else "Invalid username or password."
        return render(request, "login.html", {"error": message})
    response = redirect("/web")
    set_login_cookie(response, user)
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
        accounts.revoke_sessions(db, user)
    response = redirect("/web/login")
    response.delete_cookie(TOKEN_COOKIE)
    return response


@router.get("/web/journal-rank")
def journal_rank(request: Request, name: str = Query(min_length=1, max_length=300),
                 refresh: bool = False, db: Session = Depends(get_db)) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        raise HTTPException(status_code=401, detail="Login required")
    from netvault_server.server.easyscholar import lookup
    try:
        return lookup(name, refresh=refresh)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@router.get("/web/dashboard-revision", include_in_schema=False)
def dashboard_revision(request: Request, db: Session = Depends(get_db)) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return {"revision": get_dashboard_revision()}


@router.get("/web", response_class=HTMLResponse, include_in_schema=False)
def dashboard(
    request: Request,
    filter: str = "utd24",
    pin: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    pins = [name.strip() for name in pin[:10] if name.strip() and len(name.strip()) <= 255]
    filter_key = normalize_filter_key(filter)
    journal_limit = user.dashboard_journal_limit
    stats = get_dashboard_stats(
        db,
        filter_key,
        pins,
        user_id=user.id,
        journal_limit=journal_limit,
    )
    # Ship all journal totals for correct global sorting and name/initials search,
    # but avoid emitting tens of thousands of invisible heatmap DOM nodes.
    heatmap_rows = [
        [
            row["journal"],
            row["total"],
            [
                [index, cell["count"], cell["level"]]
                for index, cell in enumerate(row["cells"])
                if cell["count"] > 0
            ],
        ]
        for row in stats["journal_year"]["rows"]
    ]
    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            "dashboard_journal_limit": user.dashboard_journal_limit,
            "heatmap_rows": heatmap_rows,
            "dashboard_revision": get_dashboard_revision(),
            **stats,
        },
    )


@router.post("/web/preferences/all-journal-limit", include_in_schema=False)
def update_all_journal_limit(
    request: Request,
    journal_limit: int = Form(...),
    active_filter: str = Form("all"),
    csrf_token_value: str = Form("", alias="csrf_token"),
    db: Session = Depends(get_db),
) -> Any:
    validate_csrf(request, csrf_token_value)
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not 1 <= journal_limit <= 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Journal display limit must be between 1 and 200",
        )
    user.dashboard_journal_limit = journal_limit
    db.commit()
    # Display-limit changes do not alter the cached full journal dataset.
    if request.headers.get("x-requested-with") == "fetch":
        return {"journal_limit": journal_limit}
    return redirect(f"/web?filter={quote(normalize_filter_key(active_filter))}")


@router.post("/web/journal-filters", include_in_schema=False)
def web_create_journal_filter(
    request: Request,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
) -> Any:
    validate_csrf(request, request.headers.get("x-csrf-token", ""))
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    name = payload.get("name")
    if not isinstance(name, str):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Provide a list name")
    try:
        result = create_user_journal_filter(db, user.id, name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    from netvault_server.server.stats import invalidate_stats_cache

    invalidate_stats_cache()
    return result


@router.get("/web/journal-filters/{filter_key}", include_in_schema=False)
def web_journal_filter(
    request: Request,
    filter_key: str,
    db: Session = Depends(get_db),
) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        return journal_filter_state(db, user.id, filter_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.post("/web/journal-filters/{filter_key}", include_in_schema=False)
def web_save_journal_filter(
    request: Request,
    filter_key: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
) -> Any:
    validate_csrf(request, request.headers.get("x-csrf-token", ""))
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    journals = payload.get("journals")
    name = payload.get("name")
    if not isinstance(journals, list) or len(journals) > 2_000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at most 2,000 journal names",
        )
    if any(not isinstance(name, str) or len(name.strip()) > 255 for name in journals):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Each journal name must be text with at most 255 characters",
        )
    try:
        result = save_user_journal_filter(
            db,
            user.id,
            filter_key,
            journals,
            name if isinstance(name, str) else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    from netvault_server.server.stats import invalidate_stats_cache

    invalidate_stats_cache()
    return result


@router.delete("/web/journal-filters/{filter_key}", include_in_schema=False)
def web_reset_journal_filter(
    request: Request,
    filter_key: str,
    db: Session = Depends(get_db),
) -> Any:
    validate_csrf(request, request.headers.get("x-csrf-token", ""))
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        if is_custom_filter_key(normalize_filter_key(filter_key)):
            result = delete_user_journal_filter(db, user.id, filter_key)
        else:
            result = reset_user_journal_filter(db, user.id, filter_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    from netvault_server.server.stats import invalidate_stats_cache

    invalidate_stats_cache()
    return result


@router.get("/web/info", response_class=HTMLResponse, include_in_schema=False)
def info_page(request: Request, db: Session = Depends(get_db)) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return render(request, "info.html", {"user": user, "version": __version__})


class BulkDoiSearchRequest(BaseModel):
    dois: list[str] = Field(min_length=1, max_length=500)


@router.post("/web/pdfs/batch", include_in_schema=False)
def create_web_doi_batch(
    request: Request,
    payload: BulkDoiSearchRequest,
    db: Session = Depends(get_db),
) -> Any:
    """Issue an opaque URL for a user-scoped, paginated, exact-DOI PDF search."""
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        raise HTTPException(status_code=401, detail="Login required")
    validate_csrf(request, request.headers.get("x-csrf-token", ""))
    try:
        token, count = create_doi_batch(user.id, payload.dois)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"url": external_path("/web/pdfs") + "?batch=" + token, "count": count}


@router.get("/web/pdfs", response_class=HTMLResponse, include_in_schema=False)
def pdfs_page(
    request: Request,
    q: str = "",
    page: int = Query(default=1, ge=1),
    batch: str = Query(default="", max_length=64),
    journal: str = Query(default="", max_length=255),
    year: int | None = Query(default=None, ge=1, le=9999),
    db: Session = Depends(get_db),
) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    q = q.strip()
    journal = journal.strip()
    searched = bool(q or journal or year is not None or batch)
    batch_dois = get_doi_batch(user.id, batch) if batch else None
    pdfs = []
    total = 0
    settings = get_settings()
    page_size = settings.web_page_size
    error = None
    if batch and batch_dois is None:
        error = "DOI batch expired or unavailable. Run the batch search again from Dashboard."
    elif not batch and len(q) > settings.search_query_max_chars:
        error = f"Search queries are limited to {settings.search_query_max_chars} characters."
    if searched and error is None:
        base_query = (
            search_pdfs_by_dois_query(db, batch_dois)
            if batch_dois is not None
            else search_pdfs_query(db, q, journal=journal, year=year)
        )
        query = (base_query
                 .add_columns(func.count(Pdf.id).over().label("total_count"))
                 .offset((page - 1) * page_size).limit(page_size))
        rows = db.execute(query).unique().all()
        pdfs = [row[0] for row in rows]
        total = int(rows[0].total_count) if rows else 0
    total_pages = max(1, (total + page_size - 1) // page_size)
    search_params = {"batch": batch} if batch else {"q": q}
    if not batch and journal:
        search_params["journal"] = journal
    if not batch and year is not None:
        search_params["year"] = str(year)
    return render(
        request,
        "pdfs.html",
        {
            "user": user,
            "pdfs": pdfs,
            "q": q,
            "journal": journal,
            "year": year,
            "search_params": urlencode(search_params),
            "batch_doi_count": len(batch_dois) if batch_dois else 0,
            "searched": searched,
            "page": page,
            "total": total,
            "total_pages": total_pages,
            "error": error,
        },
    )


@router.post("/web/admin/pdfs/{pdf_id}/correct-doi/preview", include_in_schema=False)
def web_preview_doi_correction(
    pdf_id: int,
    request: Request,
    doi: str = Form(...),
    reason: str = Form(...),
    csrf_token_value: str = Form("", alias="csrf_token"),
    db: Session = Depends(get_db),
) -> dict:
    validate_csrf(request, csrf_token_value)
    admin = require_web_admin(request, db)
    if isinstance(admin, RedirectResponse):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    return correct_pdf_doi_service(
        db,
        admin,
        pdf_id,
        doi,
        reason,
        dry_run=True,
    )


@router.post("/web/admin/pdfs/{pdf_id}/correct-doi", include_in_schema=False)
def web_apply_doi_correction(
    pdf_id: int,
    request: Request,
    doi: str = Form(...),
    reason: str = Form(...),
    expected_sha256: str = Form(...),
    expected_current_doi: str = Form(...),
    allow_title_mismatch: bool = Form(False),
    csrf_token_value: str = Form("", alias="csrf_token"),
    db: Session = Depends(get_db),
) -> dict:
    validate_csrf(request, csrf_token_value)
    admin = require_web_admin(request, db)
    if isinstance(admin, RedirectResponse):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    return correct_pdf_doi_service(
        db,
        admin,
        pdf_id,
        doi,
        reason,
        expected_sha256=expected_sha256,
        expected_current_doi=expected_current_doi,
        allow_title_mismatch=allow_title_mismatch,
        dry_run=False,
    )


@router.get("/web/admin/pdfs/{pdf_id}/doi-corrections", include_in_schema=False)
def web_doi_correction_history(
    pdf_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> list[dict]:
    admin = require_web_admin(request, db)
    if isinstance(admin, RedirectResponse):
        raise HTTPException(status_code=401, detail="Login required")
    return [row.model_dump(mode="json") for row in doi_correction_history(db, pdf_id)]


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
    try:
        payload = UserCreateRequest(username=username, password=password, role=role)
        user = accounts.create_user(db, payload)
    except ValidationError as exc:
        return admin_context(request, admin, db, error=accounts.form_validation_message(exc))
    except HTTPException as exc:
        return admin_context(request, admin, db, error=exc.detail)
    return admin_context(request, admin, db, message=f"Created {user.username} ({user.role.value}).")


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
    try:
        target = accounts.reset_password(db, username, PasswordResetRequest(password=password))
    except ValidationError as exc:
        return admin_context(request, admin, db, error=accounts.form_validation_message(exc))
    except HTTPException as exc:
        return admin_context(request, admin, db, error=exc.detail)
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
    try:
        target = accounts.set_active(db, username, active, admin)
    except HTTPException as exc:
        return admin_context(request, admin, db, error=exc.detail)
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
        raise HTTPException(status_code=401, detail="Login required")
    try:
        payload = Sha256ExistsRequest.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Provide at most 500 hashes and DOI values") from exc
    return existing_pdfs(db, payload.sha256, payload.doi).model_dump(mode="json")


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


def _resolve_web_pdf_file(
    db: Session,
    *,
    doi: str | None,
    pdf_id: int | None,
) -> tuple[Pdf, Path]:
    return resolve_pdf_file(db, doi=doi, pdf_id=pdf_id)


@router.get("/web/pdfs/preview", include_in_schema=False)
def web_pdf_preview(
    request: Request,
    doi: str | None = None,
    pdf_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    pdf, _ = _resolve_web_pdf_file(db, doi=doi, pdf_id=pdf_id)
    return send_pdf_file(pdf, user, db, preview=True)


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
    pdf, _ = _resolve_web_pdf_file(db, doi=doi, pdf_id=pdf_id)
    return send_pdf_file(pdf, user, db)


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
    if user.role != UserRole.admin and not activity_is_allowed(
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
