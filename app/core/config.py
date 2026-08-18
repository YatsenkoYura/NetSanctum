"""
Centralized application configuration.

All values are loaded from environment variables (or .env file).
Modules MUST NOT define their own config — they read from this single source.
"""

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

_DOTENV_FILE = ".env" if os.getenv("NETSANCTUM_LOAD_DOTENV", "1") == "1" else None


class Settings(BaseSettings):
    """Immutable application-level settings loaded once at startup."""

    model_config = SettingsConfigDict(
        env_file=_DOTENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────
    APP_NAME: str = "NetSanctum"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    ENABLED_MODULES: str = ""
    ENABLED_MODULES_FILE: str = "/app/storage/config/enabled-modules.json"
    INSTALLED_MODULES: str = ""
    INSTALLED_MODULES_FILE: str = "/opt/netsanctum/installed-modules"
    REQUIRE_INSTALLED_MODULES_MARKER: bool = False
    ACCESS_TOKEN_HASH_PATH: str = "/app/storage/config/access_token.hash"
    ACCESS_TOKEN_PLAINTEXT_PATH: str = "/app/storage/config/access_token.txt"
    PUBLIC_BASE_URL: str = ""
    SECURE_COOKIES: bool = False
    TRUSTED_HOSTS: str = "localhost,127.0.0.1,testserver"
    ALLOW_REMOTE_METADATA_FETCH: bool = False

    # ── Observability ─────────────────────────────────────
    OBSERVABILITY_LOG_KEY: str = "netsanctum:logs"
    OBSERVABILITY_LOG_LIMIT: int = 1000
    OBSERVABILITY_LOG_TTL_SECONDS: int = 604800

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
