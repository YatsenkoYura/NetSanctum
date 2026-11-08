"""NSP v2 (NSP2) — единый самодостаточный контейнер пакета модулей.

Потоковая архитектура: сборка ведёт **на диск** чанками по 64 KiB через
tempfile + fsync + os.replace — ни один собранный контейнер не держится
целиком в RAM. Чтение поддерживает `seek()` и HTTP Range-запросы.

Формат поддерживает два режима в одном файле:
- **unsealed** — открытые данные, сжатие текстовых блобов Zstd.
- **sealed** — Argon2id → HKDF-SHA256 → Chunked ChaCha20-Poly1305.

Clean break: обратная совместимость со старым NSPK (v1) **не нужна**.
Криптография Vault (envelope.py, asymmetric.py, DEK_BYTES,
inbox_private_key) и drive_sync **не трогаются**.

Lives in core: формат — общий для всех модулей. Продуктовая логика
(какие ресурсы пакетировать, какая парольная фраза) остаётся с модулем;
NSP2 знает только raw-байты и URL.

## Crypto contract (Python ↔ Rust byte-for-byte)

All integers are **Little-Endian** (`<` in `struct.pack`).

### slot_aad — строго 82 байта
``Header[0:32] + b"nsp2:slot:argon2id" + struct.pack("<IIII", version, m_cost, t_cost, p_cost) + salt(16)``
= 32 + 18 + 16 + 16 = **82 bytes** (НЕ JSON-байты!).

### index_aad — строго 104 байта
``Header[0:32] + Footer[0:72]`` (включая footer CRC32 и b"NSP!").

### chunk_aad — строго 33 байта
``container_id(16) + struct.pack("<QQB", blob_id, chunk_index, is_last)``
= 16 + 8 + 8 + 1 = **33 bytes**.

### nonce — строго 12 байт
``b"\\x00\\x00\\x00\\x00" + struct.pack("<Q", chunk_index)``

### Пустой блоб (raw_len == 0)
Ровно 1 чанок: ``ChaCha20(key, nonce, b"", aad)`` → **16 bytes on disk**
(один тег Poly1305 для пустого plaintext, ``chunk_index=0, is_last=1``).
Nonce (12 байт) **НЕ** записывается на диск — вычисляется детерминированно.
Читается обратно как пустой bytes.

### rekey()
Смена passphrase без перешифрования Data Region:
1. Прочитать KeySlots, unwrap DEK старым паролем.
2. Сгенерировать новый KeySlot с новым паролем (новый salt + nonce).
3. Записать Header + Data Region (без изменений) → новый Index (зашифрован).
4. Записать новый KeySlot → Footer (последние 72 байта файла).
"""

import hashlib
import json
import math
import os
import struct
import tempfile
import zlib
from dataclasses import dataclass
from typing import BinaryIO

import argon2
import zstandard as zstd
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.crypto.codec import decode_b64, encode_b64

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAGIC_WRITE: bytes = b"NSP2"
MAGIC_FOOTER: bytes = b"NSP!"
HEADER_SIZE: int = 32
FOOTER_SIZE: int = 72
FOOTER_FORMAT: str = "<QQQIHH32sI4s"

FORMAT_VERSION: int = 2

FLAG_SEALED: int = 0x0001
FLAG_PADDED: int = 0x0002

CONTAINER_ID_BYTES: int = 16
SALT_BYTES: int = 16
INDEX_SALT_BYTES: int = 32
NONCE_SIZE: int = 12
DEK_BYTES: int = 32
INDEX_NONCE: bytes = b"\x00" * 12
CHUNK_SIZE_SEALED: int = 65536  # 64 KiB
PADDED_ALIGNMENT: int = 262144  # 256 KiB

INDEX_MAX_LEN: int = 64 * 1024 * 1024  # 64 MiB
KEYSLOTS_MAX_LEN: int = 65536  # 64 KiB

COMPRESSION_ZSTD: str = "zstd"
COMPRESSION_NONE: str = "none"

ZSTD_PREFIXES: tuple[str, ...] = (
    "text/",
    "application/json",
    "application/javascript",
    "image/svg+xml",
)

# Anti-DoS limits for KeySlots parameters (checked BEFORE Argon2id call)
M_COST_MIN: int = 16384
M_COST_MAX: int = 524288
T_COST_MIN: int = 1
T_COST_MAX: int = 16
P_COST_MAX: int = 8

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class NSP2Error(RuntimeError):
    """Base error for NSP2 operations."""


class NSP2UnsealedError(NSP2Error):
    """Requested sealed content from an unsealed container."""


class NSP2SealedError(NSP2Error):
    """Wrong passphrase or key for a sealed container."""


# ---------------------------------------------------------------------------
# Compression helpers
# ---------------------------------------------------------------------------

_ZSTD_COMPRESSOR = zstd.ZstdCompressor()
_ZSTD_DECOMPRESSOR = zstd.ZstdDecompressor()


def _should_compress(mime: str) -> bool:
    """Should *mime* be compressed with Zstd?"""
    mime_lower = mime.split(";")[0].strip().lower()
    return any(mime_lower.startswith(p) for p in ZSTD_PREFIXES)


def _compress(data: bytes, kind: str) -> bytes:
    if kind == COMPRESSION_ZSTD:
        return _ZSTD_COMPRESSOR.compress(data)
    return data


# ---------------------------------------------------------------------------
# HKDF helpers
# ---------------------------------------------------------------------------


def _hkdf_blob_key(dek: bytes, container_id: bytes, blob_id: int) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=DEK_BYTES,
        salt=container_id,
        info=b"nsp2:blob:" + struct.pack("<Q", blob_id),
    ).derive(dek)


def _hkdf_index_key(dek: bytes, container_id: bytes, index_salt: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=DEK_BYTES,
        salt=container_id,
        info=b"nsp2:index:" + index_salt,
    ).derive(dek)


# ---------------------------------------------------------------------------
# Header / Footer
# ---------------------------------------------------------------------------


def build_header(flags: int, container_id: bytes | None = None) -> bytes:
    """Build a 32-byte NSP2 header."""
    if container_id is None:
        container_id = os.urandom(CONTAINER_ID_BYTES)
    return struct.pack(
        "<4sHH16s8s",
        MAGIC_WRITE,
        FORMAT_VERSION,
        flags,
        container_id,
        b"\x00" * 8,
    )


def parse_header(raw: bytes) -> tuple[int, int, bytes]:
    """Return ``(format_version, flags, container_id)``."""
    if len(raw) < HEADER_SIZE:
        raise NSP2Error("Header too short")
    magic, fmt_ver, flags, container_id, _reserved = struct.unpack("<4sHH16s8s", raw[:HEADER_SIZE])
    if magic != MAGIC_WRITE:
        raise NSP2Error(f"Bad magic: {magic!r}")
    return fmt_ver, flags, container_id


def build_footer(
    index_offset: int,
    index_len: int,
    keyslots_offset: int,
    keyslots_len: int,
    flags: int,
    index_hash_or_salt: bytes,
) -> bytes:
    """Build a 72-byte NSP2 footer with CRC32."""
    crc = (
        zlib.crc32(
            struct.pack(
                "<QQQIHH32s",
                index_offset,
                index_len,
                keyslots_offset,
                keyslots_len,
                flags,
                FORMAT_VERSION,
                index_hash_or_salt,
            )
        )
        & 0xFFFFFFFF
    )
    return struct.pack(
        FOOTER_FORMAT,
        index_offset,
        index_len,
        keyslots_offset,
        keyslots_len,
        flags,
        FORMAT_VERSION,
        index_hash_or_salt,
        crc,
        MAGIC_FOOTER,
    )


def parse_footer(footer_bytes: bytes) -> dict:
    """Parse exactly 72 bytes of footer.

    *footer_bytes* must be exactly FOOTER_SIZE bytes.
    """
    if len(footer_bytes) != FOOTER_SIZE:
        raise NSP2Error(f"Footer must be {FOOTER_SIZE} bytes, got {len(footer_bytes)}")
    if footer_bytes[-4:] != MAGIC_FOOTER:
        raise NSP2Error(f"Bad footer magic: {footer_bytes[-4:]!r}")
    (
        index_offset,
        index_len,
        keyslots_offset,
        keyslots_len,
        flags,
        fmt_ver,
        index_hash_or_salt,
        crc_stored,
        _magic,
    ) = struct.unpack(FOOTER_FORMAT, footer_bytes)
    crc_check = zlib.crc32(footer_bytes[:64]) & 0xFFFFFFFF
    if crc_check != crc_stored:
        raise NSP2Error(f"CRC mismatch: {crc_check:#010x} != {crc_stored:#010x}")
    return {
        "format_version": fmt_ver,
        "flags": flags,
        "index_offset": index_offset,
        "index_len": index_len,
        "keyslots_offset": keyslots_offset,
        "keyslots_len": keyslots_len,
        "index_hash_or_salt": index_hash_or_salt,
    }


# ---------------------------------------------------------------------------
# Sealed blob chunks — AAD is exactly 33 bytes
# ---------------------------------------------------------------------------


def _seal_chunk(
    key: bytes,
    chunk_index: int,
    plaintext: bytes,
    container_id: bytes,
    blob_id: int,
    is_last: bool,
) -> bytes:
    """Seal a single chunk → ciphertext(plain+16) ONLY.

    Returns ct_only: ``plain_len + 16`` bytes (ChaCha20-Poly1305 IETF ciphertext+tag).
    For an empty plaintext this is exactly 16 bytes.
    """
    nonce = b"\x00\x00\x00\x00" + struct.pack("<Q", chunk_index)
    aad = container_id + struct.pack("<QQB", blob_id, chunk_index, int(is_last))
    assert len(nonce) == 12, f"nonce len {len(nonce)} != 12"
    assert len(aad) == 33, f"AAD len {len(aad)} != 33"
    ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    return ct


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@dataclass
class _BlobMeta:
    id: int
    offset: int
    stored_len: int
    encoded_len: int
    raw_len: int
    compression: str
    chunk_size: int
    sha256: str


@dataclass
class _EntryMeta:
    url: str
    blob_id: int
    mime: str
    type: str
    layer: str


# ---------------------------------------------------------------------------
# Index JSON builder
# ---------------------------------------------------------------------------


def _build_index_json(
    package_info: dict,
    blob_metas: list[_BlobMeta],
    entries: list[_EntryMeta],
) -> dict:
    blobs_array = [
        {
            "id": bm.id,
            "offset": bm.offset,
            "stored_len": bm.stored_len,
            "encoded_len": bm.encoded_len,
            "raw_len": bm.raw_len,
            "compression": bm.compression,
            "chunk_size": bm.chunk_size,
            "sha256": bm.sha256,
        }
        for bm in blob_metas
    ]
    entries_array = [
        {
            "url": e.url,
            "blob_id": e.blob_id,
            "mime": e.mime,
            "type": e.type,
            "layer": e.layer,
        }
        for e in entries
    ]

    # Runtime hash — lines joined with \n (see spec §1.4.5)
    runtime_entries = sorted(
        [e for e in entries if e.layer == "runtime"],
        key=lambda e: e.url,
    )
    runtime_parts = []
    sha_map = {bm.id: bm.sha256 for bm in blob_metas}
    for e in runtime_entries:
        runtime_parts.append(f"{e.url},{sha_map[e.blob_id]}")
    runtime_sha = hashlib.sha256("\n".join(runtime_parts).encode("utf-8")).hexdigest()

    result = {**package_info, "blobs": blobs_array, "entries": entries_array}
    rt = result.get("runtime", {})
    if not isinstance(rt, dict):
        rt = {}
    rt["embedded"] = True
    rt["sha256"] = runtime_sha
    result["runtime"] = rt
    return result


# ---------------------------------------------------------------------------
# KeySlots helpers
# ---------------------------------------------------------------------------


def _build_keyslots_json(
    dek: bytes,
    header_bytes: bytes,
    passphrase: str,
    *,
    m_cost_kib: int = 65536,
    t_cost: int = 3,
    p_cost: int = 1,
    version: int = 19,
) -> tuple[bytes, dict]:
    """Build the KeySlots JSON and return (slot_aad_bytes, slot_data)."""
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_SIZE)

    kek = argon2.low_level.hash_secret_raw(
        passphrase.encode("utf-8"),
        salt,
        time_cost=t_cost,
        memory_cost=m_cost_kib,
        parallelism=p_cost,
        hash_len=DEK_BYTES,
        type=argon2.low_level.Type.ID,
        version=version,
    )

    # Slot AAD — strictly 82 bytes
    slot_aad = (
        header_bytes[:HEADER_SIZE]
        + b"nsp2:slot:argon2id"
        + struct.pack("<IIII", version, m_cost_kib, t_cost, p_cost)
        + salt
    )
    assert len(slot_aad) == 82, f"slot_aad len {len(slot_aad)} != 82"

    ct_and_tag = ChaCha20Poly1305(kek).encrypt(nonce, dek, slot_aad)
    assert len(ct_and_tag) == 48, f"ct+tag len {len(ct_and_tag)} != 48"

    slots_json = json.dumps(
        {
            "version": 1,
            "slots": [
                {
                    "slot_id": 0,
                    "type": "argon2id",
                    "kdf": {
                        "version": version,
                        "m_cost_kib": m_cost_kib,
                        "t_cost": t_cost,
                        "p_cost": p_cost,
                    },
                    "salt_b64": encode_b64(salt).rstrip("="),
                    "nonce_b64": encode_b64(nonce).rstrip("="),
                    "wrapped_dek_b64": encode_b64(ct_and_tag).rstrip("="),
                }
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")

    return slot_aad, slots_json


# ---------------------------------------------------------------------------
# Streaming writer — NspWriter
# ---------------------------------------------------------------------------

_CHUNK_WRITE = 65536  # 64 KiB chunks for disk writes


class NspWriter:
    """Streaming NSP2 writer — writes directly to a file-like object on disk.

    Parameters
    ----------
    file_obj : BinaryIO
        A seekable file object (typically a tempfile) open for writing.
    sealed : bool
        If ``True`` use sealed mode (Argon2id + ChaCha20-Poly1305).
    passphrase : str, optional
        Required when ``sealed=True``.
    padded : bool
        If ``True``, pad Data Region to 256 KiB alignment.
    m_cost_kib, t_cost, p_cost : int
        Argon2id parameters (sealed only).
    container_id : bytes | None
        Fixed 16-byte container ID (for rekey). Auto-generated if ``None``.
    """

    def __init__(
        self,
        file_obj: BinaryIO,
        sealed: bool = False,
        passphrase: str | None = None,
        padded: bool = False,
        m_cost_kib: int = 65536,
        t_cost: int = 3,
        p_cost: int = 1,
        container_id: bytes | None = None,
    ) -> None:
        self._f = file_obj
        self._sealed = sealed
        self._passphrase = passphrase
        self._padded = padded
        self._m_cost_kib = m_cost_kib
        self._t_cost = t_cost
        self._p_cost = p_cost

        if sealed and not passphrase:
            raise ValueError("passphrase is required for sealed mode")

        self._container_id = container_id or os.urandom(CONTAINER_ID_BYTES)
        self._blob_counter: int = 0
        self._blob_metas: list[_BlobMeta] = []
        self._entries: list[_EntryMeta] = []
        self._sha_to_id: dict[str, int] = {}
        self._current_offset: int = HEADER_SIZE

        flags = FLAG_SEALED if sealed else 0
        if padded:
            flags |= FLAG_PADDED
        self._flags = flags

        self._header = build_header(flags, self._container_id)
        self._f.write(self._header)

        # For sealed: DEK + index key derivation state
        self._dek: bytes | None = None
        if sealed:
            self._dek = os.urandom(DEK_BYTES)

        # For sealed text dedup: raw_sha → compressed bytes (keep in RAM for text only)
        self._text_dedup: dict[str, bytes] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_blob(
        self,
        url: str,
        content: bytes,
        mime: str = "application/octet-stream",
        rtype: str = "text",
    ) -> None:
        """Add a resource blob to the container.

        For ``compression == "none"`` (binary media) the content is hashed
        and written directly without storing in ``raw_by_sha`` — only the
        hash key is kept.
        For ``compression == "zstd"`` (text) deduplication works via
        ``_text_dedup`` dictionary.
        """
        compression = COMPRESSION_ZSTD if _should_compress(mime) else COMPRESSION_NONE
        raw_sha = hashlib.sha256(content).hexdigest()
        layer = "runtime" if url.startswith("/static/") else "content"

        if compression == COMPRESSION_NONE:
            # Binary / no-compression: no RAM storage, just dedup via sha
            if raw_sha in self._sha_to_id:
                self._entries.append(_EntryMeta(url, self._sha_to_id[raw_sha], mime, rtype, layer))
                return

            blob_id = self._blob_counter
            self._blob_counter += 1
            self._sha_to_id[raw_sha] = blob_id

            encoded = content  # no compression
            self._f.write(encoded)
            offset = self._current_offset
            self._current_offset += len(encoded)

            self._blob_metas.append(
                _BlobMeta(
                    id=blob_id,
                    offset=offset,
                    stored_len=len(encoded),
                    encoded_len=len(encoded),
                    raw_len=len(content),
                    compression=COMPRESSION_NONE,
                    chunk_size=CHUNK_SIZE_SEALED if self._sealed else 0,
                    sha256=raw_sha,
                )
            )
            self._entries.append(_EntryMeta(url, blob_id, mime, rtype, layer))

        else:
            # Text / zstd-compression: dedup via _text_dedup
            if raw_sha in self._sha_to_id:
                self._entries.append(_EntryMeta(url, self._sha_to_id[raw_sha], mime, rtype, layer))
                return

            blob_id = self._blob_counter
            self._blob_counter += 1
            self._sha_to_id[raw_sha] = blob_id

            # Compress (and dedup)
            if raw_sha in self._text_dedup:
                encoded = self._text_dedup[raw_sha]
            else:
                encoded = _compress(content, COMPRESSION_ZSTD)
                self._text_dedup[raw_sha] = encoded

            if self._sealed:
                # Sealed + zstd: seal chunks
                num_chunks = math.ceil(len(encoded) / CHUNK_SIZE_SEALED) if encoded else 0
                sealed_size = 0
                if num_chunks == 0:
                    # Empty blob
                    chunk_data = _seal_chunk(
                        self._blob_key(blob_id), 0, b"", self._container_id, blob_id, True
                    )
                    self._f.write(chunk_data)
                    sealed_size = len(chunk_data)
                else:
                    for ci in range(num_chunks):
                        is_last = ci == num_chunks - 1
                        start = ci * CHUNK_SIZE_SEALED
                        pt = encoded[start : start + CHUNK_SIZE_SEALED]
                        chunk_data = _seal_chunk(
                            self._blob_key(blob_id), ci, pt, self._container_id, blob_id, is_last
                        )
                        self._f.write(chunk_data)
                        sealed_size += len(chunk_data)
                offset = self._current_offset
                self._current_offset += sealed_size
            else:
                self._f.write(encoded)
                offset = self._current_offset
                self._current_offset += len(encoded)

            self._blob_metas.append(
                _BlobMeta(
                    id=blob_id,
                    offset=offset,
                    stored_len=self._current_offset - offset,
                    encoded_len=len(encoded),
                    raw_len=len(content),
                    compression=COMPRESSION_ZSTD,
                    chunk_size=CHUNK_SIZE_SEALED if self._sealed else 0,
                    sha256=raw_sha,
                )
            )
            self._entries.append(_EntryMeta(url, blob_id, mime, rtype, layer))

    def blob_key(self, blob_id: int) -> bytes:
        """Derive the ChaCha20 key for a sealed blob."""
        return _hkdf_blob_key(self._dek, self._container_id, blob_id)

    def finish(self, package_info: dict) -> int:
        """Finalize the NSP2 container and return total file size.

        Writes: padding (if padded) → Index Block → KeySlots (if sealed) → Footer.
        Footer is always the last 72 bytes.
        """
        # Index
        index_json = _build_index_json(package_info, self._blob_metas, self._entries)
        index_bytes = json.dumps(index_json, separators=(",", ":")).encode("utf-8")
        index_compressed = _ZSTD_COMPRESSOR.compress(index_bytes)

        # Padding before index
        data_region_end = self._current_offset
        if self._padded:
            target = math.ceil(data_region_end / PADDED_ALIGNMENT) * PADDED_ALIGNMENT
            if target > data_region_end:
                self._f.write(os.urandom(target - data_region_end))
                data_region_end = target

        index_offset = data_region_end
        index_len = len(index_compressed)

        # Sealed: encrypt index
        if self._sealed:
            index_salt = os.urandom(INDEX_SALT_BYTES)
            index_key = _hkdf_index_key(self._dek, self._container_id, index_salt)

            # KeySlots — must be built before sealing index (size needed for footer)
            _, keyslots_bytes = _build_keyslots_json(
                self._dek,
                self._header,
                self._passphrase,  # type: ignore[arg-type]
                m_cost_kib=self._m_cost_kib,
                t_cost=self._t_cost,
                p_cost=self._p_cost,
            )
            keyslots_len = len(keyslots_bytes)
            index_enc_len = len(index_compressed) + 16  # AEAD: +16 tag
            keyslots_offset = index_offset + index_enc_len

            # Footer (before sealing, so we know keyslots_offset)
            footer = build_footer(
                index_offset=index_offset,
                index_len=index_enc_len,
                keyslots_offset=keyslots_offset,
                keyslots_len=keyslots_len,
                flags=self._flags,
                index_hash_or_salt=index_salt,
            )
            aad = self._header[:HEADER_SIZE] + footer[:FOOTER_SIZE]
            assert len(aad) == 104
            index_encrypted = ChaCha20Poly1305(index_key).encrypt(INDEX_NONCE, index_compressed, aad)

            self._f.write(index_encrypted)
            self._f.write(keyslots_bytes)
            self._f.write(footer)
            total = keyslots_offset + keyslots_len + FOOTER_SIZE

        else:
            # Unsealed: plain index, no keyslots
            footer = build_footer(
                index_offset=index_offset,
                index_len=index_len,
                keyslots_offset=0,
                keyslots_len=0,
                flags=self._flags,
                index_hash_or_salt=hashlib.sha256(index_compressed).digest(),
            )
            self._f.write(index_compressed)
            self._f.write(footer)
            total = index_offset + index_len + FOOTER_SIZE

        self._f.flush()
        return total

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _blob_key(self, blob_id: int) -> bytes:
        """Derive blob key (sealed only)."""
        if self._dek is None:
            raise NSP2Error("Not a sealed container")
        return _hkdf_blob_key(self._dek, self._container_id, blob_id)


# ---------------------------------------------------------------------------
# Public API — convenience wrappers
# ---------------------------------------------------------------------------


def create_package(
    resources: list[dict],
    package_info: dict,
    sealed: bool = False,
    passphrase: str | None = None,
    padded: bool = False,
    m_cost_kib: int = 65536,
    t_cost: int = 3,
    p_cost: int = 1,
) -> bytes:
    """High-level entry point: build an NSP2 container in memory.

    For streaming (production), use :class:`NspWriter` or
    ``create_package_streaming()``.
    """
    if sealed and not passphrase:
        raise ValueError("passphrase is required for sealed mode")

    # Default to padded=True for sealed for maximum privacy
    if sealed and not padded:
        padded = True

    with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as tf:
        writer = NspWriter(
            tf,
            sealed=sealed,
            passphrase=passphrase,
            padded=padded,
            m_cost_kib=m_cost_kib,
            t_cost=t_cost,
            p_cost=p_cost,
        )
        for res in resources:
            writer.add_blob(
                url=res["url"],
                content=res["content"],
                mime=res.get("mime", "application/octet-stream"),
                rtype=res.get("type", "text"),
            )
        writer.finish(package_info)
        tf.seek(0)
        return tf.read()


def create_package_streaming(
    resources: list[dict],
    package_info: dict,
    sealed: bool = False,
    passphrase: str | None = None,
    padded: bool = False,
    m_cost_kib: int = 65536,
    t_cost: int = 3,
    p_cost: int = 1,
) -> BinaryIO:
    """Build NSP2 and stream it to a real tempfile on disk.

    Returns a ``SpooledTemporaryFile`` open in ``rb`` mode.
    Caller must ``.close()`` when done.
    """
    if sealed and not passphrase:
        raise ValueError("passphrase is required for sealed mode")
    if sealed and not padded:
        padded = True

    tf = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    writer = NspWriter(
        tf,
        sealed=sealed,
        passphrase=passphrase,
        padded=padded,
        m_cost_kib=m_cost_kib,
        t_cost=t_cost,
        p_cost=p_cost,
    )
    for res in resources:
        writer.add_blob(
            url=res["url"],
            content=res["content"],
            mime=res.get("mime", "application/octet-stream"),
            rtype=res.get("type", "text"),
        )
    writer.finish(package_info)
    tf.seek(0)
    return tf


# ---------------------------------------------------------------------------
# rekey_nsp2 — re-encrypt Index Block with new passphrase
# ---------------------------------------------------------------------------


def rekey_nsp2(
    source: BinaryIO,
    destination: BinaryIO,
    old_passphrase: str,
    new_passphrase: str,
    *,
    m_cost_kib: int | None = None,
    t_cost: int | None = None,
    p_cost: int | None = None,
) -> None:
    """Re-encrypt an NSP2 sealed container with a new passphrase.

    Does NOT re-encrypt the Data Region — only Index Block and KeySlots.
    Footer is always the last 72 bytes.

    Parameters
    ----------
    source / destination:
        Seekable binary file objects.
    m_cost_kib / t_cost / p_cost:
        New Argon2id parameters. If ``None``, reuse old slot values.
    """
    _chunk_size = 65536  # 64 KiB read chunks for streaming copy

    # ── 1. Read header ──────────────────────────────────────────────
    header = source.read(HEADER_SIZE)
    if len(header) < HEADER_SIZE:
        raise NSP2Error("Source too short for header")
    fmt_ver, flags, container_id = parse_header(header)
    if fmt_ver != FORMAT_VERSION:
        raise NSP2Error(f"Unsupported format version: {fmt_ver}")
    if not (flags & FLAG_SEALED):
        raise NSP2Error("Can only rekey sealed containers")

    # ── 2. Read footer (last 72 bytes) ──────────────────────────────
    source.seek(0, 2)  # EOF
    total_size = source.tell()
    if total_size < FOOTER_SIZE:
        raise NSP2Error("File too short")
    source.seek(total_size - FOOTER_SIZE)
    old_footer_raw = source.read(FOOTER_SIZE)
    old_footer = parse_footer(old_footer_raw)

    # ── 3. Read KeySlots + unwrap DEK (inlined to avoid circular import) ─
    ks_offset = old_footer["keyslots_offset"]
    ks_len = old_footer["keyslots_len"]
    if ks_offset == 0 or ks_len == 0:
        raise NSP2Error("Sealed container has no KeySlots block")
    source.seek(ks_offset)
    keyslots_bytes = source.read(ks_len)
    keyslots_json = keyslots_bytes.decode("utf-8")
    data = json.loads(keyslots_json)
    slots = data.get("slots", [])
    slot = slots[0]
    kdf = slot["kdf"]
    version = int(kdf["version"])
    m_cost = int(kdf["m_cost_kib"])
    t_cost = int(kdf["t_cost"])
    p_cost = int(kdf["p_cost"])
    salt = decode_b64(slot["salt_b64"] + "=" * (-len(slot["salt_b64"]) % 4))
    nonce = decode_b64(slot["nonce_b64"] + "=" * (-len(slot["nonce_b64"]) % 4))
    wrapped = decode_b64(slot["wrapped_dek_b64"] + "=" * (-len(slot["wrapped_dek_b64"]) % 4))
    kek = argon2.low_level.hash_secret_raw(
        old_passphrase.encode("utf-8"), salt, time_cost=t_cost,
        memory_cost=m_cost, parallelism=p_cost, hash_len=DEK_BYTES,
        type=argon2.low_level.Type.ID, version=version,
    )
    slot_aad = (
        header[:HEADER_SIZE] + b"nsp2:slot:argon2id"
        + struct.pack("<IIII", version, m_cost, t_cost, p_cost) + salt
    )
    try:
        dek = ChaCha20Poly1305(kek).decrypt(nonce, wrapped, slot_aad)
    except Exception as exc:
        raise NSP2SealedError("Wrong passphrase for this sealed container") from exc

    # ── 4. Read & decrypt old Index Block ───────────────────────────
    idx_offset = old_footer["index_offset"]
    idx_len = old_footer["index_len"]
    source.seek(idx_offset)
    raw_index_encrypted = source.read(idx_len)

    index_salt = old_footer["index_hash_or_salt"]
    index_key = _hkdf_index_key(dek, container_id, index_salt)
    footer_aad = header[:HEADER_SIZE] + old_footer_raw[:FOOTER_SIZE]
    try:
        raw_index = ChaCha20Poly1305(index_key).decrypt(INDEX_NONCE, raw_index_encrypted, footer_aad)
    except Exception as exc:
        raise NSP2SealedError("Index block decryption failed during rekey") from exc

    zstd_index = raw_index  # already decompressed? No — index is zstd-compressed

    # ── 5. Stream-copy Header + Data Region (unchanged) ─────────────
    source.seek(0)
    remaining = idx_offset  # copy everything before index
    while remaining > 0:
        cs = min(_chunk_size, remaining)
        chunk = source.read(cs)
        if not chunk:
            break
        destination.write(chunk)
        remaining -= len(chunk)

    new_index_offset = destination.tell()

    # ── 6. New KeySlots ─────────────────────────────────────────────
    new_m = m_cost_kib or 65536
    new_t = t_cost or 3
    new_p = p_cost or 1
    _, new_keyslots_bytes = _build_keyslots_json(
        dek,
        header,
        new_passphrase,
        m_cost_kib=new_m,
        t_cost=new_t,
        p_cost=new_p,
    )

    # ── 7. Fresh index salt ─────────────────────────────────────────
    new_index_salt = os.urandom(INDEX_SALT_BYTES)
    new_index_len = len(zstd_index) + 16  # AEAD tag
    new_keyslots_offset = new_index_offset + new_index_len
    new_keyslots_len = len(new_keyslots_bytes)

    # ── 8. New footer ───────────────────────────────────────────────
    new_footer = build_footer(
        index_offset=new_index_offset,
        index_len=new_index_len,
        keyslots_offset=new_keyslots_offset,
        keyslots_len=new_keyslots_len,
        flags=flags,
        index_hash_or_salt=new_index_salt,
    )

    # ── 9. Seal index with new key ──────────────────────────────────
    new_index_key = _hkdf_index_key(dek, container_id, new_index_salt)
    new_footer_aad = header[:HEADER_SIZE] + new_footer[:FOOTER_SIZE]
    new_index_encrypted = ChaCha20Poly1305(new_index_key).encrypt(INDEX_NONCE, zstd_index, new_footer_aad)

    # ── 10. Write: index_enc + keyslots + footer (exactly 72B) ──────
    destination.write(new_index_encrypted)
    destination.write(new_keyslots_bytes)
    destination.write(new_footer)

    destination.flush()


def rekey_nsp2_file(
    path: str,
    old_passphrase: str,
    new_passphrase: str,
    *,
    m_cost_kib: int | None = None,
    t_cost: int | None = None,
    p_cost: int | None = None,
) -> None:
    """Rekey an NSP2 file in-place (via tmp + os.replace)."""
    tmp_path = path + ".tmp"
    with open(path, "rb") as src, open(tmp_path, "wb") as dst:
        rekey_nsp2(
            src, dst, old_passphrase, new_passphrase, m_cost_kib=m_cost_kib, t_cost=t_cost, p_cost=p_cost
        )
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Inspect (sealed: metadata only, no passphrase)
# ---------------------------------------------------------------------------


def inspect_nsp2(file_obj: BinaryIO) -> dict:
    """Inspect an NSP2 file without reading it all into RAM.

    For sealed: returns ``container_id``, ``flags``, ``file_size``, ``kdf_params``.
    For unsealed: returns those plus ``blob_count``, ``entry_count`` (reads index).

    Returns a plain dict (never raises on bad data — returns ``{"error": ...}``).
    """
    try:
        file_obj.seek(0)
        header = file_obj.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            return {"error": "file too short"}
        fmt_ver, flags, container_id = parse_header(header)

        file_obj.seek(0, 2)
        total_size = file_obj.tell()
        if total_size < FOOTER_SIZE:
            return {"error": "file too short for footer"}
        file_obj.seek(total_size - FOOTER_SIZE)
        footer_raw = file_obj.read(FOOTER_SIZE)
        footer = parse_footer(footer_raw)

        result: dict = {
            "format_version": fmt_ver,
            "container_id": container_id.hex(),
            "flags": flags,
            "file_size": total_size,
        }

        if flags & FLAG_SEALED:
            ks_offset = footer["keyslots_offset"]
            ks_len = footer["keyslots_len"]
            if ks_offset > 0 and ks_len > 0:
                file_obj.seek(ks_offset)
                ks_bytes = file_obj.read(min(ks_len, 4096))  # just version/slots
                try:
                    ks_json = json.loads(ks_bytes)
                    slots = ks_json.get("slots", [])
                    if slots:
                        kdf = slots[0].get("kdf", {})
                        result["kdf_params"] = {
                            "m_cost_kib": kdf.get("m_cost_kib"),
                            "t_cost": kdf.get("t_cost"),
                            "p_cost": kdf.get("p_cost"),
                        }
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            result["is_sealed"] = True
        else:
            # Unsealed: read index to get blob/entry counts
            idx_off = footer["index_offset"]
            idx_len = footer["index_len"]
            if idx_off > 0 and idx_len < INDEX_MAX_LEN:
                file_obj.seek(idx_off)
                raw_idx = file_obj.read(idx_len)
                try:
                    decompressed = _ZSTD_DECOMPRESSOR.decompress(raw_idx)
                    idx_json = json.loads(decompressed.decode("utf-8"))
                    result["blob_count"] = len(idx_json.get("blobs", []))
                    result["entry_count"] = len(idx_json.get("entries", []))
                except Exception:
                    pass
            result["is_sealed"] = False

        return result

    except Exception as exc:
        return {"error": str(exc)}
