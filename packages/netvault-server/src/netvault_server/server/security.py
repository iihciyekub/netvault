from datetime import datetime, timedelta, timezone
from threading import Lock
from time import monotonic

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
import bcrypt
from jose import JWTError, jwt

from netvault_server.server.config import get_settings

ALGORITHM = "HS256"
password_hasher = PasswordHasher()
DUMMY_PASSWORD_HASH = password_hasher.hash("netvault-login-timing-placeholder")
_login_failures: dict[str, list[float]] = {}
_login_lock = Lock()
_activity_events: dict[str, list[float]] = {}
_activity_lock = Lock()
LOGIN_WINDOW_SECONDS = 60.0
LOGIN_MAX_FAILURES = 5


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if password_hash.startswith("$argon2"):
        try:
            return password_hasher.verify(password_hash, password)
        except VerificationError:
            return False
    if password_hash.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
        except (ValueError, TypeError):
            return False
    return False


def password_needs_rehash(password_hash: str) -> bool:
    if not password_hash.startswith("$argon2"):
        return True
    try:
        return password_hasher.check_needs_rehash(password_hash)
    except VerificationError:
        return True


def login_is_allowed(key: str) -> bool:
    cutoff = monotonic() - LOGIN_WINDOW_SECONDS
    with _login_lock:
        if len(_login_failures) > 10_000:
            expired_keys = [
                candidate
                for candidate, timestamps in _login_failures.items()
                if not timestamps or timestamps[-1] < cutoff
            ]
            for candidate in expired_keys:
                _login_failures.pop(candidate, None)
            while len(_login_failures) > 10_000:
                _login_failures.pop(next(iter(_login_failures)))
        recent = [timestamp for timestamp in _login_failures.get(key, []) if timestamp >= cutoff]
        _login_failures[key] = recent
        return len(recent) < LOGIN_MAX_FAILURES


def record_login_failure(key: str) -> None:
    with _login_lock:
        _login_failures.setdefault(key, []).append(monotonic())


def clear_login_failures(key: str) -> None:
    with _login_lock:
        _login_failures.pop(key, None)


def activity_is_allowed(
    key: str,
    limit: int,
    *,
    window_seconds: float = 3600.0,
    amount: int = 1,
) -> bool:
    """Apply a bounded per-process rate limit for authenticated expensive actions."""
    now = monotonic()
    cutoff = now - window_seconds
    with _activity_lock:
        if len(_activity_events) > 20_000:
            expired = [
                candidate
                for candidate, timestamps in _activity_events.items()
                if not timestamps or timestamps[-1] < cutoff
            ]
            for candidate in expired:
                _activity_events.pop(candidate, None)
        recent = [timestamp for timestamp in _activity_events.get(key, []) if timestamp >= cutoff]
        if len(recent) + amount > limit:
            _activity_events[key] = recent
            return False
        recent.extend([now] * amount)
        _activity_events[key] = recent
        return True


def create_access_token(username: str, token_version: int = 0) -> str:
    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_minutes)
    payload = {"sub": username, "ver": token_version, "exp": expires_at}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> str | None:
    claims = decode_access_token_claims(token)
    return claims[0] if claims else None


def decode_access_token_claims(token: str) -> tuple[str, int] | None:
    try:
        payload = jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM])
    except JWTError:
        return None
    subject = payload.get("sub")
    version = payload.get("ver", 0)
    if not isinstance(subject, str) or not isinstance(version, int):
        return None
    return subject, version
