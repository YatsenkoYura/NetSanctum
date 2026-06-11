"""
Centralized application configuration.

All values are loaded from environment variables (or .env file).
Modules MUST NOT define their own config — they read from this single source.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Immutable application-level settings loaded once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────
    APP_NAME: str = "NetSanctum"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    # ── Database ─────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://netsanctum:change_me@localhost:5432/netsanctum"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://netsanctum:change_me@localhost:5432/netsanctum"

    # ── Redis / Celery ───────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"



    # ── API Key ──────────────────────────────────────────
    MASTER_API_KEY: str = "dev-api-key-change-me"

    # ── Encryption ───────────────────────────────────────
    FILE_ENCRYPTION_KEY: str = "dev-file-encryption-key-change-me"

    # ── Storage ──────────────────────────────────────────
    STORAGE_BACKEND: str = "local"  # "local" | "s3"
    LOCAL_STORAGE_ROOT: str = "./storage"

    # S3 settings (used when STORAGE_BACKEND=s3)
    S3_BUCKET_NAME: str = ""
    S3_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    S3_ENDPOINT_URL: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton of application settings."""
    return Settings()
