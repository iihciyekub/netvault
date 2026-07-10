from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from netvault_server import __version__


class Settings(BaseSettings):
    database_url: str = Field(
        default="sqlite:///./netvault-dev.db",
        validation_alias="NETVAULT_DATABASE_URL",
    )
    storage_root: Path = Field(default=Path("./storage"), validation_alias="NETVAULT_STORAGE_ROOT")
    secret_key: str = Field(default="dev-secret-change-me", validation_alias="NETVAULT_SECRET_KEY")
    access_token_minutes: int = Field(default=60 * 24 * 7, validation_alias="NETVAULT_TOKEN_MINUTES")
    # Keep enough headroom for multipart framing below Cloudflare's 100 MB
    # Free/Pro request limit. Operators on larger plans can override this.
    max_pdf_bytes: int = Field(default=95 * 1024 * 1024, validation_alias="NETVAULT_MAX_PDF_BYTES")
    max_upload_files: int = Field(default=25, validation_alias="NETVAULT_MAX_UPLOAD_FILES")
    max_batch_bytes: int = Field(default=95 * 1024 * 1024, validation_alias="NETVAULT_MAX_BATCH_BYTES")
    max_zip_files: int = Field(default=100, validation_alias="NETVAULT_MAX_ZIP_FILES")
    max_zip_bytes: int = Field(default=1024 * 1024 * 1024, validation_alias="NETVAULT_MAX_ZIP_BYTES")
    secure_cookies: bool = Field(default=False, validation_alias="NETVAULT_SECURE_COOKIES")
    base_path: str = Field(default="", validation_alias="NETVAULT_BASE_PATH")
    bootstrap_admin: str | None = Field(default=None, validation_alias="NETVAULT_BOOTSTRAP_ADMIN")
    bootstrap_admin_password: str | None = Field(
        default=None,
        validation_alias="NETVAULT_BOOTSTRAP_ADMIN_PASSWORD",
    )
    crossref_mailto: str | None = Field(default=None, validation_alias="NETVAULT_CROSSREF_MAILTO")
    crossref_user_agent: str = Field(
        default=f"NetVault/{__version__} (https://github.com/iihciyekub/netvault)",
        validation_alias="NETVAULT_CROSSREF_USER_AGENT",
    )
    search_query_max_chars: int = Field(default=200, validation_alias="NETVAULT_SEARCH_QUERY_MAX_CHARS")
    web_page_size: int = Field(default=25, validation_alias="NETVAULT_WEB_PAGE_SIZE")
    upload_rate_per_hour: int = Field(default=5000, validation_alias="NETVAULT_UPLOAD_RATE_PER_HOUR")
    download_rate_per_hour: int = Field(default=1200, validation_alias="NETVAULT_DOWNLOAD_RATE_PER_HOUR")
    min_storage_free_bytes: int = Field(
        default=5 * 1024 * 1024 * 1024,
        validation_alias="NETVAULT_MIN_STORAGE_FREE_BYTES",
    )
    database_pool_size: int = Field(default=10, validation_alias="NETVAULT_DATABASE_POOL_SIZE")
    database_max_overflow: int = Field(default=10, validation_alias="NETVAULT_DATABASE_MAX_OVERFLOW")
    database_statement_timeout_ms: int = Field(
        default=30_000,
        validation_alias="NETVAULT_DATABASE_STATEMENT_TIMEOUT_MS",
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
