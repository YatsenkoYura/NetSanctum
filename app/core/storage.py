"""
Storage abstraction layer.

Modules MUST NOT write to disk or S3 directly.
They use the `get_storage()` singleton which returns the active backend.
"""

import hashlib
import io
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings


class StorageInterface(ABC):
    """Abstract contract for all storage backends."""

    def _get_encryption_key(self) -> bytes:
        """Derive a 32-byte key from the configured file encryption key or master key."""
        settings = get_settings()
        raw_key = getattr(settings, "FILE_ENCRYPTION_KEY", None) or settings.MASTER_API_KEY
        return hashlib.sha256(raw_key.encode("utf-8")).digest()

    @abstractmethod
    def save_file(self, data: bytes, path: str) -> str:
        """
        Persist binary data at the given logical path.
        Returns the canonical path/key where the file was stored.
        """
        ...

    def save_file_encrypted(self, data: bytes, path: str) -> str:
        """
        Encrypt binary data using AES-256-GCM and persist it at the given logical path.
        Returns the canonical path/key where the file was stored.
        """
        key = self._get_encryption_key()
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        encrypted_data = aesgcm.encrypt(nonce, data, None)
        # Prepend the 12-byte nonce to the ciphertext
        payload = nonce + encrypted_data
        return self.save_file(payload, path)

    @abstractmethod
    def get_file_stream(self, path: str) -> BinaryIO:
        """
        Return a readable binary stream for the file at the given path.
        Raises FileNotFoundError if the file does not exist.
        """
        ...

    def get_file_stream_decrypted(self, path: str) -> BinaryIO:
        """
        Retrieve the encrypted file, decrypt it using AES-256-GCM, and return a readable stream.
        """
        key = self._get_encryption_key()
        stream = self.get_file_stream(path)
        try:
            payload = stream.read()
        finally:
            stream.close()

        if len(payload) < 12:
            raise ValueError(f"Invalid encrypted file '{path}': payload is too short.")

        nonce = payload[:12]
        ciphertext = payload[12:]
        aesgcm = AESGCM(key)
        try:
            decrypted_data = aesgcm.decrypt(nonce, ciphertext, None)
        except Exception as e:
            raise ValueError(f"Failed to decrypt file '{path}': {e}")

        return io.BytesIO(decrypted_data)

    def get_file_decrypted(self, path: str) -> bytes:
        """
        Retrieve the encrypted file, decrypt it, and return its raw bytes.
        """
        with self.get_file_stream_decrypted(path) as f:
            return f.read()

    @abstractmethod
    def delete_file(self, path: str) -> bool:
        """
        Delete the file at the given path.
        Returns True if deleted, False if not found.
        """
        ...

    @abstractmethod
    def file_exists(self, path: str) -> bool:
        """Check whether a file exists at the given path."""
        ...


class LocalStorage(StorageInterface):
    """File-system storage backend for development."""

    def __init__(self, root_dir: str) -> None:
        self._root = Path(root_dir).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _full_path(self, path: str) -> Path:
        """Resolve and sanitize the path to prevent directory traversal."""
        resolved = (self._root / path).resolve()
        if not str(resolved).startswith(str(self._root)):
            raise ValueError(f"Path traversal detected: {path}")
        return resolved

    def save_file(self, data: bytes, path: str) -> str:
        full = self._full_path(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        return str(path)

    def get_file_stream(self, path: str) -> BinaryIO:
        full = self._full_path(path)
        if not full.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        return open(full, "rb")

    def delete_file(self, path: str) -> bool:
        full = self._full_path(path)
        if full.is_file():
            full.unlink()
            return True
        return False

    def file_exists(self, path: str) -> bool:
        return self._full_path(path).is_file()


class S3Storage(StorageInterface):
    """
    AWS S3 storage backend.

    Ready-to-use implementation — just set STORAGE_BACKEND=s3 and
    provide the S3_* / AWS_* environment variables.
    """

    def __init__(
        self,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        endpoint_url: str | None = None,
    ) -> None:
        import boto3

        session_kwargs: dict = {
            "region_name": region,
        }
        if access_key and secret_key:
            session_kwargs["aws_access_key_id"] = access_key
            session_kwargs["aws_secret_access_key"] = secret_key

        session = boto3.Session(**session_kwargs)
        client_kwargs: dict = {}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url

        self._client = session.client("s3", **client_kwargs)
        self._bucket = bucket

    def save_file(self, data: bytes, path: str) -> str:
        self._client.put_object(Bucket=self._bucket, Key=path, Body=data)
        return path

    def get_file_stream(self, path: str) -> BinaryIO:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=path)
            return response["Body"]
        except self._client.exceptions.NoSuchKey:
            raise FileNotFoundError(f"S3 object not found: {path}")

    def delete_file(self, path: str) -> bool:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=path)
            return True
        except Exception:
            return False

    def file_exists(self, path: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=path)
            return True
        except Exception:
            return False


# ── Factory ──────────────────────────────────────────────
_storage_instance: StorageInterface | None = None


def get_storage() -> StorageInterface:
    """Return the active storage backend (singleton)."""
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance

    settings = get_settings()

    if settings.STORAGE_BACKEND == "s3":
        _storage_instance = S3Storage(
            bucket=settings.S3_BUCKET_NAME,
            region=settings.S3_REGION,
            access_key=settings.AWS_ACCESS_KEY_ID,
            secret_key=settings.AWS_SECRET_ACCESS_KEY,
            endpoint_url=settings.S3_ENDPOINT_URL or None,
        )
    else:
        _storage_instance = LocalStorage(root_dir=settings.LOCAL_STORAGE_ROOT)

    return _storage_instance
