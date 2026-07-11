from pathlib import Path
from typing import Any

import tomli_w

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


APP_DIR = Path.home() / ".config" / "netvault"
CREDENTIALS_PATH = APP_DIR / "credentials.toml"
HASH_CACHE_PATH = APP_DIR / "hash-cache.json"
IDENTITY_CACHE_PATH = APP_DIR / "identity-cache.json"


def load_credentials() -> dict[str, Any]:
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        with CREDENTIALS_PATH.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def save_credentials(server_url: str, token: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    normalized_url = server_url.rstrip("/")
    CREDENTIALS_PATH.write_text(
        tomli_w.dumps({"server_url": normalized_url, "token": token}),
        encoding="utf-8",
    )
    CREDENTIALS_PATH.chmod(0o600)


def clear_credentials() -> None:
    CREDENTIALS_PATH.unlink(missing_ok=True)


def require_credentials() -> tuple[str, str]:
    credentials = load_credentials()
    server_url = credentials.get("server_url")
    token = credentials.get("token")
    if not isinstance(server_url, str) or not isinstance(token, str):
        raise RuntimeError("Not logged in. Run `netvault login <server-url>` first.")
    return server_url, token
