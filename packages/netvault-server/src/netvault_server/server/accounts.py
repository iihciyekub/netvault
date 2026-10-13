"""Account actions shared by the API and HTML form adapters."""

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from netvault_server.server.models import User, utc_now
from netvault_server.server.schemas import LoginRequest, PasswordResetRequest, UserCreateRequest
from netvault_server.server.security import (
    DUMMY_PASSWORD_HASH,
    clear_login_failures,
    hash_password,
    login_is_allowed,
    password_needs_rehash,
    record_login_failure,
    verify_password,
)


def authenticate(db: Session, username: str, password: str, client_host: str) -> User:
    try:
        LoginRequest(username=username, password=password)
    except ValidationError as exc:
        raise HTTPException(status_code=401, detail="Invalid credentials") from exc
    key = f"{client_host}:{username.casefold()}"
    if not login_is_allowed(key):
        raise HTTPException(status_code=429, detail="Too many login attempts")
    user = db.scalar(select(User).where(User.username == username))
    valid = verify_password(password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if user is None or not user.is_active or not valid:
        record_login_failure(key)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    clear_login_failures(key)
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = utc_now()
    db.commit()
    return user


def create_user(db: Session, payload: UserCreateRequest) -> User:
    if db.scalar(select(User.id).where(User.username == payload.username)) is not None:
        raise HTTPException(status_code=409, detail="Username already exists")
    user = User(
        username=payload.username, password_hash=hash_password(payload.password), role=payload.role
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def reset_password(db: Session, username: str, payload: PasswordResetRequest) -> User:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = hash_password(payload.password)
    user.token_version += 1
    db.commit()
    db.refresh(user)
    return user


def set_active(db: Session, username: str, active: bool, admin: User) -> User:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id and not active:
        raise HTTPException(status_code=400, detail="You cannot deactivate your current account.")
    user.is_active = active
    db.commit()
    db.refresh(user)
    return user


def revoke_sessions(db: Session, user: User) -> None:
    user.token_version += 1
    db.commit()


def form_validation_message(error: ValidationError) -> str:
    field = error.errors()[0]["loc"][0]
    return {
        "username": "Username must use 1-80 ASCII letters, numbers, dot, dash, or underscore.",
        "password": "Password must be 8-128 characters.",
        "role": "Invalid role.",
    }.get(field, "Invalid account details.")
