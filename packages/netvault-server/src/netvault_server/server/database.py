from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from netvault_server.server.config import get_settings


class Base(DeclarativeBase):
    pass


def _connect_args(database_url: str) -> dict[str, object]:
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {"options": f"-c statement_timeout={get_settings().database_statement_timeout_ms}"}


def _pool_args(database_url: str) -> dict[str, object]:
    if database_url.startswith("sqlite"):
        return {}
    settings = get_settings()
    return {
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_max_overflow,
        "pool_timeout": 30,
        "pool_recycle": 1800,
    }


engine = create_engine(
    get_settings().database_url,
    connect_args=_connect_args(get_settings().database_url),
    pool_pre_ping=True,
    **_pool_args(get_settings().database_url),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
