"""
Storage abstraction layer.

Modules MUST NOT write to disk or S3 directly.
They use the `get_storage()` singleton which returns the active backend.
"""

import hashlib
import io
import os
import secrets
import shutil
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings
from app.core.encryption_keys import legacy_encryption_keys, primary_encryption_key

ENCRYPTED_FILE_MAGIC = b"NSENC\x02\x00\x00"
NONCE_SIZE = 12
# Filename marker for an encrypted object. It is a naming convention, never a
# reliable signal on its own — `looks_encrypted` reads the header — but it is what
# lets a download be named after the file inside the envelope.
ENCRYPTED_SUFFIX = ".enc"

# Chunked, seekable envelope. The chunk index lives in the nonce, so no
# (key, nonce) pair repeats across chunks, and each chunk authenticates on its own.
SEEKABLE_MAGIC = b"NSENCS\x01\x00\x00"
SEEKABLE_CHUNK_SIZE = 1024 * 1024
SEEKABLE_FILE_NONCE_SIZE = 8
# magic | file nonce | chunk size | plaintext length. The length is stored rather
# than derived, because the final chunk is short and the count cannot be recovered
# from the stored size alone.
SEEKABLE_HEADER_SIZE = len(SEEKABLE_MAGIC) + SEEKABLE_FILE_NONCE_SIZE + 4 + 8

# Second version of the chunked envelope. v1 bound each chunk to the path and the
# file nonce only: the plaintext length in the header was unauthenticated, so a
# rewritten length changed what the size helpers reported while every chunk still
# verified. v2 binds the length, the chunk size and the chunk count into every
# chunk's associated data, so a header edit voids the chunks instead of the report.
# New writes use v2; v1 objects keep reading with their original associated data.
SEEKABLE_MAGIC_V2 = b"NSENCS\x02\x00\x00"
# magic | file nonce | chunk size | plaintext length | chunk count.
SEEKABLE_HEADER_V2_SIZE = len(SEEKABLE_MAGIC_V2) + SEEKABLE_FILE_NONCE_SIZE + 4 + 8 + 4
SEEKABLE_HEADER_MAX_SIZE = max(SEEKABLE_HEADER_SIZE, SEEKABLE_HEADER_V2_SIZE)


def stream_size_sha256(stream: BinaryIO, chunk_size: int = 1024 * 1024) -> tuple[int, str]:
    """Read a stream incrementally and return its byte length and SHA-256 digest."""
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(chunk_size):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def file_size_sha256(path: str | os.PathLike[str]) -> tuple[int, str]:
    with open(path, "rb") as stream:
        return stream_size_sha256(stream)


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

    def save_stream(self, stream: BinaryIO, path: str) -> str:
        """Persist a readable binary stream without requiring callers to buffer it."""
        return self.save_file(stream.read(), path)

    def save_file_from_path(self, source_path: str | os.PathLike[str], path: str) -> str:
        """Persist a file from disk using the backend's streaming implementation."""
        with open(source_path, "rb") as stream:
            return self.save_stream(stream, path)

    def save_file_encrypted(self, data: bytes, path: str, *, key: bytes | None = None) -> str:
        """
        Encrypt binary data using AES-256-GCM and persist it at the given logical path.
        Returns the canonical path/key where the file was stored.
        """
        return self.save_file(self._encrypt_payload(data, path, key=key), path)

    @staticmethod
    def _associated_data(path: str) -> bytes:
        return ENCRYPTED_FILE_MAGIC + b"\x00" + path.encode("utf-8")

    def _encrypt_payload(self, data: bytes, path: str, *, key: bytes | None = None) -> bytes:
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = AESGCM(key if key is not None else self._get_encryption_key()).encrypt(
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

    def get_file_stream_decrypted(self, path: str, *, key: bytes | None = None) -> BinaryIO:
        """
        Retrieve the encrypted file, decrypt it using AES-256-GCM, and return a readable stream.
        """
        if self.is_seekable_encrypted(path):
            # Callers such as alllib and computercraft reach the file through this
            # one method, so it has to speak both envelopes.
            total = self.get_seekable_plaintext_size(path)
            return io.BytesIO(b"".join(self.read_seekable_range(path, 0, total, key=key)))

        stream = self.get_file_stream(path)
        try:
            payload = stream.read()
        finally:
            stream.close()

        return io.BytesIO(self._decrypt_payload(payload, path))

    # ── Seekable envelope ────────────────────────────────────────────────
    # AES-GCM over one blob cannot be seeked: the GHASH tag spans the whole
    # ciphertext, so serving `bytes=1000-2000` would mean decrypting from byte
    # zero. Video needs seeking, so large objects use a chunked envelope where
    # every chunk is its own AEAD with the path bound into its associated data.
    # A range then touches only the chunks it covers.

    def save_file_encrypted_seekable(self, stream: BinaryIO, path: str, *, key: bytes | None = None) -> str:
        """Encrypt a stream into the chunked (v2) envelope without buffering it whole.

        An explicit `key` encrypts under that key instead of the application file
        key — sealed Vault collections keep a per-collection file key this way.
        Whatever the key, only that key opens the object: an explicit key is never
        mixed with the legacy rotation the application key gets, because a wrong
        file key must fail rather than fall through to an unrelated one.
        """
        file_nonce = os.urandom(SEEKABLE_FILE_NONCE_SIZE)
        # The count has to be known before the first chunk is sealed, because it
        # is part of every chunk's associated data. The input stream is spooled
        # to disk first — chunked, so RAM stays flat — which costs one transient
        # copy of the plaintext next to the ciphertext the writer already holds.
        with tempfile.TemporaryDirectory(prefix="seekenc_") as workdir:
            spooled = Path(workdir) / "plaintext"
            plaintext_length = 0
            with spooled.open("wb") as sink:
                while True:
                    piece = stream.read(SEEKABLE_CHUNK_SIZE)
                    if not piece:
                        break
                    sink.write(piece)
                    plaintext_length += len(piece)
            chunk_count = (plaintext_length + SEEKABLE_CHUNK_SIZE - 1) // SEEKABLE_CHUNK_SIZE
            aad = self._seekable_v2_associated_data(path, file_nonce, plaintext_length, chunk_count)
            aesgcm = AESGCM(key if key is not None else self._get_encryption_key())
            temporary = Path(workdir) / "payload"
            with spooled.open("rb") as source, temporary.open("wb") as sink:
                sink.write(SEEKABLE_MAGIC_V2 + file_nonce + SEEKABLE_CHUNK_SIZE.to_bytes(4, "big"))
                sink.write(plaintext_length.to_bytes(8, "big"))
                sink.write(chunk_count.to_bytes(4, "big"))
                index = 0
                while True:
                    chunk = source.read(SEEKABLE_CHUNK_SIZE)
                    if not chunk:
                        break
                    index_bytes = index.to_bytes(4, "big")
                    sink.write(aesgcm.encrypt(file_nonce + index_bytes, chunk, aad + index_bytes))
                    index += 1
            return self.save_file_from_path(temporary, path)

    @staticmethod
    def _seekable_version(header: bytes) -> int:
        """Which chunked envelope a stored object uses, from its first bytes."""
        if header.startswith(SEEKABLE_MAGIC_V2):
            return 2
        if header.startswith(SEEKABLE_MAGIC):
            return 1
        return 0

    def seekable_envelope_version(self, path: str) -> int:
        """0 for a plain object, 1 or 2 for a chunked envelope.

        Read from the header, never the name: which version an object is comes
        from its first bytes, and a migration that trusted the filename would
        rewrite files that were already fine and skip files that were not.
        """
        with self.get_file_stream(path) as stream:
            return self._seekable_version(stream.read(SEEKABLE_HEADER_MAX_SIZE))

    @staticmethod
    def upgraded_envelope_path(path: str) -> str:
        """Where the v2 copy of a v1 object belongs.

        A new name, because the envelope binds its own path: rewriting in place
        would invalidate every chunk it had already written. The marker goes
        before `.enc` so the suffix parsers (`media_type_for`, the storage
        browser's inner-name guess) still see a file they recognise.
        """
        if path.endswith(ENCRYPTED_SUFFIX):
            return f"{path[: -len(ENCRYPTED_SUFFIX)]}.v2{ENCRYPTED_SUFFIX}"
        return f"{path}.v2"

    def upgrade_seekable_envelope(self, path: str, *, key: bytes | None = None) -> str:
        """Rewrite a v1 chunked object as v2 and return the new path.

        The new object is written in full, read back and compared before this
        returns, so a caller that gets a path back has a verified file: the
        verification is here rather than in the caller because every caller
        would otherwise have to remember it, and a migration that does not
        verify is a migration that eats files.

        Raises ValueError for anything it cannot do safely — a v2 object, a
        plain object, a file that does not open — and leaves the original in
        place in every one of those cases. Deleting the old file is the
        caller's decision, once the row points at the new one.
        """
        version = self.seekable_envelope_version(path)
        if version != 1:
            raise ValueError(f"'{path}' is not a v1 seekable object")
        total = self.get_seekable_plaintext_size(path)
        plaintext = b"".join(self.read_seekable_range(path, 0, total, key=key))
        if len(plaintext) != total:
            raise ValueError(f"'{path}' decrypted short")
        target = self.upgraded_envelope_path(path)
        with tempfile.TemporaryDirectory(prefix="envelope_") as workdir:
            staged = Path(workdir) / "payload"
            with staged.open("wb") as sink:
                sink.write(plaintext)
            with staged.open("rb") as source:
                self.save_file_encrypted_seekable(source, target, key=key)
        if self.seekable_envelope_version(target) != 2:
            raise ValueError(f"'{target}' did not come out as v2")
        check_total = self.get_seekable_plaintext_size(target)
        if check_total != total:
            raise ValueError(f"'{target}' has a different length than '{path}'")
        restored = b"".join(self.read_seekable_range(target, 0, check_total, key=key))
        if not secrets.compare_digest(restored, plaintext):
            raise ValueError(f"'{target}' does not read back as '{path}' did")
        return target

    def is_seekable_encrypted(self, path: str) -> bool:
        """Whether the stored object uses the chunked envelope, either version."""
        with self.get_file_stream(path) as stream:
            return self._seekable_version(stream.read(len(SEEKABLE_MAGIC_V2))) > 0

    def get_seekable_plaintext_size(self, path: str) -> int:
        """Plaintext length of a chunked envelope, computed from stored sizes alone."""
        with self.get_file_stream(path) as stream:
            header = stream.read(SEEKABLE_HEADER_MAX_SIZE)
        version = self._seekable_version(header)
        if version == 2 and len(header) >= SEEKABLE_HEADER_V2_SIZE:
            base = len(SEEKABLE_MAGIC_V2) + SEEKABLE_FILE_NONCE_SIZE + 4
            return int.from_bytes(header[base : base + 8], "big")
        if version == 1 and len(header) >= SEEKABLE_HEADER_SIZE:
            base = len(SEEKABLE_MAGIC) + SEEKABLE_FILE_NONCE_SIZE + 4
            return int.from_bytes(header[base : base + 8], "big")
        raise ValueError(f"Invalid encrypted file '{path}': header is truncated.")

    def read_seekable_range(
        self, path: str, start: int, length: int, *, key: bytes | None = None
    ) -> Iterator[bytes]:
        """Yield plaintext bytes `[start, start + length)` in chunks.

        Only the chunks that overlap the range are read and decrypted, so a seek
        into a large video costs one or two chunks rather than the whole file.
        """
        if length <= 0:
            return
        with self.get_file_stream(path) as stream:
            header = stream.read(SEEKABLE_HEADER_MAX_SIZE)
            version = self._seekable_version(header)
            if version == 2:
                if len(header) < SEEKABLE_HEADER_V2_SIZE:
                    raise ValueError(f"Invalid encrypted file '{path}': header is truncated.")
                yield from self._read_seekable_v2_range(stream, header, path, start, length, key)
                return
            if version == 1:
                yield from self._read_seekable_v1_range(stream, header, path, start, length, key)
                return
            raise ValueError(f"File '{path}' is not a seekable encrypted object.")

    def _read_seekable_v1_range(
        self, stream, header: bytes, path: str, start: int, length: int, key
    ) -> Iterator[bytes]:
        """The original envelope: chunks bound to the path and the file nonce."""
        magic = len(SEEKABLE_MAGIC)
        file_nonce = header[magic : magic + SEEKABLE_FILE_NONCE_SIZE]
        aad = self._seekable_associated_data(path, file_nonce)
        yield from self._read_seekable_chunks(
            stream,
            SEEKABLE_HEADER_SIZE,
            SEEKABLE_CHUNK_SIZE,
            file_nonce,
            aad,
            path,
            start,
            length,
            key,
            plaintext_length=int.from_bytes(
                header[magic + SEEKABLE_FILE_NONCE_SIZE + 4 : magic + SEEKABLE_FILE_NONCE_SIZE + 12], "big"
            ),
        )

    def _read_seekable_v2_range(
        self, stream, header: bytes, path: str, start: int, length: int, key
    ) -> Iterator[bytes]:
        """The current envelope: the header's sizes are authenticated, not trusted."""
        magic = len(SEEKABLE_MAGIC_V2)
        file_nonce = header[magic : magic + SEEKABLE_FILE_NONCE_SIZE]
        base = magic + SEEKABLE_FILE_NONCE_SIZE
        chunk_size = int.from_bytes(header[base : base + 4], "big")
        plaintext_length = int.from_bytes(header[base + 4 : base + 12], "big")
        chunk_count = int.from_bytes(header[base + 12 : base + 16], "big")
        if chunk_size <= 0 or chunk_size > 64 * 1024 * 1024:
            raise ValueError(f"Invalid encrypted file '{path}': bad chunk size.")
        expected = (plaintext_length + chunk_size - 1) // chunk_size if plaintext_length else 0
        if chunk_count != expected:
            raise ValueError(f"Invalid encrypted file '{path}': header sizes disagree.")
        aad = self._seekable_v2_associated_data(path, file_nonce, plaintext_length, chunk_count)
        yield from self._read_seekable_chunks(
            stream,
            SEEKABLE_HEADER_V2_SIZE,
            chunk_size,
            file_nonce,
            aad,
            path,
            start,
            length,
            key,
            plaintext_length=plaintext_length,
        )

    def _read_seekable_chunks(
        self,
        stream,
        offset: int,
        chunk_size: int,
        file_nonce: bytes,
        aad: bytes,
        path: str,
        start: int,
        length: int,
        key,
        *,
        plaintext_length: int | None = None,
    ) -> Iterator[bytes]:
        # Every chunk but the last is exactly chunk_size bytes, so the
        # covering chunk can be computed instead of found by reading forward.
        wanted_end = start + length
        index = start // chunk_size
        position = index * chunk_size
        while position < wanted_end:
            if plaintext_length is not None and position >= plaintext_length:
                # Past the end the header promises. A read past EOF ends here;
                # a file that ends *before* its promise fails below instead.
                return
            stream.seek(offset + index * (chunk_size + 16))
            stored = stream.read(chunk_size + 16)
            if not stored:
                # The object ends before its header says it does. Returning short
                # would serve a truncated file as whole; the header is covered by
                # the chunk AAD, so a mismatch here is damage, not a short file.
                raise ValueError(f"Invalid encrypted file '{path}': object is truncated.")
            chunk_plain = len(stored) - 16
            chunk_start = position
            chunk_end = position + chunk_plain
            if chunk_end > start:
                index_bytes = index.to_bytes(4, "big")
                plaintext = self._decrypt_chunk(
                    stored,
                    file_nonce + index_bytes,
                    aad + index_bytes,
                    path,
                    key=key,
                )
                trim_start = max(0, start - chunk_start)
                piece = plaintext[trim_start : trim_start + (wanted_end - max(chunk_start, start))]
                if piece:
                    yield piece
            position = chunk_end
            index += 1

    def _decrypt_chunk(
        self, stored: bytes, nonce: bytes, aad: bytes, path: str, *, key: bytes | None = None
    ) -> bytes:
        if key is not None:
            # An explicit key stands alone: falling through to the application's
            # legacy keys would open one collection's file with another secret's
            # history, and a wrong file key must fail instead of wandering.
            try:
                return AESGCM(key).decrypt(nonce, stored, aad)
            except Exception as error:
                raise ValueError(f"Failed to decrypt file '{path}': {error}") from error
        last_error = None
        for candidate in (self._get_encryption_key(), *self._get_legacy_encryption_keys()):
            try:
                return AESGCM(candidate).decrypt(nonce, stored, aad)
            except Exception as error:
                last_error = error
        raise ValueError(f"Failed to decrypt file '{path}': {last_error}")

    def _seekable_associated_data(self, path: str, file_nonce: bytes) -> bytes:
        return SEEKABLE_MAGIC + b"\x00" + file_nonce + path.encode("utf-8")

    def _seekable_v2_associated_data(
        self, path: str, file_nonce: bytes, plaintext_length: int, chunk_count: int
    ) -> bytes:
        return (
            SEEKABLE_MAGIC_V2
            + b"\x00"
            + file_nonce
            + path.encode("utf-8")
            + SEEKABLE_CHUNK_SIZE.to_bytes(4, "big")
            + plaintext_length.to_bytes(8, "big")
            + chunk_count.to_bytes(4, "big")
        )

    def get_file_decrypted(self, path: str, *, key: bytes | None = None) -> bytes:
        """
        Retrieve the encrypted file, decrypt it, and return its raw bytes.
        """
        with self.get_file_stream_decrypted(path, key=key) as f:
            return f.read()

    def get_encrypted_plaintext_size(self, path: str) -> int:
        """Return the envelope's plaintext length without decrypting the whole object."""
        with self.get_file_stream(path) as stream:
            prefix = stream.read(max(len(ENCRYPTED_FILE_MAGIC), len(SEEKABLE_MAGIC_V2)))
        if self._seekable_version(prefix) > 0:
            return self.get_seekable_plaintext_size(path)
        stored_size = self.get_file_size(path)
        overhead = NONCE_SIZE + 16
        if prefix[: len(ENCRYPTED_FILE_MAGIC)] == ENCRYPTED_FILE_MAGIC:
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

    def looks_encrypted(self, path: str) -> bool:
        """Whether the stored object carries one of our encryption envelopes.

        A cheap prefix check. Deciding this by the filename is not enough: the
        same logical path holds a plaintext object before a module starts
        encrypting and an envelope afterwards.
        """
        with self.get_file_stream(path) as stream:
            head = stream.read(max(len(ENCRYPTED_FILE_MAGIC), len(SEEKABLE_MAGIC_V2)))
        return head.startswith(ENCRYPTED_FILE_MAGIC) or self._seekable_version(head) > 0

    def read_maybe_encrypted(self, path: str) -> bytes:
        """The object's bytes, decrypted if it is an envelope.

        Callers that must keep serving objects written before their module started
        encrypting use this: the bytes are identical either way, so the caller does
        not need to know which envelope — or none — the object happens to use.
        """
        if self.looks_encrypted(path):
            return self.get_file_decrypted(path)
        with self.get_file_stream(path) as stream:
            return stream.read()

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

    def local_path(self, path: str) -> Path:
        """Return the sanitized filesystem path for local-only processing."""
        return self._full_path(path)

    def save_file(self, data: bytes, path: str) -> str:
        return self.save_stream(io.BytesIO(data), path)

    def save_stream(self, stream: BinaryIO, path: str) -> str:
        full = self._full_path(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{full.name}.", suffix=".tmp", dir=full.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as output:
                shutil.copyfileobj(stream, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, full)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
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
        return self.save_stream(io.BytesIO(data), path)

    def save_stream(self, stream: BinaryIO, path: str) -> str:
        from boto3.s3.transfer import TransferConfig

        self._client.upload_fileobj(
            stream,
            self._bucket,
            path,
            Config=TransferConfig(multipart_threshold=8 * 1024 * 1024, multipart_chunksize=8 * 1024 * 1024),
        )
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
