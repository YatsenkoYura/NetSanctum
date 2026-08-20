"""
Centralized application configuration.

All values are loaded from environment variables (or .env file).
Modules MUST NOT define their own config — they read from this single source.
"""

import os
from functools import lru_cache
from pathlib import Path

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
    NETSANCTUM_ENVIRONMENT: str = "development"
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

    # ── YouTube / yt-dlp ─────────────────────────────────
    YTDLP_CACHE_DIR: str = "/app/storage/.cache/yt-dlp"
    YOUTUBE_POT_PROVIDER_URL: str = ""
    YOUTUBE_YTDLP_PUBLIC_INTERVAL_SECONDS: float = 5.0
    YOUTUBE_YTDLP_AUTH_INTERVAL_SECONDS: float = 10.0
    YOUTUBE_YTDLP_REQUEST_INTERVAL_SECONDS: float = 1.0
    YOUTUBE_YTDLP_BACKOFF_SECONDS: int = 3600

    # ── API Key ──────────────────────────────────────────
    MASTER_API_KEY: str = "dev-api-key-change-me"

    # ── Encryption ───────────────────────────────────────
    FILE_ENCRYPTION_KEY_PATH: str = ""
    LEGACY_FILE_ENCRYPTION_KEYS_PATH: str = ""
    # Legacy migration key. Production encryption uses FILE_ENCRYPTION_KEY_PATH.
    FILE_ENCRYPTION_KEY: str = "dev-file-encryption-key-change-me"
    ENCRYPTION_MIGRATION_BATCH_SIZE: int = 1
    ENCRYPTION_MIGRATION_INTERVAL_SECONDS: float = 5.0
    ENCRYPTION_MIGRATION_IDLE_SECONDS: float = 300.0

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


def validate_runtime_security(settings: Settings | None = None) -> None:
    """Fail closed when a production process has unsafe secret configuration."""
    settings = settings or get_settings()
    if settings.NETSANCTUM_ENVIRONMENT.lower() != "production":
        return

    errors = []
    if len(settings.MASTER_API_KEY) < 32 or settings.MASTER_API_KEY == "dev-api-key-change-me":
        errors.append("MASTER_API_KEY must be a unique random value of at least 32 characters")
    if not settings.FILE_ENCRYPTION_KEY_PATH.strip():
        errors.append("FILE_ENCRYPTION_KEY_PATH must point to the generated runtime key")
    else:
        try:
            if len(Path(settings.FILE_ENCRYPTION_KEY_PATH).read_bytes().strip()) < 32:
                errors.append("runtime file-encryption key must contain at least 32 bytes")
        except OSError:
            errors.append("FILE_ENCRYPTION_KEY_PATH is not readable")
    if "change_me" in settings.DATABASE_URL or "change_me" in settings.DATABASE_URL_SYNC:
        errors.append("database credentials still contain a known placeholder")
    if settings.PUBLIC_BASE_URL.startswith("https://") and not settings.SECURE_COOKIES:
        errors.append("SECURE_COOKIES must be enabled for an HTTPS PUBLIC_BASE_URL")
    if errors:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))
