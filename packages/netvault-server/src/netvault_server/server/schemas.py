from datetime import datetime

from pydantic import BaseModel

from netvault_server.server.models import UserRole


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserRead(BaseModel):
    id: int
    username: str
    role: UserRole
    is_active: bool

    model_config = {"from_attributes": True}


class LoginRequest(BaseModel):
    username: str
    password: str


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: UserRole = UserRole.user


class PasswordResetRequest(BaseModel):
    password: str


class PdfRead(BaseModel):
    id: int
    doi: str
    doi_source: str | None = None
    sha256: str
    original_name: str
    title: str | None = None
    authors: str | None = None
    container_title: str | None = None
    publisher: str | None = None
    published_year: int | None = None
    crossref_status: str
    crossref_url: str | None = None
    size: int
    uploaded_at: datetime
    uploaded_by: str


class PdfDetail(PdfRead):
    storage_path: str
    upload_count: int
    doi_evidence: str | None = None


class UploadResponse(BaseModel):
    pdf: PdfRead
    deduplicated: bool


class Sha256ExistsRequest(BaseModel):
    sha256: list[str]


class Sha256ExistsResponse(BaseModel):
    existing: dict[str, PdfRead]
