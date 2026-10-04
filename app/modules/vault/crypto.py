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
INBOX_PREFIX_V2 = "nsi:v2:"


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


# The current envelope, v2.
#
# v1 derived the item-wrapping key as `sha256("nsi:v1:" || shared)`: a prefix
# concatenated with a secret, which is a hash where a KDF belongs, and which
# binds neither party. Anyone holding an ephemeral keypair and a shared secret
# could reproduce the key without holding either identity, and nothing in the
# record said which collection or which card it belonged to — so a wrapped key
# lifted from one row could be offered to another.
#
# v2 derives through HKDF-SHA256 with the whole identity in the `info`: both
# public keys — the ephemeral one and the collection's, which is also copied
# into the record so a reader needs no second lookup — plus the collection id
# and the row id, length-framed so no two different triples can spell the same
# bytes. The very same buffer is the AEAD's associated data, so the item key
# is bound to that identity twice over: once in the derivation, once in the tag.
#
# The copied recipient key is not trusted as identity — editing it yields a
# different derived key and a failed tag, not a different reader. It is there
# so the record can be opened from itself alone.
INBOX_VERSION_V2 = 2
INBOX_BINDING_PREFIX = b"ns:vault:inbox:v2"
INBOX_HKDF_SALT = b"ns:vault:inbox:salt:v2"
# version | ephemeral public | collection public | nonce | ciphertext | tag
INBOX_V2_FIXED_SIZE = 1 + 32 + 32 + 12 + 32 + 16


def inbox_binding(collection_id: int, kind: str, row_id: int, ephemeral: bytes, recipient: bytes) -> bytes:
    """The canonical identity of one blind write: what its key is bound to.

    Length-framed rather than joined with separators, so `{collection 1, row 23}`
    and `{collection 12, row 3}` cannot produce the same bytes.
    """
    kind_bytes = kind.encode("utf-8")
    return b"".join(
        (
            INBOX_BINDING_PREFIX,
            int(collection_id).to_bytes(4, "big"),
            len(kind_bytes).to_bytes(2, "big"),
            kind_bytes,
            int(row_id).to_bytes(4, "big"),
            ephemeral,
            recipient,
        )
    )


def inbox_write_version(wrapped_key: str) -> int:
    """Which envelope a stored wrapper is written in: 1, 2, or 0 for neither."""
    if wrapped_key.startswith(INBOX_PREFIX_V2):
        return 2
    if wrapped_key.startswith(INBOX_PREFIX):
        return 1
    return 0


def _inbox_shared(private_key: bytes, peer_public: bytes) -> bytes:
    """The X25519 secret between our half and the other party's public key.

    Symmetric on purpose: the writer computes it with its ephemeral private key
    and the collection's public key, the reader with the collection's private
    key and the ephemeral public key, and both arrive at the same bytes.
    """
    return X25519PrivateKey.from_private_bytes(private_key).exchange(
        X25519PublicKey.from_public_bytes(peer_public)
    )


def _inbox_wrapping_key(shared: bytes, binding: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=DEK_BYTES,
        salt=INBOX_HKDF_SALT,
        info=binding,
    ).derive(shared)


def seal_for_inbox(
    payload: bytes, public_key: bytes, *, collection_id: int, kind: str, row_id: int
) -> SealedWrite:
    """Encrypt a payload so that only the holder of `private_key` can read it.

    An ephemeral X25519 keypair is generated per write and thrown away: only the
    shared secret survives, inside the wrapped key. Nothing in the database can
    reproduce it.

    Writes the current envelope, which needs nothing but the collection's public
    key — no passphrase, no session — so a blind write is as strong as an
    unlocked one. Rows written under v1 keep opening as they always did; the
    next write of that row is what moves it, and no migration touches them.
    """
    item_key = new_data_key()
    return SealedWrite(
        payload=seal(item_key, payload, context=context_for(kind, row_id)),
        wrapped_key=rewrap_inbox_key(
            item_key, public_key, collection_id=collection_id, kind=kind, row_id=row_id
        ),
    )


def rewrap_inbox_key(
    item_key: bytes, public_key: bytes, *, collection_id: int, kind: str, row_id: int
) -> str:
    """Wrap an item key under a collection's public key, current envelope.

    Separate from `seal_for_inbox` because re-wrapping must not mint a new item
    key: a rotation changes the wrapper around each payload and leaves every
    payload byte exactly where it was, which is the difference between
    re-wrapping a vault and rewriting it. Returns the wrapper on its own for
    exactly that reason.
    """
    if len(public_key) != 32:
        raise ValueError("An inbox public key must be 32 bytes")
    if collection_id is None:
        raise ValueError("A blind write must belong to a collection")
    if len(item_key) != DEK_BYTES:
        raise ValueError("An item key must be 32 bytes")
    ephemeral_private, ephemeral_public = generate_inbox_keypair()
    binding = inbox_binding(collection_id, kind, row_id, ephemeral_public, public_key)
    kek = _inbox_wrapping_key(_inbox_shared(ephemeral_private, public_key), binding)
    nonce = os.urandom(12)
    wrapped = nonce + AESGCM(kek).encrypt(nonce, item_key, binding)
    blob = bytes([INBOX_VERSION_V2]) + ephemeral_public + public_key + wrapped
    return INBOX_PREFIX_V2 + _b64(blob)


def open_from_inbox(
    write: SealedWrite, private_key: bytes, *, collection_id: int, kind: str, row_id: int
) -> bytes:
    """Recover a blind write's payload, from either envelope version."""
    return unseal(
        open_inbox_key(write, private_key, collection_id=collection_id, kind=kind, row_id=row_id),
        write.payload,
        context=context_for(kind, row_id),
    )


def open_inbox_key(
    write: SealedWrite, private_key: bytes, *, collection_id: int, kind: str, row_id: int
) -> bytes:
    """The item key a record wraps, from either envelope version.

    What the payload reader does first, exposed because a key rotation needs
    exactly this: the key, without the payload. Nothing is decrypted to re-wrap
    a row, and no payload is rewritten.
    """
    if len(private_key) != 32:
        raise ValueError("An inbox private key must be 32 bytes")
    context = context_for(kind, row_id)
    version = inbox_write_version(write.wrapped_key)
    if version == 2:
        raw = _unb64(write.wrapped_key.removeprefix(INBOX_PREFIX_V2))
        if len(raw) != INBOX_V2_FIXED_SIZE:
            raise ValueError("The stored item key is malformed")
        if raw[0] != INBOX_VERSION_V2:
            raise ValueError(f"Unsupported inbox envelope version {raw[0]}")
        ephemeral = raw[1:33]
        recipient = raw[33:65]
        binding = inbox_binding(collection_id, kind, row_id, ephemeral, recipient)
        kek = _inbox_wrapping_key(_inbox_shared(private_key, ephemeral), binding)
        try:
            return AESGCM(kek).decrypt(raw[65:77], raw[77:], binding)
        except InvalidTag as exc:
            raise ValueError("The stored item key does not belong to this Vault") from exc
    if version == 1:
        # The original envelope: a hash of the prefix and the shared secret,
        # with the row bound only through the payload's own context. It opens
        # exactly as it always did, because a passphrase that opens a vault
        # today must open it after every upgrade.
        raw = _unb64(write.wrapped_key.removeprefix(INBOX_PREFIX))
        if len(raw) <= 32:
            raise ValueError("The stored item key is malformed")
        kek = hashlib.sha256(INBOX_PREFIX.encode("ascii") + _inbox_shared(private_key, raw[:32])).digest()
        try:
            return unseal(kek, raw[32:].decode("utf-8"), context=context)
        except ValueError as exc:
            raise ValueError("The stored item key does not belong to this Vault") from exc
    raise ValueError("The stored item key is malformed")


# ── Per-collection file key ────────────────────────────────────────────
# Sealed collections used to encrypt their pictures with the shared application
# file key. A file key per collection narrows that — and it is derived, not
# stored: `HKDF(inbox_private, salt=collection, info=file-key:v1)`. There is no
# column, no backfill and no "missing key" state, because derivation cannot be
# missing: every sealed row yields its file key the moment it is unlocked.
# A stored wrapper was considered and rejected. It would buy rotation without
# re-encryption, which is theater — a rotated key that still opens old files is
# not a rotation — while adding a migration, a backfill and a corrupt state.
# Rotating here means bumping the info version and re-encrypting, the same work
# either way. Compromising the inbox key gives both keys in both designs; the
# info string keeps their domains apart.

FILE_KEY_INFO = b"ns:vault:file-key:v1"


def derive_file_key(private_key: bytes, collection_id: int) -> bytes:
    """The collection's file key. Deterministic, 32 bytes, unique per collection."""
    if len(private_key) != DEK_BYTES:
        raise ValueError("A data key must be 32 bytes")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=DEK_BYTES,
        salt=f"ns:vault:file-key:{collection_id}".encode(),
        info=FILE_KEY_INFO,
    )
    return hkdf.derive(private_key)
