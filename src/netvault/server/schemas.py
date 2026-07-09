from datetime import datetime

from pydantic import BaseModel

from netvault.server.models import UserRole


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
    sha256: str
    original_name: str
    size: int
    uploaded_at: datetime
    uploaded_by: str


class PdfDetail(PdfRead):
    storage_path: str
    upload_count: int


class UploadResponse(BaseModel):
    pdf: PdfRead
    deduplicated: bool
