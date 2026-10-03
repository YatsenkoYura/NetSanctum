"""Sealed Vault collections.

An encrypted collection keeps a random data-encryption key (DEK) that is never
stored in the clear. The DEK is wrapped by a key-encryption key derived from the
owner's passphrase with scrypt, and only the wrapper is persisted. The unwrapped
DEK lives in Redis for as long as the vault is unlocked, so the passphrase is
never written down anywhere.

The split exists so that changing the passphrase re-wraps one small blob instead
of every image and every video byte in the collection.
"""

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

WRAPPED_PREFIX = "nsk:v1:"
PAYLOAD_PREFIX = "nsp:v1:"
KDF_NAME = "scrypt"
# scrypt cost. n=2**15 with r=8 costs roughly 100ms on a small server, which is
# the point: it makes an offline guessing attack expensive without making an
# unlock feel broken.
KDF_N = 2**15
KDF_R = 8
KDF_P = 1
SALT_BYTES = 16
DEK_BYTES = 32
# Bound into the wrapper by default. A caller that knows which collection the key
# belongs to should pass that id instead, so a wrapper cannot be copied across.
DEK_CONTEXT = b"netsanctum:vault:dek:v1"


class VaultUnlockError(ValueError):
    """The passphrase did not unwrap this collection's key."""


@dataclass(frozen=True, slots=True)
class WrappedKey:
    salt: str
    wrapped: str
    kdf: str = KDF_NAME
    n: int = KDF_N
    r: int = KDF_R
    p: int = KDF_P


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def derive_kek(passphrase: str, salt: bytes, *, n: int = KDF_N, r: int = KDF_R, p: int = KDF_P) -> bytes:
    """Stretch the passphrase into a key-encryption key."""
    if not passphrase:
        raise VaultUnlockError("A passphrase is required")
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=DEK_BYTES,
        maxmem=132 * n * r * 2,
    )


def new_data_key() -> bytes:
    return os.urandom(DEK_BYTES)


def wrap_data_key(
    dek: bytes,
    passphrase: str,
    *,
    context: bytes = DEK_CONTEXT,
    n: int = KDF_N,
    r: int = KDF_R,
    p: int = KDF_P,
) -> WrappedKey:
    """Wrap a data key under a passphrase and return only what is safe to persist."""
    if len(dek) != DEK_BYTES:
        raise ValueError("A data key must be 32 bytes")
    salt = os.urandom(SALT_BYTES)
    kek = derive_kek(passphrase, salt, n=n, r=r, p=p)
    nonce = os.urandom(12)
    blob = nonce + AESGCM(kek).encrypt(nonce, dek, context)
    return WrappedKey(salt=_b64(salt), wrapped=WRAPPED_PREFIX + _b64(blob), kdf=KDF_NAME, n=n, r=r, p=p)


def unwrap_data_key(wrapped: WrappedKey, passphrase: str, *, context: bytes = DEK_CONTEXT) -> bytes:
    """Recover the DEK. Raises VaultUnlockError for any wrong passphrase."""
    if wrapped.kdf != KDF_NAME:
        raise VaultUnlockError(f"Unsupported key derivation {wrapped.kdf!r}")
    if not wrapped.wrapped.startswith(WRAPPED_PREFIX):
        raise VaultUnlockError("The stored key wrapper is malformed")
    kek = derive_kek(passphrase, _unb64(wrapped.salt), n=wrapped.n, r=wrapped.r, p=wrapped.p)
    raw = _unb64(wrapped.wrapped.removeprefix(WRAPPED_PREFIX))
    if len(raw) <= 12:
        raise VaultUnlockError("The stored key wrapper is malformed")
    try:
        dek = AESGCM(kek).decrypt(raw[:12], raw[12:], context)
    except InvalidTag as exc:
        raise VaultUnlockError("Wrong passphrase for this Vault") from exc
    if len(dek) != DEK_BYTES:
        raise VaultUnlockError("The unwrapped key has the wrong length")
    return dek


def seal(dek: bytes, payload: bytes, *, context: bytes) -> str:
    """Encrypt a value for storage. `context` must identify the row."""
    nonce = os.urandom(12)
    blob = nonce + AESGCM(dek).encrypt(nonce, payload, context)
    return PAYLOAD_PREFIX + _b64(blob)


def unseal(dek: bytes, value: str, *, context: bytes) -> bytes:
    if not value or not value.startswith(PAYLOAD_PREFIX):
        raise ValueError("The stored payload is not sealed")
    raw = _unb64(value.removeprefix(PAYLOAD_PREFIX))
    try:
        return AESGCM(dek).decrypt(raw[:12], raw[12:], context)
    except InvalidTag as exc:
        raise ValueError("The sealed payload does not belong to this Vault") from exc


def is_sealed(value: str | None) -> bool:
    return bool(value) and value.startswith(PAYLOAD_PREFIX)


def token_for(value: str, length: int = 24) -> str:
    return _b64(os.urandom(length))[:length]


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(str(left), str(right))


def context_for(kind: str, identifier: Any) -> bytes:
    """Bind ciphertext to its row so a blob cannot be pasted somewhere else."""
    return f"netsanctum:vault:{kind}:{identifier}".encode()


# ── Blind write inbox ──────────────────────────────────────────────────────
# Writing into a sealed vault must not require the passphrase: the browser
# extension has no way to be given it, and a capture that arrives in the clear
# would defeat the vault entirely. So each sealed collection also carries an
# X25519 keypair. The public half sits in the clear and is enough to seal a new
# item; the private half is sealed under the passphrase and is what a later
# unlock needs in order to open anything.

INBOX_PREFIX = "nsi:v1:"
INBOX_KDF_CONTEXT = b"netsanctum:vault:inbox-key:v1"


@dataclass(frozen=True, slots=True)
class SealedWrite:
    """One blind write: the ciphertext plus the key that opens it."""

    payload: str
    wrapped_key: str


def generate_inbox_keypair() -> tuple[bytes, bytes]:
    """Return (private, public) raw 32-byte X25519 keys."""
    private = X25519PrivateKey.generate()
    return (
        private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )


def seal_inbox_private_key(
    private_key: bytes, passphrase: str, *, context: bytes = INBOX_KDF_CONTEXT
) -> WrappedKey:
    """Seal a collection's inbox private key under its passphrase."""
    return wrap_data_key(private_key, passphrase, context=context)


def open_inbox_private_key(
    wrapped: WrappedKey, passphrase: str, *, context: bytes = INBOX_KDF_CONTEXT
) -> bytes:
    return unwrap_data_key(wrapped, passphrase, context=context)


def seal_for_inbox(payload: bytes, public_key: bytes, *, context: bytes) -> SealedWrite:
    """Encrypt a payload so that only the holder of `private_key` can read it.

    An ephemeral X25519 keypair is generated per write and thrown away: only the
    shared secret survives, inside the wrapped key. Nothing in the database can
    reproduce it.
    """
    if len(public_key) != 32:
        raise ValueError("An inbox public key must be 32 bytes")
    ephemeral_private, ephemeral_public = generate_inbox_keypair()
    shared = X25519PrivateKey.from_private_bytes(ephemeral_private).exchange(
        X25519PublicKey.from_public_bytes(public_key)
    )
    # The shared secret is used as a key, never as the key itself.
    kek = hashlib.sha256(INBOX_PREFIX.encode("ascii") + shared).digest()
    item_key = new_data_key()
    sealed = seal(kek, item_key, context=context)
    return SealedWrite(
        payload=seal(item_key, payload, context=context),
        wrapped_key=INBOX_PREFIX + _b64(ephemeral_public + sealed.encode("utf-8")),
    )


def open_from_inbox(write: SealedWrite, private_key: bytes, *, context: bytes) -> bytes:
    """Recover a blind write's payload."""
    if len(private_key) != 32:
        raise ValueError("An inbox private key must be 32 bytes")
    if not write.wrapped_key.startswith(INBOX_PREFIX):
        raise ValueError("The stored item key is malformed")
    raw = _unb64(write.wrapped_key.removeprefix(INBOX_PREFIX))
    if len(raw) <= 32:
        raise ValueError("The stored item key is malformed")
    shared = X25519PrivateKey.from_private_bytes(private_key).exchange(
        X25519PublicKey.from_public_bytes(raw[:32])
    )
    kek = hashlib.sha256(INBOX_PREFIX.encode("ascii") + shared).digest()
    item_key = unseal(kek, raw[32:].decode("utf-8"), context=context)
    return unseal(item_key, write.payload, context=context)
