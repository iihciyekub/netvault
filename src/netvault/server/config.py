from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field(
        default="sqlite:///./netvault-dev.db",
        validation_alias="NETVAULT_DATABASE_URL",
    )
    storage_root: Path = Field(default=Path("./storage"), validation_alias="NETVAULT_STORAGE_ROOT")
    secret_key: str = Field(default="dev-secret-change-me", validation_alias="NETVAULT_SECRET_KEY")
    access_token_minutes: int = Field(default=60 * 24 * 7, validation_alias="NETVAULT_TOKEN_MINUTES")
    max_pdf_bytes: int = Field(default=100 * 1024 * 1024, validation_alias="NETVAULT_MAX_PDF_BYTES")
    bootstrap_admin: str | None = Field(default=None, validation_alias="NETVAULT_BOOTSTRAP_ADMIN")
    bootstrap_admin_password: str | None = Field(
        default=None,
        validation_alias="NETVAULT_BOOTSTRAP_ADMIN_PASSWORD",
    )
    crossref_mailto: str | None = Field(default=None, validation_alias="NETVAULT_CROSSREF_MAILTO")
    crossref_user_agent: str = Field(
        default="NetVault/0.3.0 (https://github.com/iihciyekub/netvault)",
        validation_alias="NETVAULT_CROSSREF_USER_AGENT",
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
