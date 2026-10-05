"""Sealed offline packages: the transfer envelopes for encrypted module content.

A sealed package leaves the node as ciphertext and is stored on the client as
ciphertext. The client's disk never holds the deal the server holds with the
owner: the package DEK is wrapped under a passphrase-derived KEK, every
resource has its own HKDF subkey, and media travels in a chunked envelope so a
Range request decrypts only the chunks it covers — in RAM, never via a temp file.

The primitives are the intersection of what the node and the desktop client both
implement: ChaCha20-Poly1305 with 12-byte nonces (NOT XChaCha — the node's
`cryptography` has no XChaCha, and two ciphers is two places to be wrong),
HKDF-SHA256, and Argon2id. The KDF cost is fixed at m=64 MiB, t=3, p=1:
parallelism 1 is mandatory, because the desktop validator admits p up to 4 and
anything above that would not open there.

Version 1, and the first version is the whole format: there is no legacy reader
because nothing sealed has ever been shipped. If the envelope changes, it gets
a new version and a new magic, and v1 keeps opening.

Lives in core because the mechanism is not a vault's: any module shipping
sealed bytes offline needs exactly this, and a second sealed envelope is a
second thing to get wrong. Policy — which passphrase, which resources, who may
download — stays with the module that asks.
"""

import os
import unicodedata
from collections.abc import Iterator
from typing import BinaryIO

import argon2
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.crypto.codec import decode_b64, encode_b64
from app.core.crypto.kdf import UnlockError, canonical_params

TRANSFER_VERSION = 1

# magic(8) | file nonce(8) | chunk size u32be | plaintext length u64be | chunk count u32be.
SEALED_MAGIC = b"NSSEAL\x01\x00\x00"
SEALED_MAGIC_SIZE = len(SEALED_MAGIC)
TRANSFER_NONCE_SIZE = 12
TRANSFER_FILE_NONCE_SIZE = 8
TRANSFER_CHUNK_SIZE = 1024 * 1024
TRANSFER_HEADER_SIZE = SEALED_MAGIC_SIZE + TRANSFER_FILE_NONCE_SIZE + 4 + 8 + 4
TRANSFER_DEK_BYTES = 32
TRANSFER_SALT_BYTES = 16

# The transfer KDF cost. Fixed, not tuned per package: the parameters travel in
# the manifest so a raise would not lock anyone out, but both ends must accept
# whatever is written — and the desktop admits p up to 4, so p=1 is the only
# safe choice. m=64 MiB matches both vaults' weight class.
TRANSFER_M_COST = 64 * 1024
TRANSFER_T_COST = 3
TRANSFER_PARALLELISM = 1

# Reader bounds, checked before any allocation. The written cost sits far below
# each ceiling; anything above is a forged manifest, refused without deriving.
TRANSFER_M_COST_MIN = 32 * 1024
TRANSFER_M_COST_MAX = 256 * 1024
TRANSFER_T_COST_MAX = 10
TRANSFER_PARALLELISM_MAX = 4

# Header bounds for the chunked envelope. The writer always uses 1 MiB; the
# reader accepts a range so a future writer may choose differently without a
# version bump — but the count must agree with the sizes, exactly.
TRANSFER_CHUNK_SIZE_MIN = 1024
TRANSFER_CHUNK_SIZE_MAX = 64 * 1024 * 1024

WRAP_AAD_PREFIX = b"ns:sealed-pkg:wrap:v1"
RES_AAD_PREFIX = b"ns:sealed-pkg:res:v1"
RESOURCE_HKDF_SALT_PREFIX = b"ns:sealed-pkg:res:v1"


def transfer_kdf_params() -> dict[str, int]:
    """The one KDF cost sealed packages are written with."""
    return {
        "m_cost": TRANSFER_M_COST,
        "t_cost": TRANSFER_T_COST,
        "parallelism": TRANSFER_PARALLELISM,
    }


def check_transfer_cost(params: dict[str, int]) -> None:
    """Refuse absurd KDF costs from a manifest before deriving anything."""
    try:
        m = int(params["m_cost"])
        t = int(params["t_cost"])
        p = int(params["parallelism"])
    except (KeyError, TypeError, ValueError) as error:
        raise UnlockError("The sealed package names an unreadable KDF cost") from error
    if not (TRANSFER_M_COST_MIN <= m <= TRANSFER_M_COST_MAX):
        raise UnlockError("The sealed package names an unsafe KDF cost")
    if not (1 <= t <= TRANSFER_T_COST_MAX) or not (1 <= p <= TRANSFER_PARALLELISM_MAX):
        raise UnlockError("The sealed package names an unsafe KDF cost")


def _passphrase_bytes(passphrase: str) -> bytes:
    """One spelling of a passphrase. NFC, like the vault unlock."""
    return unicodedata.normalize("NFC", passphrase).encode("utf-8")


def derive_transfer_kek(passphrase: str, salt: bytes, **cost: int) -> bytes:
    """Stretch the package passphrase into a key-encryption key.

    The passphrase is the whole secret — salt and cost are public manifest
    fields, exactly like a vault wrapper. There is no raw-bytes fallback:
    sealed packages are normalized from the first version ever shipped.
    """
    if not passphrase:
        raise UnlockError("A passphrase is required")
    if len(salt) != TRANSFER_SALT_BYTES:
        raise UnlockError("The sealed package has a malformed KDF salt")
    params = {
        "m_cost": cost.get("m_cost", TRANSFER_M_COST),
        "t_cost": cost.get("t_cost", TRANSFER_T_COST),
        "parallelism": cost.get("parallelism", TRANSFER_PARALLELISM),
    }
    check_transfer_cost(params)
    return argon2.low_level.hash_secret_raw(
        _passphrase_bytes(passphrase),
        salt,
        time_cost=params["t_cost"],
        memory_cost=params["m_cost"],
        parallelism=params["parallelism"],
        hash_len=TRANSFER_DEK_BYTES,
        type=argon2.low_level.Type.ID,
    )


def transfer_wrap_aad(package_id: str, params: dict[str, int], salt: bytes) -> bytes:
    """What the wrapped package DEK is bound to: package, cost, salt."""
    return WRAP_AAD_PREFIX + b"|" + package_id.encode("utf-8") + b"|" + canonical_params(params) + b"|" + salt


def new_transfer_dek() -> bytes:
    return os.urandom(TRANSFER_DEK_BYTES)


def wrap_package_dek(
    dek: bytes,
    passphrase: str,
    package_id: str,
    *,
    salt: bytes | None = None,
    nonce: bytes | None = None,
) -> dict[str, object]:
    """Wrap a package DEK under its passphrase. Returns the manifest fragment.

    `salt`/`nonce` are injectable so the golden vectors are deterministic;
    production passes nothing and gets fresh randomness. The fragment carries
    everything a client needs except the passphrase: salt, KDF cost and the
    nonce-prefixed ciphertext, all base64url.
    """
    if len(dek) != TRANSFER_DEK_BYTES:
        raise ValueError("A package key must be 32 bytes")
    if not package_id:
        raise ValueError("A sealed package needs an id to bind the wrapper to")
    params = transfer_kdf_params()
    salt = salt if salt is not None else os.urandom(TRANSFER_SALT_BYTES)
    nonce = nonce if nonce is not None else os.urandom(TRANSFER_NONCE_SIZE)
    if len(salt) != TRANSFER_SALT_BYTES or len(nonce) != TRANSFER_NONCE_SIZE:
        raise ValueError("Bad salt or nonce length for a sealed package wrapper")
    kek = derive_transfer_kek(passphrase, salt, **params)
    aad = transfer_wrap_aad(package_id, params, salt)
    blob = nonce + ChaCha20Poly1305(kek).encrypt(nonce, dek, aad)
    return {
        "version": TRANSFER_VERSION,
        "kdf": {"algorithm": "argon2id", **params},
        "salt": encode_b64(salt),
        "wrapped_dek": encode_b64(blob),
    }


def unwrap_package_dek(sealing: dict, passphrase: str, package_id: str) -> bytes:
    """Recover the package DEK from a manifest `sealing` object.

    Anything wrong — unknown version, absurd cost, tampered bytes, wrong
    passphrase — is UnlockError, with no distinction: the caller must not learn
    which of those it was, and neither must anyone watching.
    """
    try:
        version = int(sealing["version"])
        kdf = dict(sealing["kdf"])
        salt = decode_b64(sealing["salt"])
        raw = decode_b64(sealing["wrapped_dek"])
    except (KeyError, TypeError, ValueError) as error:
        raise UnlockError("The sealed package descriptor is malformed") from error
    if version != TRANSFER_VERSION or kdf.get("algorithm") != "argon2id":
        raise UnlockError("Unsupported sealed package version")
    # Exactly nonce(12) + DEK(32) + tag(16): anything else is truncation.
    if len(raw) != TRANSFER_NONCE_SIZE + TRANSFER_DEK_BYTES + 16:
        raise UnlockError("The sealed package descriptor is malformed")
    params = {
        "m_cost": int(kdf.get("m_cost", 0)),
        "t_cost": int(kdf.get("t_cost", 0)),
        "parallelism": int(kdf.get("parallelism", 0)),
    }
    kek = derive_transfer_kek(passphrase, salt, **params)
    aad = transfer_wrap_aad(package_id, params, salt)
    try:
        dek = ChaCha20Poly1305(kek).decrypt(raw[:TRANSFER_NONCE_SIZE], raw[TRANSFER_NONCE_SIZE:], aad)
    except InvalidTag as exc:
        raise UnlockError("Wrong passphrase for this sealed package") from exc
    if len(dek) != TRANSFER_DEK_BYTES:
        raise UnlockError("The unwrapped package key has the wrong length")
    return dek


def transfer_resource_key(dek: bytes, package_id: str, url: str) -> bytes:
    """The key for one resource, derived — never stored, never transmitted."""
    if len(dek) != TRANSFER_DEK_BYTES:
        raise ValueError("A package key must be 32 bytes")
    if not package_id or not url:
        raise ValueError("A resource key needs a package and a URL to bind to")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=TRANSFER_DEK_BYTES,
        salt=RESOURCE_HKDF_SALT_PREFIX + package_id.encode("utf-8"),
        info=url.encode("utf-8"),
    ).derive(dek)


def _small_aad(url: str, plaintext_len: int) -> bytes:
    return RES_AAD_PREFIX + b"|" + url.encode("utf-8") + b"|" + int(plaintext_len).to_bytes(8, "big")


def seal_small_resource(key: bytes, url: str, plaintext: bytes, *, nonce: bytes | None = None) -> bytes:
    """Seal a small resource whole: nonce(12) || ciphertext+tag."""
    if len(key) != TRANSFER_DEK_BYTES:
        raise ValueError("A resource key must be 32 bytes")
    nonce = nonce if nonce is not None else os.urandom(TRANSFER_NONCE_SIZE)
    if len(nonce) != TRANSFER_NONCE_SIZE:
        raise ValueError("Bad nonce length for a sealed resource")
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, plaintext, _small_aad(url, len(plaintext)))


def open_small_resource(key: bytes, url: str, blob: bytes) -> bytes:
    """Open a small sealed resource, or refuse. The length is authenticated."""
    if len(key) != TRANSFER_DEK_BYTES:
        raise ValueError("A resource key must be 32 bytes")
    if len(blob) < TRANSFER_NONCE_SIZE + 16:
        raise UnlockError("The sealed resource is truncated")
    nonce, ciphertext = blob[:TRANSFER_NONCE_SIZE], blob[TRANSFER_NONCE_SIZE:]
    plaintext_len = len(blob) - TRANSFER_NONCE_SIZE - 16
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, _small_aad(url, plaintext_len))
    except InvalidTag as exc:
        raise UnlockError("The sealed resource does not belong to this package") from exc


def _chunk_aad(url: str, file_nonce: bytes, chunk_size: int, plaintext_len: int, chunk_count: int) -> bytes:
    return (
        SEALED_MAGIC
        + b"\x00"
        + file_nonce
        + url.encode("utf-8")
        + int(chunk_size).to_bytes(4, "big")
        + int(plaintext_len).to_bytes(8, "big")
        + int(chunk_count).to_bytes(4, "big")
    )


def seal_chunked_resource(
    key: bytes,
    url: str,
    plaintext: bytes,
    *,
    file_nonce: bytes | None = None,
    chunk_size: int = TRANSFER_CHUNK_SIZE,
) -> bytes:
    """Seal bytes into the chunked envelope: header || chunks, each its own AEAD."""
    if len(key) != TRANSFER_DEK_BYTES:
        raise ValueError("A resource key must be 32 bytes")
    if not (TRANSFER_CHUNK_SIZE_MIN <= chunk_size <= TRANSFER_CHUNK_SIZE_MAX):
        raise ValueError("Bad chunk size for a sealed resource")
    file_nonce = file_nonce if file_nonce is not None else os.urandom(TRANSFER_FILE_NONCE_SIZE)
    if len(file_nonce) != TRANSFER_FILE_NONCE_SIZE:
        raise ValueError("Bad file nonce for a sealed resource")
    plaintext_len = len(plaintext)
    chunk_count = (plaintext_len + chunk_size - 1) // chunk_size if plaintext_len else 0
    aad = _chunk_aad(url, file_nonce, chunk_size, plaintext_len, chunk_count)
    out = bytearray()
    out += SEALED_MAGIC + file_nonce + chunk_size.to_bytes(4, "big")
    out += plaintext_len.to_bytes(8, "big") + chunk_count.to_bytes(4, "big")
    aead = ChaCha20Poly1305(key)
    for index in range(chunk_count):
        piece = plaintext[index * chunk_size : (index + 1) * chunk_size]
        index_bytes = index.to_bytes(4, "big")
        out += aead.encrypt(file_nonce + index_bytes, piece, aad + index_bytes)
    return bytes(out)


def iter_sealed_chunks(
    key: bytes,
    url: str,
    source: Iterator[bytes],
    plaintext_length: int,
    *,
    file_nonce: bytes | None = None,
    chunk_size: int = TRANSFER_CHUNK_SIZE,
) -> Iterator[bytes]:
    """Yield a chunked envelope header-first, sealing plaintext in transit.

    The streaming core: the gateway and the media endpoint serve ranges from
    these bytes without ever holding the whole file, and the producer writes
    them straight to the sink. `plaintext_length` must be exact — the chunk
    count is part of every chunk's AAD, so a short source voids the envelope
    rather than truncating it.
    """
    if len(key) != TRANSFER_DEK_BYTES:
        raise ValueError("A resource key must be 32 bytes")
    if not (TRANSFER_CHUNK_SIZE_MIN <= chunk_size <= TRANSFER_CHUNK_SIZE_MAX):
        raise ValueError("Bad chunk size for a sealed resource")
    if plaintext_length < 0:
        raise ValueError("Bad plaintext length for a sealed resource")
    file_nonce = file_nonce if file_nonce is not None else os.urandom(TRANSFER_FILE_NONCE_SIZE)
    if len(file_nonce) != TRANSFER_FILE_NONCE_SIZE:
        raise ValueError("Bad file nonce for a sealed resource")
    chunk_count = (plaintext_length + chunk_size - 1) // chunk_size if plaintext_length else 0
    aad = _chunk_aad(url, file_nonce, chunk_size, plaintext_length, chunk_count)
    yield (
        SEALED_MAGIC
        + file_nonce
        + chunk_size.to_bytes(4, "big")
        + plaintext_length.to_bytes(8, "big")
        + chunk_count.to_bytes(4, "big")
    )
    aead = ChaCha20Poly1305(key)
    read = 0
    buffer = b""
    index = 0
    for piece in source:
        buffer += piece
        while len(buffer) >= chunk_size and index < chunk_count:
            index_bytes = index.to_bytes(4, "big")
            yield aead.encrypt(file_nonce + index_bytes, buffer[:chunk_size], aad + index_bytes)
            buffer = buffer[chunk_size:]
            read += chunk_size
            index += 1
    if buffer or index < chunk_count:
        # The tail: whatever is left must be exactly the final chunk.
        if index != chunk_count - 1 or not buffer:
            raise UnlockError("The sealed stream disagrees with its promised length")
        index_bytes = index.to_bytes(4, "big")
        yield aead.encrypt(file_nonce + index_bytes, buffer, aad + index_bytes)
        read += len(buffer)
        index += 1
    if read != plaintext_length or index != chunk_count:
        raise UnlockError("The sealed stream disagrees with its promised length")


def seal_chunked_stream(
    key: bytes,
    url: str,
    stream: BinaryIO,
    plaintext_length: int,
    sink: BinaryIO,
    *,
    file_nonce: bytes | None = None,
    chunk_size: int = TRANSFER_CHUNK_SIZE,
) -> None:
    """Seal a stream into the chunked envelope without holding it whole.

    What the producer uses for gigabyte media: the plaintext is read chunk by
    chunk and the ciphertext goes straight to the sink. Thin wrapper over
    `iter_sealed_chunks`, which is what the streaming endpoints serve from.
    """

    def _pieces() -> Iterator[bytes]:
        while True:
            piece = stream.read(chunk_size)
            if not piece:
                return
            yield piece

    for sealed in iter_sealed_chunks(
        key, url, _pieces(), plaintext_length, file_nonce=file_nonce, chunk_size=chunk_size
    ):
        sink.write(sealed)


def parse_chunked_header(blob: bytes) -> tuple[bytes, int, int, int]:
    """The (file nonce, chunk size, plaintext length, chunk count) of an envelope.

    Untrusted input: sizes are range-checked and the count must agree with the
    sizes, but authenticity itself is enforced per chunk at open time, not here.
    """
    if len(blob) < TRANSFER_HEADER_SIZE or not blob.startswith(SEALED_MAGIC):
        raise UnlockError("Not a sealed chunked resource")
    base = SEALED_MAGIC_SIZE + TRANSFER_FILE_NONCE_SIZE
    file_nonce = blob[SEALED_MAGIC_SIZE:base]
    chunk_size = int.from_bytes(blob[base : base + 4], "big")
    plaintext_len = int.from_bytes(blob[base + 4 : base + 12], "big")
    chunk_count = int.from_bytes(blob[base + 12 : base + 16], "big")
    if not (TRANSFER_CHUNK_SIZE_MIN <= chunk_size <= TRANSFER_CHUNK_SIZE_MAX):
        raise UnlockError("The sealed resource names a bad chunk size")
    expected = (plaintext_len + chunk_size - 1) // chunk_size if plaintext_len else 0
    if chunk_count != expected:
        raise UnlockError("The sealed resource header disagrees with itself")
    return file_nonce, chunk_size, plaintext_len, chunk_count


def _open_chunk(key: bytes, url: str, file_nonce: bytes, aad: bytes, index: int, stored: bytes) -> bytes:
    index_bytes = index.to_bytes(4, "big")
    try:
        return ChaCha20Poly1305(key).decrypt(file_nonce + index_bytes, stored, aad + index_bytes)
    except InvalidTag as exc:
        raise UnlockError("The sealed resource does not belong to this package") from exc


def open_chunked_range(key: bytes, url: str, blob: bytes, start: int, length: int) -> Iterator[bytes]:
    """Yield plaintext `[start, start+length)` from a chunked envelope in RAM.

    Only the covered chunks are opened. A Range into a gigabyte video costs one
    or two chunks, and nothing else is touched.
    """
    if length <= 0:
        return
    if len(key) != TRANSFER_DEK_BYTES:
        raise ValueError("A resource key must be 32 bytes")
    file_nonce, chunk_size, plaintext_len, _count = parse_chunked_header(blob)
    aad = _chunk_aad(url, file_nonce, chunk_size, plaintext_len, _count)
    if start < 0 or (plaintext_len and start >= plaintext_len):
        raise UnlockError("The sealed range starts past the end")
    wanted_end = min(start + length, plaintext_len)
    offset = TRANSFER_HEADER_SIZE
    index = start // chunk_size
    position = index * chunk_size
    # Walk chunk starts arithmetically: every chunk but the last is exactly
    # chunk_size plaintext bytes, hence chunk_size+16 stored bytes.
    while position < wanted_end:
        stored_at = offset + index * (chunk_size + 16)
        is_last = (index + 1) * chunk_size >= plaintext_len
        if is_last:
            last_len = plaintext_len - index * chunk_size
            stored = blob[stored_at : stored_at + last_len + 16]
            if len(stored) != last_len + 16:
                raise UnlockError("The sealed resource is truncated")
        else:
            stored = blob[stored_at : stored_at + chunk_size + 16]
            if len(stored) != chunk_size + 16:
                raise UnlockError("The sealed resource is truncated")
        plaintext = _open_chunk(key, url, file_nonce, aad, index, stored)
        trim = max(0, start - position)
        piece = plaintext[trim : trim + (wanted_end - max(position, start))]
        if piece:
            yield piece
        position += len(plaintext)
        index += 1


def read_sealed_range(key: bytes, url: str, stream: BinaryIO, start: int, length: int) -> Iterator[bytes]:
    """Yield plaintext `[start, start+length)` from a seekable ciphertext stream.

    The gateway path: the ciphertext lives on disk (or in CAS) and only the
    covered chunks are read and opened. Plaintext exists solely in the yielded
    bytes — no temp file, no second copy.
    """
    if length <= 0:
        return
    if len(key) != TRANSFER_DEK_BYTES:
        raise ValueError("A resource key must be 32 bytes")
    stream.seek(0)
    header = stream.read(TRANSFER_HEADER_SIZE)
    file_nonce, chunk_size, plaintext_len, count = parse_chunked_header(header)
    aad = _chunk_aad(url, file_nonce, chunk_size, plaintext_len, count)
    if start < 0 or (plaintext_len and start >= plaintext_len):
        raise UnlockError("The sealed range starts past the end")
    wanted_end = min(start + length, plaintext_len)
    index = start // chunk_size
    position = index * chunk_size
    while position < wanted_end:
        is_last = (index + 1) * chunk_size >= plaintext_len
        if is_last:
            chunk_plain = plaintext_len - index * chunk_size
        else:
            chunk_plain = chunk_size
        stream.seek(TRANSFER_HEADER_SIZE + index * (chunk_size + 16))
        stored = stream.read(chunk_plain + 16)
        if len(stored) != chunk_plain + 16:
            raise UnlockError("The sealed resource is truncated")
        plaintext = _open_chunk(key, url, file_nonce, aad, index, stored)
        trim = max(0, start - position)
        piece = plaintext[trim : trim + (wanted_end - max(position, start))]
        if piece:
            yield piece
        position += chunk_plain
        index += 1


__all__ = [
    "RES_AAD_PREFIX",
    "SEALED_MAGIC",
    "TRANSFER_CHUNK_SIZE",
    "TRANSFER_CHUNK_SIZE_MAX",
    "TRANSFER_CHUNK_SIZE_MIN",
    "TRANSFER_DEK_BYTES",
    "TRANSFER_FILE_NONCE_SIZE",
    "TRANSFER_HEADER_SIZE",
    "TRANSFER_M_COST",
    "TRANSFER_NONCE_SIZE",
    "TRANSFER_PARALLELISM",
    "TRANSFER_SALT_BYTES",
    "TRANSFER_T_COST",
    "TRANSFER_VERSION",
    "WRAP_AAD_PREFIX",
    "check_transfer_cost",
    "derive_transfer_kek",
    "iter_sealed_chunks",
    "new_transfer_dek",
    "open_chunked_range",
    "open_small_resource",
    "parse_chunked_header",
    "read_sealed_range",
    "seal_chunked_resource",
    "seal_chunked_stream",
    "seal_small_resource",
    "transfer_kdf_params",
    "transfer_resource_key",
    "transfer_wrap_aad",
    "unwrap_package_dek",
    "wrap_package_dek",
]
