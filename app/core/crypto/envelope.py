"""The payload envelope: one data key, one value, bound to its row.

Sealing is AES-GCM under the data key with the row's identity as associated
data, so a blob cannot be pasted into another row and still open. The key
changes; the payload does not — which is the whole reason the wrapper and the
payload are separate envelopes.
"""

import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.crypto.codec import decode_b64, encode_b64

PAYLOAD_PREFIX = "nsp:v1:"


def seal(dek: bytes, payload: bytes, *, context: bytes) -> str:
    """Encrypt a value for storage. `context` must identify the row."""
    nonce = os.urandom(12)
    blob = nonce + AESGCM(dek).encrypt(nonce, payload, context)
    return PAYLOAD_PREFIX + encode_b64(blob)


def unseal(dek: bytes, value: str, *, context: bytes) -> bytes:
    if not value or not value.startswith(PAYLOAD_PREFIX):
        raise ValueError("The stored payload is not sealed")
    raw = decode_b64(value.removeprefix(PAYLOAD_PREFIX))
    try:
        return AESGCM(dek).decrypt(raw[:12], raw[12:], context)
    except InvalidTag as exc:
        raise ValueError("The sealed payload does not belong to this Vault") from exc


def is_sealed(value: str | None) -> bool:
    return bool(value) and value.startswith(PAYLOAD_PREFIX)


def context_for(kind: str, identifier: Any) -> bytes:
    """Bind ciphertext to its row so a blob cannot be pasted somewhere else.

    One envelope holds one row's fields as a single blob, so there is nothing
    to swap within a row — the swap this prevents is across rows. Callers that
    seal two values of one row under one key must include the field name in
    `kind` (e.g. "item:title"), otherwise the two blobs are interchangeable.
    `kind` may not contain ":" precisely because the encoding joins with it.
    """
    if not kind or ":" in kind or "\x00" in kind:
        raise ValueError("A seal context kind must be a non-empty string without ':'")
    return f"netsanctum:vault:{kind}:{identifier}".encode()


def derive_subkey(ikm: bytes, *, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """A purpose-separated key from an existing one, by HKDF-SHA256.

    The `info` is what keeps two derived keys from ever being the same bytes,
    which is the entire reason a module derives a subkey instead of reusing the
    key it already has. Two modules that pick the same `info` collide; that is
    the caller's one responsibility and the reason this takes it as an argument
    rather than composing it here.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)
