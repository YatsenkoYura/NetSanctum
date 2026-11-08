"""Fernet-based encryption utilities for storing mail secrets in the database.

Uses the same root key material as file encryption, deriving a separate
purpose-specific key so that a leaked mail password does not compromise
other encrypted data.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Purpose tag is deliberately distinct from file/settings keys used in
# encryption_keys.py so that compromise of one purpose cannot be reused
# for another without re-deriving the root.
_MAIL_KDF_PURPOSE = b"netsanctum:mail:secrets:v1"


def _derive_mail_key(raw_root: bytes) -> bytes:
    """Derive a 32-byte Fernet-compatible key from the raw root key."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_MAIL_KDF_PURPOSE,
    )
    return hkdf.derive(raw_root)


def _get_root_key() -> bytes:
    """Return the raw root encryption key (same logic as encryption_keys._root_encryption_key)."""
    settings = get_settings()
    key_path = settings.FILE_ENCRYPTION_KEY_PATH.strip()
    if key_path:
        try:
            key_material = open(key_path, "rb").read().strip()
        except OSError as error:
            raise RuntimeError(f"Could not read FILE_ENCRYPTION_KEY_PATH: {key_path}") from error
        if len(key_material) < 32:
            raise RuntimeError("Runtime file-encryption key must contain at least 32 bytes")
        import hashlib

        return hashlib.sha256(key_material).digest()

    if settings.NETSANCTUM_ENVIRONMENT.lower() == "production":
        raise RuntimeError("FILE_ENCRYPTION_KEY_PATH is required in production")

    import hashlib

    return hashlib.sha256(settings.FILE_ENCRYPTION_KEY.encode("utf-8")).digest()


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """Return a Fernet instance seeded from the project's root encryption key."""
    raw = _get_root_key()
    fernet_key = _derive_mail_key(raw)
    import base64

    fernet_key = base64.urlsafe_b64encode(fernet_key)
    return Fernet(fernet_key)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt *plaintext* with Fernet and return a URL-safe base64 string."""
    fernet = _get_fernet()
    return fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext_b64: str) -> str:
    """Decrypt a Fernet-encrypted string back to plain text.

    Raises ``ValueError`` when the ciphertext is malformed or decryption
    fails (wrong key, tampered data, expired token).
    """
    fernet = _get_fernet()
    try:
        return fernet.decrypt(ciphertext_b64.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Cannot decrypt secret (invalid or tampered)") from exc


def decrypt_or_raise(ciphertext_b64: str | None, field: str = "secret") -> str:
    """Return the decrypted value or raise ``ValueError`` on failure.

    Convenience wrapper used by service layer when the plaintext is
    required to send a message or open an IMAP connection.
    """
    if not ciphertext_b64:
        raise ValueError(f"{field} is empty")
    return decrypt_secret(ciphertext_b64)
