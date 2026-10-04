"""Passphrase to key-encryption key, and the wrapper that stores a data key.

This is where an owner's passphrase becomes the only secret a sealed collection
has: a random data key is wrapped by a KEK derived from the passphrase with
Argon2id, and only the wrapper is persisted. Changing the passphrase re-wraps one
small blob instead of every byte of the collection.

Argon2id runs at RFC 9106's second recommended option — 64 MiB, three passes,
four lanes. An unlock happens once per tab and then lives in Redis for the
session, so the cost is paid rarely; an offline guessing attack pays it per
attempt, forever. The old scrypt parameters are kept in the reader, not the
writer: a passphrase that opens a vault today must open it after every upgrade,
or the upgrade is data loss.

No pepper, deliberately. The `vault_collections` row carries the wrapper, its
salt and its cost and nothing else, so a stolen database is an offline guessing
problem rather than an insider problem — the passphrase is the whole secret.

Lives in core because the mechanism is not a vault's: any module that has to
protect a key with something a human typed needs exactly this, and a second
implementation of a passphrase KDF is a second thing to get wrong. Nothing here
decides what makes a passphrase acceptable — that is a product judgement and
stays with the module that asks.
"""

import hashlib
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
# What a wrapper written before this change says about itself. It stays readable
# for good: a passphrase that opens a vault today must open it after every
# upgrade, or the upgrade is data loss.
LEGACY_KDF_NAME = "scrypt"
SUPPORTED_KDFS = frozenset({KDF_NAME, LEGACY_KDF_NAME})

# Argon2id cost: RFC 9106's second recommended option (64 MiB, three passes, four
# lanes). An unlock happens once per tab and then lives in Redis for the session,
# so the seconds it costs are paid rarely, while an offline guessing attack pays
# them per attempt, forever. The old scrypt parameters cost about 100ms — cheap
# enough that a GPU turned a dictionary into an afternoon.
ARGON2_M_COST = 64 * 1024
ARGON2_T_COST = 3
ARGON2_PARALLELISM = 4

# scrypt parameters, kept only to read wrappers that were written with them.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
DEK_BYTES = 32
# Bound into the wrapper by default. A caller that knows which collection the key
# belongs to should pass that id instead, so a wrapper cannot be copied across.
DEK_CONTEXT = b"netsanctum:vault:dek:v1"


# Bound into the wrapper by default. A caller that knows which collection the key
# belongs to should pass that id instead, so a wrapper cannot be copied across.
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
    # scrypt, for wrappers written before Argon2id
    n: int = SCRYPT_N
    r: int = SCRYPT_R
    p: int = SCRYPT_P

    def params(self) -> dict[str, int]:
        """The cost parameters worth persisting for this KDF.

        Only the ones its own KDF reads. Storing scrypt's n/r/p next to an Argon2id
        wrapper would suggest they had a say in how it was sealed.
        """
        if self.kdf == LEGACY_KDF_NAME:
            return {"n": self.n, "r": self.r, "p": self.p}
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
    salt = decode_b64(wrapped.salt)
    return derive_kek(passphrase, salt, kdf=wrapped.kdf, **wrapped.params()), salt


def derive_kek(passphrase: str, salt: bytes, *, kdf: str = KDF_NAME, **cost: int) -> bytes:
    """Stretch the passphrase into a key-encryption key, at the cost the wrapper names."""
    if not passphrase:
        raise UnlockError("A passphrase is required")
    raw = passphrase.encode("utf-8")
    if kdf == LEGACY_KDF_NAME:
        n, r, p = cost.get("n", SCRYPT_N), cost.get("r", SCRYPT_R), cost.get("p", SCRYPT_P)
        return hashlib.scrypt(raw, salt=salt, n=n, r=r, p=p, dklen=DEK_BYTES, maxmem=132 * n * r * 2)
    if kdf == KDF_NAME:
        return argon2.low_level.hash_secret_raw(
            raw,
            salt,
            time_cost=cost.get("t_cost", ARGON2_T_COST),
            memory_cost=cost.get("m_cost", ARGON2_M_COST),
            parallelism=cost.get("parallelism", ARGON2_PARALLELISM),
            hash_len=DEK_BYTES,
            type=argon2.low_level.Type.ID,
        )
    raise UnlockError(f"Unsupported key derivation {kdf!r}")


def new_data_key() -> bytes:
    return os.urandom(DEK_BYTES)


def wrap_data_key(
    dek: bytes,
    passphrase: str,
    *,
    context: bytes = DEK_CONTEXT,
    kdf: str = KDF_NAME,
    **cost: int,
) -> WrappedKey:
    """Wrap a data key under a passphrase and return only what is safe to persist."""
    if len(dek) != DEK_BYTES:
        raise ValueError("A data key must be 32 bytes")
    if kdf not in SUPPORTED_KDFS:
        raise UnlockError(f"Unsupported key derivation {kdf!r}")
    # The wrapper is built first, so the cost it carries is the cost used. Deriving
    # with one set of parameters and persisting another would lock the owner out.
    wrapper = WrappedKey(salt="", wrapped="", kdf=kdf, wrap_version=WRAP_VERSION, **cost)
    salt = os.urandom(SALT_BYTES)
    kek = derive_kek(passphrase, salt, **wrapper.params(), kdf=wrapper.kdf)
    nonce = os.urandom(12)
    aad = wrap_aad(context, kdf=wrapper.kdf, params=wrapper.params(), salt=salt)
    blob = nonce + AESGCM(kek).encrypt(nonce, dek, aad)
    return replace(wrapper, salt=encode_b64(salt), wrapped=WRAPPED_PREFIX + encode_b64(blob))


def unwrap_data_key(wrapped: WrappedKey, passphrase: str, *, context: bytes = DEK_CONTEXT) -> bytes:
    """Recover the DEK. Raises UnlockError for any wrong passphrase."""
    # An unknown KDF is refused rather than assumed: guessing here would derive the
    # wrong key and report a wrong passphrase, which sends the owner looking for a
    # typo instead of at the upgrade that changed the derivation.
    if wrapped.kdf not in SUPPORTED_KDFS:
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
    """
    raw = decode_b64(wrapped.wrapped.removeprefix(WRAPPED_PREFIX))
    if len(raw) <= 12:
        raise UnlockError("The stored key wrapper is malformed")
    if wrapped.wrap_version >= WRAP_VERSION:
        aad = wrap_aad(context, kdf=wrapped.kdf, params=wrapped.params(), salt=salt)
    else:
        # Wrappers from before the versioned AAD bound only `context`. They open
        # as they always did, and the next unlock re-wraps them into v2.
        aad = context
    try:
        dek = AESGCM(kek).decrypt(raw[:12], raw[12:], aad)
    except InvalidTag as exc:
        raise UnlockError("Wrong passphrase for this Vault") from exc
    if len(dek) != DEK_BYTES:
        raise UnlockError("The unwrapped key has the wrong length")
    return dek
