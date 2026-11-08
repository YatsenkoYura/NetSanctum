"""NSP2 reader — decoding sealed and unsealed containers.

Responsibilities:
- Parse Header, Footer, Index Block, KeySlots Block.
- For **unsealed** containers: decompress (Zstd) and return raw bytes.
- For **sealed** containers: unwrap DEK from KeySlots via Argon2id,
  derive blob keys via HKDF, decrypt ChaCha20-Poly1305 chunks.
- Range reads for sealed media: O(1) chunk decode without reading the file.

The reader is deliberately modular: callers pass a `passphrase_fn` callback
that resolves the passphrase for a given container_id. This keeps the reader
free of vault-specific logic (inbox_private_key, collection passphrase, etc.).

Lives in core: any module that needs to read an NSP2 package uses this.
"""

import json
import math
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO

import argon2
import zstandard as zstd
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.crypto.codec import decode_b64
from app.core.nsp2 import (
    DEK_BYTES,
    FOOTER_SIZE,
    HEADER_SIZE,
    INDEX_MAX_LEN,
    KEYSLOTS_MAX_LEN,
    NONCE_SIZE,
    SALT_BYTES,
    NSP2Error,
    NSP2SealedError,
    NSP2UnsealedError,
    parse_footer,
    parse_header,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ZSTD_DECOMPRESSOR = zstd.ZstdDecompressor()


# ---------------------------------------------------------------------------
# Validation helpers (anti-DoS before Argon2id)
# ---------------------------------------------------------------------------


def _validate_keyslots(json_str: str, keyslots_len: int) -> dict:
    """Validate KeySlots limits before any expensive KDF calls.

    Returns the parsed JSON dict.
    """
    if keyslots_len > KEYSLOTS_MAX_LEN:
        raise NSP2SealedError(f"KeySlots too large: {keyslots_len}")
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise NSP2SealedError("Invalid KeySlots JSON") from exc

    slots = data.get("slots", [])
    if not isinstance(slots, list) or not (1 <= len(slots) <= 8):
        raise NSP2SealedError("KeySlots: 1..8 slots expected")

    slot = slots[0]  # we only support 1 slot for now

    kdf = slot.get("kdf", {})
    version = int(kdf.get("version", 0))
    m_cost = int(kdf.get("m_cost_kib", 0))
    t_cost = int(kdf.get("t_cost", 0))
    p_cost = int(kdf.get("p_cost", 0))

    if version != 19:
        raise NSP2SealedError(f"Unsupported KDF version: {version}")
    if not (16384 <= m_cost <= 524288):
        raise NSP2SealedError(f"Unsafe m_cost: {m_cost}")
    if not (1 <= t_cost <= 16):
        raise NSP2SealedError(f"Unsafe t_cost: {t_cost}")
    if not (1 <= p_cost <= 8):
        raise NSP2SealedError(f"Unsafe p_cost: {p_cost}")

    salt_b64 = slot.get("salt_b64", "")
    nonce_b64 = slot.get("nonce_b64", "")
    wrapped_b64 = slot.get("wrapped_dek_b64", "")

    # Strip padding for length checks
    salt = decode_b64(salt_b64 + "=" * (4 - len(salt_b64) % 4))
    nonce = decode_b64(nonce_b64 + "=" * (4 - len(nonce_b64) % 4))
    wrapped = decode_b64(wrapped_b64 + "=" * (4 - len(wrapped_b64) % 4))

    if len(salt) != SALT_BYTES:
        raise NSP2SealedError(f"Bad salt length: {len(salt)}")
    if len(nonce) != NONCE_SIZE:
        raise NSP2SealedError(f"Bad nonce length: {len(nonce)}")
    if len(wrapped) != DEK_BYTES + 16:  # DEK + tag
        raise NSP2SealedError(f"Bad wrapped_DEK length: {len(wrapped)}")

    return data


# ---------------------------------------------------------------------------
# Unwrap DEK from KeySlots
# ---------------------------------------------------------------------------


def unwrap_dek_from_keyslots(
    keyslots_bytes: bytes,
    header_bytes: bytes,
    passphrase: str,
) -> bytes:
    """Unwrap the package DEK from the KeySlots block.

    Raises NSP2SealedError on any failure.
    """
    keyslots_json = keyslots_bytes.decode("utf-8")
    data = _validate_keyslots(keyslots_json, len(keyslots_bytes))

    slot = data["slots"][0]
    kdf = slot["kdf"]
    version = int(kdf["version"])
    m_cost = int(kdf["m_cost_kib"])
    t_cost = int(kdf["t_cost"])
    p_cost = int(kdf["p_cost"])

    salt = decode_b64(slot["salt_b64"] + "=" * (4 - len(slot["salt_b64"]) % 4))
    nonce = decode_b64(slot["nonce_b64"] + "=" * (4 - len(slot["nonce_b64"]) % 4))
    wrapped = decode_b64(slot["wrapped_dek_b64"] + "=" * (4 - len(slot["wrapped_dek_b64"]) % 4))

    # Derive KEK
    kek = argon2.low_level.hash_secret_raw(
        passphrase.encode("utf-8"),
        salt,
        time_cost=t_cost,
        memory_cost=m_cost,
        parallelism=p_cost,
        hash_len=DEK_BYTES,
        type=argon2.low_level.Type.ID,
        version=version,
    )

    # Slot AAD (82 bytes)
    slot_aad = (
        header_bytes[:HEADER_SIZE]
        + b"nsp2:slot:argon2id"
        + struct.pack("<IIII", version, m_cost, t_cost, p_cost)
        + salt
    )

    try:
        dek = ChaCha20Poly1305(kek).decrypt(nonce, wrapped, slot_aad)
    except InvalidTag as exc:
        raise NSP2SealedError("Wrong passphrase for this sealed container") from exc

    if len(dek) != DEK_BYTES:
        raise NSP2SealedError("Unwrapped DEK has wrong length")
    return dek


# ---------------------------------------------------------------------------
# Index reading
# ---------------------------------------------------------------------------


def _hkdf_index_key(dek: bytes, container_id: bytes, index_salt: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=DEK_BYTES,
        salt=container_id,
        info=b"nsp2:index:" + index_salt,
    ).derive(dek)


def _make_chunk_nonce(chunk_index: int) -> bytes:
    """Reconstruct nonce deterministically — NOT read from disk."""
    return b"\x00\x00\x00\x00" + struct.pack("<Q", chunk_index)


def _make_chunk_aad(container_id: bytes, blob_id: int, chunk_index: int, is_last: bool) -> bytes:
    """Reconstruct AAD for a chunk — NOT read from disk."""
    return container_id + struct.pack("<QQB", blob_id, chunk_index, int(is_last))


def read_index_block(
    file_obj: BinaryIO,
    footer: dict,
    container_id: bytes,
    dek: bytes | None = None,
    header_bytes: bytes | None = None,
) -> dict:
    """Read and (if sealed) decrypt the Index Block.

    Parameters
    ----------
    header_bytes:
        The full 32-byte Header. Required for sealed containers to
        compute the ChaCha20-Poly1305 AAD (header[:32] + footer[:72]).
    """
    index_offset = footer["index_offset"]
    index_len = footer["index_len"]

    if index_len > INDEX_MAX_LEN:
        raise NSP2Error(f"Index too large: {index_len}")

    file_obj.seek(index_offset)
    raw_index = file_obj.read(index_len)

    is_sealed = bool(footer["flags"] & 0x0001)

    if is_sealed:
        if dek is None:
            raise NSP2UnsealedError("Sealed container requires DEK")
        index_salt = footer["index_hash_or_salt"]
        index_key = HKDF(
            algorithm=hashes.SHA256(),
            length=DEK_BYTES,
            salt=container_id,
            info=b"nsp2:index:" + index_salt,
        ).derive(dek)
        # AAD = header[0:32] + footer[0:72] = 104 bytes
        if header_bytes is None:
            raise NSP2SealedError("Header bytes required for sealed index decryption")
        footer_raw = footer.get("_footer_raw", b"")
        aad = header_bytes[:HEADER_SIZE] + footer_raw[:FOOTER_SIZE]
        try:
            raw_index = ChaCha20Poly1305(index_key).decrypt(b"\x00" * 12, raw_index, aad)
        except InvalidTag as exc:
            raise NSP2SealedError("Index block authentication failed") from exc

    # Decompress (Zstd)
    try:
        index_bytes = _ZSTD_DECOMPRESSOR.decompress(raw_index)
    except Exception as exc:
        raise NSP2Error(f"Index decompression failed: {exc}") from exc

    try:
        return json.loads(index_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NSP2Error(f"Index parsing failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Blob reading
# ---------------------------------------------------------------------------


def _hkdf_blob_key(dek: bytes, container_id: bytes, blob_id: int) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=DEK_BYTES,
        salt=container_id,
        info=b"nsp2:blob:" + struct.pack("<Q", blob_id),
    ).derive(dek)


@dataclass
class _ChunkRange:
    """Byte range within the sealed file covering one plaintext chunk."""

    start: int
    length: int


def _chunk_file_range(
    blob_meta: dict,
    container_id: bytes,
    start: int,
    length: int,
) -> list[tuple[int, int, int]]:
    """Compute sealed file chunks needed for plaintext range [start, start+length).

    Returns list of (chunk_index, plaintext_start_in_chunk, plaintext_len).
    """
    chunk_size = blob_meta.get("chunk_size", 0)
    if chunk_size == 0:
        return [(0, start, length)]

    encoded_len = blob_meta["encoded_len"]
    if encoded_len == 0:
        return []

    num_chunks = math.ceil(encoded_len / chunk_size)

    # Each sealed chunk: nonce(12) + pt_chunk + tag(16)
    # But first chunks are always chunk_size plaintext → stored = 12 + chunk_size + 16 = chunk_size + 28
    # Last chunk: 12 + last_plain + 16
    # Actually for the stored_len calculation, let's use: each chunk on disk = nonce(12) + (pt + tag)
    # pt is at most chunk_size bytes.

    result = []
    wanted_end = start + length
    first_chunk = start // chunk_size
    last_chunk = (wanted_end - 1) // chunk_size if wanted_end <= encoded_len else num_chunks - 1

    for ci in range(first_chunk, last_chunk + 1):
        pt_start_in_blob = ci * chunk_size
        pt_start_in_chunk = max(0, start - pt_start_in_blob)
        pt_avail = min(chunk_size, encoded_len - pt_start_in_blob)
        pt_needed = min(pt_avail - pt_start_in_chunk, wanted_end - max(start, pt_start_in_blob))
        result.append((ci, pt_start_in_chunk, pt_needed))

    return result


def read_blob(
    file_obj: BinaryIO,
    blob_meta: dict,
    container_id: bytes,
    dek: bytes | None = None,
    start: int = 0,
    length: int | None = None,
) -> bytes:
    """Read (and optionally decrypt/decompress) a blob.

    For sealed containers, only the needed chunks are decrypted.
    For unsealed, the whole blob is decompressed.

    Parameters
    ----------
    file_obj: seekable file object
    blob_meta: entry from ``index["blobs"]``
    container_id: from header
    dek: already-unwrapped DEK (None for unsealed)
    start: byte offset within the **raw** (decompressed) blob
    length: bytes to read (None = entire blob)
    """
    is_sealed = dek is not None
    stored_offset = blob_meta["offset"]
    stored_len = blob_meta["stored_len"]
    compression = blob_meta.get("compression", "none")
    chunk_size = blob_meta.get("chunk_size", 0)
    encoded_len = blob_meta["encoded_len"]

    file_obj.seek(stored_offset)
    stored_data = file_obj.read(stored_len)

    if is_sealed:
        # Decrypt chunks on demand
        # For sealed containers, start/length refer to raw (decompressed) coords,
        # but we must decrypt in encoded (compressed) coords first.
        encoded_len = blob_meta["encoded_len"]
        chunk_size = blob_meta.get("chunk_size", 0)

        if encoded_len == 0:
            # Empty blob — return empty raw
            return b""

        # Determine which encoded bytes we need based on raw range [start, end)
        raw_len = blob_meta["raw_len"]
        end = raw_len if length is None else min(start + length, raw_len)
        if start >= end:
            return b""

        # Map raw range to encoded range:
        # For zstd-compressed blobs, a raw range might span multiple compressed chunks.
        # For simplicity, decrypt all chunks that could contain the requested range.
        # The zstd decompressor handles this internally.
        if compression == "zstd" and length is not None:
            # Partial read of compressed blob: decrypt everything, then slice
            num_chunks = math.ceil(encoded_len / chunk_size) if chunk_size else 1
        else:
            # Full read or no compression: decrypt only needed chunks
            num_chunks = math.ceil(encoded_len / chunk_size) if chunk_size else 1

        blob_key = _hkdf_blob_key(dek, container_id, blob_meta["id"])
        result = bytearray()

        for ci in range(num_chunks):
            is_last = (ci + 1) * chunk_size >= encoded_len if chunk_size else True
            if is_last:
                last_plain = encoded_len - ci * chunk_size if chunk_size else encoded_len
            else:
                last_plain = chunk_size
            chunk_stored = last_plain + 16  # NO nonce on disk

            # Find this chunk in stored_data
            pos = 0
            for i in range(ci):
                if (i + 1) * chunk_size >= encoded_len:
                    pos += (encoded_len - i * chunk_size) + 16
                else:
                    pos += chunk_size + 16

            chunk_blob = stored_data[pos : pos + chunk_stored]
            nonce = _make_chunk_nonce(ci)
            ct = chunk_blob
            try:
                pt = ChaCha20Poly1305(blob_key).decrypt(
                    nonce,
                    ct,
                    _make_chunk_aad(container_id, blob_meta["id"], ci, is_last),
                )
            except InvalidTag as exc:
                raise NSP2SealedError("Chunk authentication failed") from exc
            result += pt

        # Decompress if needed (sealed blobs may be zstd-compressed)
        if compression == "zstd":
            try:
                decompressed = _ZSTD_DECOMPRESSOR.decompress(bytes(result))
            except Exception:
                # If decompression fails, return raw decrypted data
                decompressed = bytes(result)
            # Slice to requested range
            end = raw_len if length is None else min(start + length, raw_len)
            return decompressed[start:end]

        # No compression: slice to requested range
        end = raw_len if length is None else min(start + length, raw_len)
        return bytes(result[: end - start])

    else:
        # Unsealed: decompress
        if compression == "zstd":
            raw_data = _ZSTD_DECOMPRESSOR.decompress(stored_data)
        else:
            raw_data = stored_data

        end = len(raw_data) if length is None else min(start + length, len(raw_data))
        if start >= end:
            return b""
        return raw_data[start:end]


# ---------------------------------------------------------------------------
# High-level reader
# ---------------------------------------------------------------------------


@dataclass
class NSP2Container:
    """Parsed NSP2 container metadata and index."""

    format_version: int
    flags: int
    container_id: bytes
    is_sealed: bool
    is_padded: bool
    index: dict
    blob_metas: dict[int, dict]  # blob_id → blob_meta dict


def open_nsp2(
    file_obj: BinaryIO,
    passphrase: str | None = None,
    passphrase_fn: Callable[[bytes], str] | None = None,
) -> NSP2Container:
    """Open and parse an NSP2 container.

    Parameters
    ----------
    file_obj:
        Seekable binary file object pointing to the ``.nsp`` file.
    passphrase:
        Direct passphrase string (shorthand).
    passphrase_fn:
        Callback ``container_id → passphrase``. Overrides *passphrase*.

    Returns
    -------
    NSP2Container with parsed index and metadata.

    To read blob data, use :func:`read_blob` with ``container.blob_metas[bid]``.
    """
    # Read header
    header = file_obj.read(HEADER_SIZE)
    fmt_ver, flags, container_id = parse_header(header)

    if fmt_ver != 2:
        raise NSP2Error(f"Unsupported format version: {fmt_ver}")

    is_sealed = bool(flags & 0x0001)
    is_padded = bool(flags & 0x0002)

    # Read footer
    file_obj.seek(0, 2)  # EOF
    total_size = file_obj.tell()

    # Read full file for footer parsing (parse_footer needs full content)
    file_obj.seek(0)
    full_file = file_obj.read()
    footer = parse_footer(full_file, total_size)

    # Read DEK
    dek: bytes | None = None
    if is_sealed:
        actual_passphrase = None
        if passphrase_fn:
            actual_passphrase = passphrase_fn(container_id)
        elif passphrase:
            actual_passphrase = passphrase
        else:
            raise NSP2SealedError("Sealed container requires a passphrase")

        # Read KeySlots
        ks_offset = footer["keyslots_offset"]
        ks_len = footer["keyslots_len"]
        if ks_offset == 0 or ks_len == 0:
            raise NSP2Error("Sealed container has no KeySlots block")
        file_obj.seek(ks_offset)
        keyslots_bytes = file_obj.read(ks_len)
        dek = unwrap_dek_from_keyslots(keyslots_bytes, header, actual_passphrase)

    # Read Index — pass header and footer raw bytes for sealed AAD
    footer_raw_bytes = full_file[total_size - FOOTER_SIZE : total_size]
    footer["_footer_raw"] = footer_raw_bytes  # needed for AAD in read_index_block
    index = read_index_block(
        file_obj,
        footer,
        container_id,
        dek,
        header_bytes=header,
    )

    # Build blob lookup
    blob_metas = {b["id"]: b for b in index.get("blobs", [])}

    return NSP2Container(
        format_version=footer["format_version"],
        flags=flags,
        container_id=container_id,
        is_sealed=is_sealed,
        is_padded=is_padded,
        index=index,
        blob_metas=blob_metas,
    )


def resolve_url(container: NSP2Container, url: str) -> tuple[dict, bytes | None]:
    """Resolve a URL to its blob metadata and optional DEK.

    Returns ``(blob_meta, dek)``.
    """
    entries = container.index.get("entries", [])
    entry = next((e for e in entries if e["url"] == url), None)
    if entry is None:
        raise NSP2Error(f"URL not found in container: {url}")

    blob_id = entry["blob_id"]
    blob_meta = container.blob_metas.get(blob_id)
    if blob_meta is None:
        raise NSP2Error(f"Blob {blob_id} not found for URL {url}")

    dek = (container.container_id and b"") or None  # placeholder
    return blob_meta, dek if not container.is_sealed else None
