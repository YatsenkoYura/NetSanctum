"""The blind-write envelope: sealing to a key the writer does not control.

Writing into a sealed collection must not require its passphrase: a browser
extension has no way to be given one, and a capture that arrives in the clear
would defeat the vault. So the collection also carries an X25519 keypair. The
public half sits in the clear and is enough to seal a new record; the private
half is sealed under the passphrase and is what a later unlock needs to open
anything.

The writer mints an ephemeral keypair per write and throws it away: only the
shared secret survives, inside the wrapper. Nothing in the database can
reproduce it, and nothing in the database can read it either.

Version 2 derives the record key through HKDF with the whole identity in the
`info` — both public keys, plus the collection and row ids, length-framed so no
two different triples spell the same bytes. Version 1 derived it as
`sha256(prefix || shared)`: a hash where a KDF belongs, binding neither party,
so a wrapper lifted from one row could be offered to another. V1 still opens,
because a passphrase that opens a vault today must open it after every upgrade;
the next write of a row is what moves it forward, and no migration touches them.

Lives in core for the same reason as the KDF: an asymmetric sealed box with a
bound identity is a mechanism, and a blind write is not the only thing that needs
one.
"""

import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from app.core.crypto.codec import decode_b64, encode_b64
from app.core.crypto.envelope import context_for, seal, unseal
from app.core.crypto.kdf import DEK_BYTES, new_data_key

PUB_MAC_INFO = b"ns:vault:pub-mac:v1"


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
    return INBOX_PREFIX_V2 + encode_b64(blob)


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
        raw = decode_b64(write.wrapped_key.removeprefix(INBOX_PREFIX_V2))
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
        raw = decode_b64(write.wrapped_key.removeprefix(INBOX_PREFIX))
        if len(raw) <= 32:
            raise ValueError("The stored item key is malformed")
        kek = hashlib.sha256(INBOX_PREFIX.encode("ascii") + _inbox_shared(private_key, raw[:32])).digest()
        try:
            return unseal(kek, raw[32:].decode("utf-8"), context=context)
        except ValueError as exc:
            raise ValueError("The stored item key does not belong to this Vault") from exc
    raise ValueError("The stored item key is malformed")
