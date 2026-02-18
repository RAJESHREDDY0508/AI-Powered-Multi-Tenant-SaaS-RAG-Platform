"""
Application configuration via environment variables (12-factor).
Pydantic BaseSettings validates and coerces all values at startup.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    database_url: str  # asyncpg DSN, e.g. postgresql+asyncpg://user:pass@host/db

    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo_sql: bool = False   # set True in local dev to log queries

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app_env: str = "development"   # development | staging | production
    debug: bool = False

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
