from pathlib import Path
from typing import Any

import tomli_w

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


APP_DIR = Path.home() / ".config" / "netvault"
CREDENTIALS_PATH = APP_DIR / "credentials.toml"
CONFIG_PATH = APP_DIR / "config.toml"
HASH_CACHE_PATH = APP_DIR / "hash-cache.json"
IDENTITY_CACHE_PATH = APP_DIR / "identity-cache.json"
DEFAULT_UPLOAD_INDEX_NAMES = ("pdf-download-index.json",)


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


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"Could not read NetVault config {CONFIG_PATH}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"NetVault config {CONFIG_PATH} must contain a TOML table")
    return payload


def load_upload_index_settings() -> tuple[bool, tuple[str, ...]]:
    payload = load_config()
    upload = payload.get("upload", {})
    if not isinstance(upload, dict):
        raise RuntimeError("NetVault config [upload] must be a TOML table")
    index = upload.get("index", {})
    if not isinstance(index, dict):
        raise RuntimeError("NetVault config [upload.index] must be a TOML table")

    enabled = index.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RuntimeError("NetVault config upload.index.enabled must be true or false")

    names = index.get("names", list(DEFAULT_UPLOAD_INDEX_NAMES))
    if not isinstance(names, list) or not names:
        raise RuntimeError("NetVault config upload.index.names must be a non-empty string array")
    normalized: list[str] = []
    for name in names:
        if (
            not isinstance(name, str)
            or not name.strip()
            or Path(name).name != name
            or Path(name).suffix.lower() != ".json"
        ):
            raise RuntimeError(
                "NetVault config upload.index.names entries must be JSON basenames"
            )
        if name not in normalized:
            normalized.append(name)
    return enabled, tuple(normalized)


def require_credentials() -> tuple[str, str]:
    credentials = load_credentials()
    server_url = credentials.get("server_url")
    token = credentials.get("token")
    if not isinstance(server_url, str) or not isinstance(token, str):
        raise RuntimeError("Not logged in. Run `netvault login <server-url>` first.")
    return server_url, token
