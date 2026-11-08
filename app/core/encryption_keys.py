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
    """Historical keys accepted only while rotating persisted ciphertext.

    The normal read path tries these after the primary key, and the migration
    rewrites whatever they open. That fallback is what keeps files readable
    until the migration runs — and it is also why publicly known development
    values must never be fallback candidates in production: anyone able to
    plant a file sealed under a published dev key would otherwise produce
    ciphertext this deployment accepts as its own.

    The dev placeholders are always excluded. Setting ENCRYPTION_ALLOW_LEGACY_KEYS
    to False disables the fallback entirely once the migration reports
    pending=0 and unreadable=0.
    """
    settings = settings or get_settings()
    if not settings.ENCRYPTION_ALLOW_LEGACY_KEYS:
        return ()

    root = _root_encryption_key(settings)
    primary = primary_encryption_key(settings, purpose=purpose)

    # Only configuration-derived values — no hard-coded dev strings.
    # If an operator leaves the defaults unchanged, validate_runtime_security
    # will refuse the deployment in production; in development the values are
    # still known, so they must never be accepted as legacy decryption keys.
    dev_placeholders = {"dev-file-encryption-key-change-me", "dev-api-key-change-me"}
    candidates = (
        settings.FILE_ENCRYPTION_KEY,
        settings.MASTER_API_KEY,
    )
    candidates = tuple(value for value in candidates if value and value not in dev_placeholders)

    legacy_file_values: tuple[str, ...] = ()
    legacy_path = settings.LEGACY_FILE_ENCRYPTION_KEYS_PATH.strip()
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
