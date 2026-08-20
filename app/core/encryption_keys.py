import hashlib
import hmac
from pathlib import Path

from app.core.config import Settings, get_settings


def _root_encryption_key(settings: Settings | None = None) -> bytes:
    settings = settings or get_settings()
    key_path = settings.FILE_ENCRYPTION_KEY_PATH.strip()
    if key_path:
        try:
            key_material = Path(key_path).read_bytes().strip()
        except OSError as error:
            raise RuntimeError(f"Could not read FILE_ENCRYPTION_KEY_PATH: {key_path}") from error
        if len(key_material) < 32:
            raise RuntimeError("Runtime file-encryption key must contain at least 32 bytes")
        return hashlib.sha256(key_material).digest()

    if settings.NETSANCTUM_ENVIRONMENT.lower() == "production":
        raise RuntimeError("FILE_ENCRYPTION_KEY_PATH is required in production")

    # Development fallback. Production always uses the generated key file.
    return hashlib.sha256(settings.FILE_ENCRYPTION_KEY.encode("utf-8")).digest()


def primary_encryption_key(
    settings: Settings | None = None,
    *,
    purpose: str = "files",
) -> bytes:
    """Derive a purpose-specific key from the protected runtime root key."""
    if purpose not in {"files", "settings"}:
        raise ValueError(f"Unsupported encryption key purpose: {purpose}")
    root = _root_encryption_key(settings)
    return hmac.new(root, f"netsanctum:{purpose}:v2".encode(), hashlib.sha256).digest()


def legacy_encryption_keys(
    settings: Settings | None = None,
    *,
    purpose: str = "files",
) -> tuple[bytes, ...]:
    """Return historical keys accepted only while rotating persisted ciphertext."""
    settings = settings or get_settings()
    root = _root_encryption_key(settings)
    primary = primary_encryption_key(settings, purpose=purpose)
    candidates = (
        settings.FILE_ENCRYPTION_KEY,
        settings.MASTER_API_KEY,
        "dev-file-encryption-key-change-me",
        "dev-api-key-change-me",
    )
    legacy_file_values: tuple[str, ...] = ()
    legacy_path = settings.LEGACY_FILE_ENCRYPTION_KEYS_PATH.strip() if purpose == "files" else ""
    if legacy_path:
        try:
            legacy_file_values = tuple(
                value.strip() for value in Path(legacy_path).read_text().splitlines() if value.strip()
            )
        except FileNotFoundError:
            pass
        except OSError as error:
            raise RuntimeError(f"Could not read LEGACY_FILE_ENCRYPTION_KEYS_PATH: {legacy_path}") from error
    keys = (
        root,
        *(
            hashlib.sha256(value.encode("utf-8")).digest()
            for value in (*candidates, *legacy_file_values)
            if value
        ),
    )
    return tuple(key for key in dict.fromkeys(keys) if key != primary)
