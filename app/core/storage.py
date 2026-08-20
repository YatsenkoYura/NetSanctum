"""
Storage abstraction layer.

Modules MUST NOT write to disk or S3 directly.
They use the `get_storage()` singleton which returns the active backend.
"""

import io
import os
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings
from app.core.encryption_keys import legacy_encryption_keys, primary_encryption_key

ENCRYPTED_FILE_MAGIC = b"NSENC\x02\x00\x00"
NONCE_SIZE = 12


@dataclass(frozen=True, slots=True)
class EncryptionMigrationResult:
    migrated: int = 0
    current: int = 0
    unreadable: int = 0
    pending: int = 0
    examined: int = 0


class StorageInterface(ABC):
    """Abstract contract for all storage backends."""

    def _get_encryption_key(self) -> bytes:
        """Load the primary key from the protected runtime key file."""
        return primary_encryption_key()

    def _get_legacy_encryption_keys(self) -> tuple[bytes, ...]:
        return legacy_encryption_keys(purpose="files")

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
        return self.save_file(self._encrypt_payload(data, path), path)

    @staticmethod
    def _associated_data(path: str) -> bytes:
        return ENCRYPTED_FILE_MAGIC + b"\x00" + path.encode("utf-8")

    def _encrypt_payload(self, data: bytes, path: str) -> bytes:
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = AESGCM(self._get_encryption_key()).encrypt(
            nonce,
            data,
            self._associated_data(path),
        )
        return ENCRYPTED_FILE_MAGIC + nonce + ciphertext

    def _decrypt_payload(self, payload: bytes, path: str) -> bytes:
        is_current = payload.startswith(ENCRYPTED_FILE_MAGIC)
        offset = len(ENCRYPTED_FILE_MAGIC) if is_current else 0
        if len(payload) < offset + NONCE_SIZE + 16:
            raise ValueError(f"Invalid encrypted file '{path}': payload is too short.")

        nonce = payload[offset : offset + NONCE_SIZE]
        ciphertext = payload[offset + NONCE_SIZE :]
        associated_data = self._associated_data(path) if is_current else None
        last_error = None
        for key in (self._get_encryption_key(), *self._get_legacy_encryption_keys()):
            try:
                return AESGCM(key).decrypt(nonce, ciphertext, associated_data)
            except Exception as error:
                last_error = error
        raise ValueError(f"Failed to decrypt file '{path}': {last_error}")

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
        stream = self.get_file_stream(path)
        try:
            payload = stream.read()
        finally:
            stream.close()

        return io.BytesIO(self._decrypt_payload(payload, path))

    def get_file_decrypted(self, path: str) -> bytes:
        """
        Retrieve the encrypted file, decrypt it, and return its raw bytes.
        """
        with self.get_file_stream_decrypted(path) as f:
            return f.read()

    def get_encrypted_plaintext_size(self, path: str) -> int:
        """Return the envelope's plaintext length without decrypting the whole object."""
        with self.get_file_stream(path) as stream:
            prefix = stream.read(len(ENCRYPTED_FILE_MAGIC))
        stored_size = self.get_file_size(path)
        overhead = NONCE_SIZE + 16
        if prefix == ENCRYPTED_FILE_MAGIC:
            overhead += len(ENCRYPTED_FILE_MAGIC)
        plaintext_size = stored_size - overhead
        if plaintext_size < 0:
            raise ValueError(f"Invalid encrypted file '{path}': payload is too short.")
        return plaintext_size

    @abstractmethod
    def delete_file(self, path: str) -> bool:
        """
        Delete the file at the given path.
        Returns True if deleted, False if not found.
        """
        ...

    @abstractmethod
    def get_file_size(self, path: str) -> int:
        """Return the stored object size in bytes."""
        ...

    @abstractmethod
    def file_exists(self, path: str) -> bool:
        """Check whether a file exists at the given path."""
        ...

    def migrate_legacy_encryption_batch(self, limit: int = 1) -> EncryptionMigrationResult:
        return EncryptionMigrationResult()

    def migrate_legacy_encryption(self) -> int:
        return self.migrate_legacy_encryption_batch(limit=0).migrated


class LocalStorage(StorageInterface):
    """File-system storage backend for development."""

    def __init__(self, root_dir: str) -> None:
        self._root = Path(root_dir).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._unreadable_encrypted_files: dict[str, tuple[int, int]] = {}
        self._known_legacy_keys: tuple[bytes, ...] | None = None

    def _full_path(self, path: str) -> Path:
        """Resolve and sanitize the path to prevent directory traversal."""
        resolved = (self._root / path).resolve()
        if not resolved.is_relative_to(self._root):
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

    def get_file_size(self, path: str) -> int:
        return self._full_path(path).stat().st_size

    def file_exists(self, path: str) -> bool:
        return self._full_path(path).is_file()

    def migrate_legacy_encryption_batch(self, limit: int = 1) -> EncryptionMigrationResult:
        """Atomically rewrite a bounded number of legacy encrypted objects."""
        legacy_keys = self._get_legacy_encryption_keys()
        if legacy_keys != self._known_legacy_keys:
            self._unreadable_encrypted_files.clear()
            self._known_legacy_keys = legacy_keys
        migrated = 0
        current = 0
        pending = 0
        examined = 0
        for full_path in sorted(self._root.rglob("*.enc")):
            with full_path.open("rb") as stream:
                prefix = stream.read(len(ENCRYPTED_FILE_MAGIC))
                if prefix == ENCRYPTED_FILE_MAGIC:
                    current += 1
                    continue
                stat = os.fstat(stream.fileno())
                signature = (stat.st_size, stat.st_mtime_ns)
                cache_key = str(full_path)
                if self._unreadable_encrypted_files.get(cache_key) == signature:
                    continue
                if limit > 0 and examined >= limit:
                    pending += 1
                    continue
                examined += 1
                payload = prefix + stream.read()
            try:
                plaintext = self._decrypt_payload(payload, str(full_path.relative_to(self._root)))
            except ValueError:
                self._unreadable_encrypted_files[cache_key] = signature
                continue
            relative_path = str(full_path.relative_to(self._root))
            replacement = self._encrypt_payload(plaintext, relative_path)
            if not secrets.compare_digest(
                self._decrypt_payload(replacement, relative_path),
                plaintext,
            ):
                raise RuntimeError(f"Encryption migration verification failed: {cache_key}")
            temporary = full_path.with_name(f".{full_path.name}.rotate-{secrets.token_hex(8)}")
            with temporary.open("xb") as stream:
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, full_path)
            self._unreadable_encrypted_files.pop(cache_key, None)
            migrated += 1
        return EncryptionMigrationResult(
            migrated,
            current,
            len(self._unreadable_encrypted_files),
            pending,
            examined,
        )


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
        self._unreadable_encrypted_files: dict[str, tuple[int, str]] = {}
        self._known_legacy_keys: tuple[bytes, ...] | None = None

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

    def get_file_size(self, path: str) -> int:
        response = self._client.head_object(Bucket=self._bucket, Key=path)
        return int(response["ContentLength"])

    def file_exists(self, path: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=path)
            return True
        except Exception:
            return False

    def migrate_legacy_encryption_batch(self, limit: int = 1) -> EncryptionMigrationResult:
        legacy_keys = self._get_legacy_encryption_keys()
        if legacy_keys != self._known_legacy_keys:
            self._unreadable_encrypted_files.clear()
            self._known_legacy_keys = legacy_keys
        migrated = 0
        current = 0
        pending = 0
        examined = 0
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket):
            for item in page.get("Contents", []):
                key = item.get("Key", "")
                if not key.endswith(".enc"):
                    continue
                signature = (int(item.get("Size", 0)), str(item.get("ETag", "")))
                if self._unreadable_encrypted_files.get(key) == signature:
                    continue
                if limit > 0 and examined >= limit:
                    pending += 1
                    continue
                examined += 1
                with self.get_file_stream(key) as stream:
                    payload = stream.read()
                if payload.startswith(ENCRYPTED_FILE_MAGIC):
                    current += 1
                    continue
                try:
                    plaintext = self._decrypt_payload(payload, key)
                except ValueError:
                    self._unreadable_encrypted_files[key] = signature
                    continue
                replacement = self._encrypt_payload(plaintext, key)
                if not secrets.compare_digest(self._decrypt_payload(replacement, key), plaintext):
                    raise RuntimeError(f"Encryption migration verification failed: {key}")
                self._client.put_object(Bucket=self._bucket, Key=key, Body=replacement)
                self._unreadable_encrypted_files.pop(key, None)
                migrated += 1
        return EncryptionMigrationResult(
            migrated,
            current,
            len(self._unreadable_encrypted_files),
            pending,
            examined,
        )


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
