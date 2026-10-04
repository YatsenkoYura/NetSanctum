"""Sealed Vault collections.

An encrypted collection keeps a random data-encryption key (DEK) that is never
stored in the clear. The DEK is wrapped by a key-encryption key derived from the
owner's passphrase with Argon2id, and only the wrapper is persisted. Wrappers
written under scrypt before that stay readable. The unwrapped
DEK lives in Redis for as long as the vault is unlocked, so the passphrase is
never written down anywhere.

The split exists so that changing the passphrase re-wraps one small blob instead
of every image and every video byte in the collection.
"""

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass, replace
from typing import Any

import argon2
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

WRAPPED_PREFIX = "nsk:v1:"
PAYLOAD_PREFIX = "nsp:v1:"
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


class VaultUnlockError(ValueError):
    """The passphrase did not unwrap this collection's key."""


class WeakPassphraseError(ValueError):
    """Refused at creation, not at unlock: there is no recovery for a sealed vault,
    so a passphrase nobody could guess has to be demanded when it is chosen."""


MIN_PASSPHRASE_LENGTH = 12
# The shortest possible denylist: the passwords that turn an offline attack into a
# first-try success. Checked in lowercase, with and without a trailing digit run.
COMMON_PASSPHRASES = frozenset(
    {
        "password",
        "parol",
        "пароль",
        "qwerty",
        "йцукен",
        "123456",
        "12345678",
        "123456789",
        "111111",
        "000000",
        "iloveyou",
        "letmein",
        "welcome",
        "admin",
        "administrator",
        "netsanctum",
        "vault",
        "privat",
        "приват",
        "private",
        "secret",
        "секрет",
        "sekret",
    }
)


def check_passphrase_strength(passphrase: str) -> None:
    """Refuse a passphrase that cannot protect a vault, with the reason named.

    Only length and a denylist: anything cleverer needs a dictionary the server does
    not have, and a check that rejects a passphrase the owner already uses would
    lock them out of their own vault. That is why this runs at creation and at
    change, never at unlock.
    """
    candidate = (passphrase or "").strip()
    if len(candidate) < MIN_PASSPHRASE_LENGTH:
        raise WeakPassphraseError(
            f"Пароль должен быть не короче {MIN_PASSPHRASE_LENGTH} символов — "
            "восстановления для зашифрованного хранилища нет"
        )
    stripped = candidate.lower().rstrip("0123456789")
    if candidate.lower() in COMMON_PASSPHRASES or stripped in COMMON_PASSPHRASES:
        raise WeakPassphraseError("Этот пароль слишком частый — выберите другой")


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


PUB_MAC_INFO = b"ns:vault:pub-mac:v1"


def kek_for_wrapper(wrapped: WrappedKey, passphrase: str) -> tuple[bytes, bytes]:
    """The KEK and the salt for a wrapper, derived at its stored cost.

    Reading the MAC needs the KEK without unwrapping anything, so this splits the
    derivation out of `unwrap_data_key` rather than duplicating it.
    """
    salt = _unb64(wrapped.salt)
    return derive_kek(passphrase, salt, kdf=wrapped.kdf, **wrapped.params()), salt


def inbox_pub_mac(kek: bytes, public_key: bytes, collection_id: int) -> str:
    """Authenticate the inbox public key under a key derived from the KEK.

    The MAC key is not the KEK itself: the KEK unwraps the inbox key, the MAC key
    only vouches for the public half. Separating them means a verifier needs no
    unwrapping power.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"ns:vault:pub-mac:salt:v1",
        info=PUB_MAC_INFO,
    )
    mac_key = hkdf.derive(kek)
    tag = hmac.new(mac_key, public_key + b"|" + str(collection_id).encode("utf-8"), hashlib.sha256)
    return tag.hexdigest()


def verify_inbox_pub_mac(kek: bytes, public_key: bytes, collection_id: int, mac: str | None) -> bool:
    """Whether the stored public key is the one the passphrase sealed.

    `compare_digest`, not `==`: the comparison itself must not leak where two tags
    first differ. A missing MAC is not a failure here — rows from before the MAC
    existed get theirs on the way through the unlock that reads them.
    """
    if not mac:
        return False
    expected = inbox_pub_mac(kek, public_key, collection_id)
    return hmac.compare_digest(expected, mac)


def inbox_pub_fingerprint(public_key: bytes) -> str:
    """The first 16 hex of the public key's SHA-256, for checking against the
    extension. Short enough to compare by eye, long enough that a lookalike key
    does not happen by accident."""
    return hashlib.sha256(public_key).hexdigest()[:16]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def derive_kek(passphrase: str, salt: bytes, *, kdf: str = KDF_NAME, **cost: int) -> bytes:
    """Stretch the passphrase into a key-encryption key, at the cost the wrapper names."""
    if not passphrase:
        raise VaultUnlockError("A passphrase is required")
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
    raise VaultUnlockError(f"Unsupported key derivation {kdf!r}")


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
        raise VaultUnlockError(f"Unsupported key derivation {kdf!r}")
    # The wrapper is built first, so the cost it carries is the cost used. Deriving
    # with one set of parameters and persisting another would lock the owner out.
    wrapper = WrappedKey(salt="", wrapped="", kdf=kdf, wrap_version=WRAP_VERSION, **cost)
    salt = os.urandom(SALT_BYTES)
    kek = derive_kek(passphrase, salt, **wrapper.params(), kdf=wrapper.kdf)
    nonce = os.urandom(12)
    aad = wrap_aad(context, kdf=wrapper.kdf, params=wrapper.params(), salt=salt)
    blob = nonce + AESGCM(kek).encrypt(nonce, dek, aad)
    return replace(wrapper, salt=_b64(salt), wrapped=WRAPPED_PREFIX + _b64(blob))


def unwrap_data_key(wrapped: WrappedKey, passphrase: str, *, context: bytes = DEK_CONTEXT) -> bytes:
    """Recover the DEK. Raises VaultUnlockError for any wrong passphrase."""
    # An unknown KDF is refused rather than assumed: guessing here would derive the
    # wrong key and report a wrong passphrase, which sends the owner looking for a
    # typo instead of at the upgrade that changed the derivation.
    if wrapped.kdf not in SUPPORTED_KDFS:
        raise VaultUnlockError(f"Unsupported key derivation {wrapped.kdf!r}")
    if not wrapped.wrapped.startswith(WRAPPED_PREFIX):
        raise VaultUnlockError("The stored key wrapper is malformed")
    kek, salt = kek_for_wrapper(wrapped, passphrase)
    return _unwrap_with_kek(wrapped, kek, salt, context)


def _unwrap_with_kek(wrapped: WrappedKey, kek: bytes, salt: bytes, context: bytes) -> bytes:
    """Open a wrapper with an already-derived KEK, so one unlock derives once."""
    raw = _unb64(wrapped.wrapped.removeprefix(WRAPPED_PREFIX))
    if len(raw) <= 12:
        raise VaultUnlockError("The stored key wrapper is malformed")
    if wrapped.wrap_version >= WRAP_VERSION:
        aad = wrap_aad(context, kdf=wrapped.kdf, params=wrapped.params(), salt=salt)
    else:
        # Wrappers from before the versioned AAD bound only `context`. They open
        # as they always did, and the next unlock re-wraps them into v2.
        aad = context
    try:
        dek = AESGCM(kek).decrypt(raw[:12], raw[12:], aad)
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
