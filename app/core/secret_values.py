import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.encryption_keys import legacy_encryption_keys, primary_encryption_key

SECRET_PREFIX = "enc:v1:"


def _key() -> bytes:
    return primary_encryption_key(purpose="settings")


def _decryption_keys() -> tuple[bytes, ...]:
    return (_key(), *legacy_encryption_keys(purpose="settings"))


def encrypt_secret_value(value: str) -> str:
    if value.startswith(SECRET_PREFIX):
        return value
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key()).encrypt(nonce, value.encode("utf-8"), None)
    return SECRET_PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret_value(value: str | None) -> str:
    if not value or not value.startswith(SECRET_PREFIX):
        return value or ""
    payload = base64.urlsafe_b64decode(value.removeprefix(SECRET_PREFIX))
    last_error = None
    for key in _decryption_keys():
        try:
            return AESGCM(key).decrypt(payload[:12], payload[12:], None).decode("utf-8")
        except Exception as error:
            last_error = error
    raise ValueError(f"Could not decrypt secret setting: {last_error}")


def rotate_secret_value(value: str) -> str:
    """Re-encrypt a secret with the current primary key, including legacy ciphertext."""
    plaintext = decrypt_secret_value(value)
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return SECRET_PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def secret_value_uses_current_key(value: str) -> bool:
    if not value.startswith(SECRET_PREFIX):
        return False
    try:
        payload = base64.urlsafe_b64decode(value.removeprefix(SECRET_PREFIX))
        AESGCM(_key()).decrypt(payload[:12], payload[12:], None)
        return True
    except Exception:
        return False
