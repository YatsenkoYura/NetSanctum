"""Passphrase to key-encryption key, and the wrapper that stores a data key.

This is where an owner's passphrase becomes the only secret a sealed collection
has: a random data key is wrapped by a KEK derived from the passphrase with
Argon2id, and only the wrapper is persisted. Changing the passphrase re-wraps one
small blob instead of every byte of the collection.

Argon2id runs at RFC 9106's second recommended option — 64 MiB, three passes,
four lanes. An unlock happens once per tab and then lives in Redis for the session,
so the cost is paid rarely; an offline guessing attack pays it per attempt, forever.
There is exactly one KDF. Wrappers naming anything else are refused, not migrated:
the old scrypt standard was deleted with its data rather than carried forward.

No pepper, deliberately. The `vault_collections` row carries the wrapper, its
salt and its cost and nothing else, so a stolen database is an offline guessing
problem rather than an insider problem — the passphrase is the whole secret.

Lives in core because the mechanism is not a vault's: any module that has to
protect a key with something a human typed needs exactly this, and a second
implementation of a passphrase KDF is a second thing to get wrong. Nothing here
decides what makes a passphrase acceptable — that is a product judgement and
stays with the module that asks.
"""

import json
import os
from dataclasses import dataclass, replace

import argon2
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.crypto.codec import decode_b64, encode_b64


class UnlockError(ValueError):
    """The passphrase did not unwrap this key."""


WRAPPED_PREFIX = "nsk:v1:"
KDF_NAME = "argon2id"


# Argon2id cost: RFC 9106's second recommended option (64 MiB, three passes, four
# lanes). An unlock happens once per tab and then lives in Redis for the session,
# so the few hundred milliseconds it costs are paid rarely, while an offline
# guessing attack pays them per attempt, forever.
ARGON2_M_COST = 64 * 1024
ARGON2_T_COST = 3
ARGON2_PARALLELISM = 4

# Upper bounds for cost parameters read back from storage. The wrapper carries
# its own cost, so a row with m_cost in the gigabytes would otherwise turn the
# next unlock into an OOM before the authentication tag is even checked. Values
# above these are refused without deriving. Generous on purpose: the current
# cost (64 MiB, t=3, p=4) is far below each ceiling, and raising the real cost
# later means raising the ceiling alongside it.
ARGON2_M_COST_MAX = 512 * 1024
ARGON2_T_COST_MAX = 10
ARGON2_PARALLELISM_MAX = 16
SALT_BYTES = 16
DEK_BYTES = 32
# The fallback associated-data context. Callers that know which collection the
# key belongs to must pass that context explicitly; this default exists only so
# wrappers written before the binding keep opening.
DEK_CONTEXT = b"netsanctum:vault:dek:v1"


@dataclass(frozen=True, slots=True)
class WrappedKey:
    """A data key wrapped under a passphrase, and the cost needed to unwrap it.

    The cost travels with the wrapper rather than being assumed at read time. That
    is what lets the parameters change: raising them would lock out every vault
    that did not store which parameters it was sealed with.
    """

    salt: str
    wrapped: str
    kdf: str = KDF_NAME
    wrap_version: int = 1
    # Argon2id
    t_cost: int = ARGON2_T_COST
    m_cost: int = ARGON2_M_COST
    parallelism: int = ARGON2_PARALLELISM

    def params(self) -> dict[str, int]:
        """The cost parameters worth persisting for this KDF."""
        return {"t_cost": self.t_cost, "m_cost": self.m_cost, "parallelism": self.parallelism}


# The wrapper envelope version. v1 bound the ciphertext to `context` alone; v2
# binds it to the KDF name, its canonical parameters and the salt as well. A wrapper
# that silently accepted a different cost than it was sealed with would derive a
# different key and report a wrong passphrase for a correct one.
WRAP_VERSION = 2
WRAP_AAD_PREFIX = b"ns:vault:wrap:v2"


def canonical_params(params: dict[str, int]) -> bytes:
    """The cost as bytes that cannot be spelled two ways.

    `sort_keys` and compact separators fix the one representation `json.loads` must
    produce for `json.dumps` to verify against. A dict written `{t,m}` and read as
    `{m,t}` is the same cost and a different AAD without this.
    """
    return json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")


def wrap_aad(context: bytes, *, kdf: str, params: dict[str, int], salt: bytes) -> bytes:
    """What v2 binds the wrapped key to. `context` already carries the collection."""
    return (
        WRAP_AAD_PREFIX
        + b"|"
        + context
        + b"|"
        + kdf.encode("utf-8")
        + b"|"
        + canonical_params(params)
        + b"|"
        + salt
    )


def kek_for_wrapper(wrapped: WrappedKey, passphrase: str) -> tuple[bytes, bytes]:
    """The KEK and the salt for a wrapper, derived at its stored cost.

    Reading the MAC needs the KEK without unwrapping anything, so this splits the
    derivation out of `unwrap_data_key` rather than duplicating it.
    """
    try:
        salt = decode_b64(wrapped.salt)
    except ValueError as error:
        raise UnlockError("The stored key wrapper is malformed") from error
    return derive_kek(passphrase, salt, kdf=wrapped.kdf, **wrapped.params()), salt


def _passphrase_bytes(passphrase: str) -> bytes:
    """One spelling of a passphrase. NFC so macOS/Windows spellings agree."""
    import unicodedata

    return unicodedata.normalize("NFC", passphrase).encode("utf-8")


def _check_kdf_cost(kdf: str, cost: dict[str, int]) -> None:
    """Refuse absurd cost parameters before deriving, without allocating."""
    if kdf != KDF_NAME:
        raise UnlockError(f"Unsupported key derivation {kdf!r}")
    m = cost.get("m_cost", ARGON2_M_COST)
    t = cost.get("t_cost", ARGON2_T_COST)
    par = cost.get("parallelism", ARGON2_PARALLELISM)
    if not (8 <= m <= ARGON2_M_COST_MAX):
        raise UnlockError("Unsupported key derivation cost")
    if not (1 <= t <= ARGON2_T_COST_MAX):
        raise UnlockError("Unsupported key derivation cost")
    if not (1 <= par <= ARGON2_PARALLELISM_MAX):
        raise UnlockError("Unsupported key derivation cost")


def derive_kek(passphrase: str, salt: bytes, *, kdf: str = KDF_NAME, **cost: int) -> bytes:
    """Stretch the passphrase into a key-encryption key, at the cost the wrapper names."""
    if not passphrase:
        raise UnlockError("A passphrase is required")
    if kdf != KDF_NAME:
        raise UnlockError(f"Unsupported key derivation {kdf!r}")
    _check_kdf_cost(kdf, cost)
    return argon2.low_level.hash_secret_raw(
        _passphrase_bytes(passphrase),
        salt,
        time_cost=cost.get("t_cost", ARGON2_T_COST),
        memory_cost=cost.get("m_cost", ARGON2_M_COST),
        parallelism=cost.get("parallelism", ARGON2_PARALLELISM),
        hash_len=DEK_BYTES,
        type=argon2.low_level.Type.ID,
    )


def new_data_key() -> bytes:
    return os.urandom(DEK_BYTES)


def wrap_data_key(
    dek: bytes,
    passphrase: str,
    *,
    context: bytes = DEK_CONTEXT,
    **cost: int,
) -> WrappedKey:
    """Wrap a data key under a passphrase and return only what is safe to persist."""
    if len(dek) != DEK_BYTES:
        raise ValueError("A data key must be 32 bytes")
    _check_kdf_cost(KDF_NAME, cost)
    # The wrapper is built first, so the cost it carries is the cost used. Deriving
    # with one set of parameters and persisting another would lock the owner out.
    wrapper = WrappedKey(salt="", wrapped="", kdf=KDF_NAME, wrap_version=WRAP_VERSION, **cost)
    salt = os.urandom(SALT_BYTES)
    kek = derive_kek(passphrase, salt, **wrapper.params(), kdf=wrapper.kdf)
    nonce = os.urandom(12)
    aad = wrap_aad(context, kdf=wrapper.kdf, params=wrapper.params(), salt=salt)
    blob = nonce + AESGCM(kek).encrypt(nonce, dek, aad)
    return replace(wrapper, salt=encode_b64(salt), wrapped=WRAPPED_PREFIX + encode_b64(blob))


def unwrap_data_key(wrapped: WrappedKey, passphrase: str, *, context: bytes = DEK_CONTEXT) -> bytes:
    """Recover the DEK. Raises UnlockError for any wrong passphrase or foreign wrapper."""
    if wrapped.kdf != KDF_NAME:
        raise UnlockError(f"Unsupported key derivation {wrapped.kdf!r}")
    if not wrapped.wrapped.startswith(WRAPPED_PREFIX):
        raise UnlockError("The stored key wrapper is malformed")
    kek, salt = kek_for_wrapper(wrapped, passphrase)
    return unwrap_with_kek(wrapped, kek, salt, context)


def unwrap_with_kek(wrapped: WrappedKey, kek: bytes, salt: bytes, context: bytes) -> bytes:
    """Open a wrapper with a KEK you already hold.

    Deriving a KEK is the expensive half of an unlock, and a batch operation —
    re-wrapping a collection, migrating a media set — has many wrappers and one
    passphrase. This is how that pays for the derivation once instead of per row.

    Public on purpose: verifying a passphrase for a destructive action (deleting
    a space) needs the same KEK the unlock derives, without opening the vault.
    """
    try:
        raw = decode_b64(wrapped.wrapped.removeprefix(WRAPPED_PREFIX))
    except ValueError as error:
        raise UnlockError("The stored key wrapper is malformed") from error
    # Exactly nonce (12) + DEK (32) + tag (16): anything else is truncation or
    # concatenation, never a wrapper. Pre-v2 wrappers bound only `context` and
    # are refused outright: the old standard was deleted with its data.
    if len(raw) != 12 + DEK_BYTES + 16:
        raise UnlockError("The stored key wrapper is malformed")
    if wrapped.wrap_version < WRAP_VERSION:
        raise UnlockError("The stored key wrapper uses a retired format")
    aad = wrap_aad(context, kdf=wrapped.kdf, params=wrapped.params(), salt=salt)
    try:
        dek = AESGCM(kek).decrypt(raw[:12], raw[12:], aad)
    except InvalidTag as exc:
        raise UnlockError("Wrong passphrase for this Vault") from exc
    if len(dek) != DEK_BYTES:
        raise UnlockError("The unwrapped key has the wrong length")
    return dek
