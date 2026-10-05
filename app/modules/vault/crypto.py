"""What Vault decides about keys, and what it borrows from core.

The mechanism moved to `app.core.crypto`: the Argon2id KDF and the wrapper that
stores a data key under a passphrase, the payload envelope, and the X25519 blind
write envelope. All of it is re-exported here, so every import in the tree and
every `except VaultUnlockError` keeps working exactly as it did.

What stays is the two decisions that are Vault's and not the mechanism's:

- **What makes a passphrase acceptable.** Length and a denylist, refused where
  the passphrase is chosen and never checked at unlock — a check that rejects a
  passphrase somebody already uses locks them out of their own vault, and there is
  no recovery.
- **Which key encrypts which file.** The per-collection file key is derived from
  the collection's inbox key rather than stored, so there is no column, no
  backfill and no "missing key" state: derivation cannot be missing. That is a
  product choice about what a collection's media is bound to, and it lives here
  even though the HKDF underneath is core's.

An encrypted collection keeps a random data-encryption key (DEK) that is never
stored in the clear. The DEK is wrapped by a key-encryption key derived from the
owner's passphrase with Argon2id, and only the wrapper is persisted. The
unwrapped DEK lives in Redis for as long as the vault is unlocked, so the
passphrase is never written down anywhere. That split is why changing the
passphrase re-wraps one small blob instead of every image and every video byte in
the collection.
"""

from app.core.crypto import (
    ARGON2_M_COST,
    ARGON2_M_COST_MAX,
    ARGON2_PARALLELISM,
    ARGON2_PARALLELISM_MAX,
    ARGON2_T_COST,
    ARGON2_T_COST_MAX,
    DEK_BYTES,
    DEK_CONTEXT,
    INBOX_BINDING_PREFIX,
    INBOX_HKDF_SALT,
    INBOX_PREFIX,
    INBOX_PREFIX_V2,
    INBOX_V2_FIXED_SIZE,
    INBOX_VERSION_V2,
    KDF_NAME,
    PAYLOAD_PREFIX,
    PUB_MAC_INFO,
    RES_AAD_PREFIX,
    SALT_BYTES,
    SEALED_MAGIC,
    TRANSFER_CHUNK_SIZE,
    TRANSFER_DEK_BYTES,
    TRANSFER_HEADER_SIZE,
    TRANSFER_M_COST,
    TRANSFER_NONCE_SIZE,
    TRANSFER_PARALLELISM,
    TRANSFER_SALT_BYTES,
    TRANSFER_T_COST,
    TRANSFER_VERSION,
    WRAP_AAD_PREFIX,
    WRAP_VERSION,
    WRAPPED_PREFIX,
    SealedWrite,
    UnlockError,
    WrappedKey,
    canonical_params,
    check_transfer_cost,
    context_for,
    decode_b64,
    derive_kek,
    derive_subkey,
    derive_transfer_kek,
    encode_b64,
    generate_inbox_keypair,
    inbox_binding,
    inbox_keypair_matches,
    inbox_pub_fingerprint,
    inbox_pub_mac,
    inbox_write_version,
    is_sealed,
    iter_sealed_chunks,
    kek_for_wrapper,
    new_data_key,
    new_transfer_dek,
    open_chunked_range,
    open_from_inbox,
    open_inbox_key,
    open_small_resource,
    parse_chunked_header,
    read_sealed_range,
    rewrap_inbox_key,
    seal,
    seal_chunked_resource,
    seal_chunked_stream,
    seal_for_inbox,
    seal_small_resource,
    transfer_kdf_params,
    transfer_resource_key,
    transfer_wrap_aad,
    unseal,
    unwrap_data_key,
    unwrap_package_dek,
    unwrap_with_kek,
    verify_inbox_pub_mac,
    wrap_aad,
    wrap_data_key,
    wrap_package_dek,
)

# The vault's own name for core's error, kept because a module in core cannot know
# which module is asking. Same class object: `except VaultUnlockError` catches
# everything core raises for a failed unwrap, which is what every caller expects.
VaultUnlockError = UnlockError

# ── Passphrase policy ──────────────────────────────────────────────────────
# Fourteen, not twelve. This is the only thing standing between a stolen
# `vault_collections` row and an offline dictionary run: the row carries the
# wrapper, its salt and its cost, and nothing else — no pepper, deliberately, so
# that the passphrase is the whole secret. Argon2id at the chosen cost makes each
# guess expensive for the attacker and, at these lengths, still unremarkable for
# the owner. The check runs where the passphrase is chosen and never at unlock,
# because a check that rejects a passphrase somebody already uses locks them out
# of their own vault — which is why a raise here is a refusal, not a warning.


class WeakPassphraseError(ValueError):
    """Refused at creation, not at unlock: there is no recovery for a sealed vault,
    so a passphrase nobody could guess has to be demanded when it is chosen."""


MIN_PASSPHRASE_LENGTH = 14
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
    return derive_subkey(
        private_key,
        salt=f"ns:vault:file-key:{collection_id}".encode(),
        info=FILE_KEY_INFO,
        length=DEK_BYTES,
    )


# Re-exported on purpose: every name above is part of this module's public surface,
# so the import in `app.core.crypto` that fills it in is not an unused import.
__all__ = [
    "ARGON2_M_COST",
    "ARGON2_M_COST_MAX",
    "ARGON2_PARALLELISM",
    "ARGON2_PARALLELISM_MAX",
    "ARGON2_T_COST",
    "ARGON2_T_COST_MAX",
    "COMMON_PASSPHRASES",
    "DEK_BYTES",
    "DEK_CONTEXT",
    "FILE_KEY_INFO",
    "INBOX_BINDING_PREFIX",
    "INBOX_HKDF_SALT",
    "INBOX_PREFIX",
    "INBOX_PREFIX_V2",
    "INBOX_V2_FIXED_SIZE",
    "INBOX_VERSION_V2",
    "KDF_NAME",
    "MIN_PASSPHRASE_LENGTH",
    "PAYLOAD_PREFIX",
    "PUB_MAC_INFO",
    "RES_AAD_PREFIX",
    "SALT_BYTES",
    "SEALED_MAGIC",
    "TRANSFER_CHUNK_SIZE",
    "TRANSFER_DEK_BYTES",
    "TRANSFER_HEADER_SIZE",
    "TRANSFER_M_COST",
    "TRANSFER_NONCE_SIZE",
    "TRANSFER_PARALLELISM",
    "TRANSFER_SALT_BYTES",
    "TRANSFER_T_COST",
    "TRANSFER_VERSION",
    "WRAPPED_PREFIX",
    "WRAP_AAD_PREFIX",
    "WRAP_VERSION",
    "SealedWrite",
    "UnlockError",
    "VaultUnlockError",
    "WeakPassphraseError",
    "WrappedKey",
    "canonical_params",
    "check_passphrase_strength",
    "check_transfer_cost",
    "context_for",
    "decode_b64",
    "derive_file_key",
    "derive_kek",
    "derive_subkey",
    "derive_transfer_kek",
    "encode_b64",
    "generate_inbox_keypair",
    "inbox_binding",
    "inbox_keypair_matches",
    "inbox_pub_fingerprint",
    "inbox_pub_mac",
    "inbox_write_version",
    "is_sealed",
    "iter_sealed_chunks",
    "kek_for_wrapper",
    "new_data_key",
    "new_transfer_dek",
    "open_chunked_range",
    "open_from_inbox",
    "open_inbox_key",
    "open_small_resource",
    "parse_chunked_header",
    "read_sealed_range",
    "rewrap_inbox_key",
    "seal",
    "seal_chunked_resource",
    "seal_chunked_stream",
    "seal_for_inbox",
    "seal_small_resource",
    "transfer_kdf_params",
    "transfer_resource_key",
    "transfer_wrap_aad",
    "unseal",
    "unwrap_data_key",
    "unwrap_package_dek",
    "unwrap_with_kek",
    "verify_inbox_pub_mac",
    "wrap_aad",
    "wrap_data_key",
    "wrap_package_dek",
]
