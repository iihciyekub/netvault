from datetime import datetime

from pydantic import BaseModel, Field

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
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=8, max_length=128)
    role: UserRole = UserRole.user


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)


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
    replaced: bool = False


class Sha256ExistsRequest(BaseModel):
    sha256: list[str] = Field(default_factory=list, max_length=500)
    doi: list[str] = Field(default_factory=list, max_length=500)


class Sha256ExistsResponse(BaseModel):
    existing: dict[str, PdfRead]
    existing_doi: dict[str, PdfRead] = Field(default_factory=dict)


class PdfAliasClaim(BaseModel):
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    doi: str = Field(min_length=1, max_length=255)


class PdfAliasCreateRequest(BaseModel):
    aliases: list[PdfAliasClaim] = Field(min_length=1, max_length=500)


class PdfAliasCreateResponse(BaseModel):
    registered: dict[str, PdfRead]
