"""Key derivation and key envelopes, for any module that has a secret to keep.

Four things live here, and the split is by mechanism rather than by caller:

- `codec` — URL-safe base64 for the salts, nonces and keys that go into text.
- `kdf` — a passphrase stretched into a key-encryption key with Argon2id, and
  the wrapper that stores a data key under it. scrypt wrappers from before that
  still open.
- `envelope` — a payload sealed to a data key and bound to its row, and HKDF for
  deriving purpose-separated subkeys.
- `asymmetric` — X25519 keypairs and the blind-write envelope: sealing to a
  holder's public key so a writer with no passphrase can still write.

None of it decides policy. What makes a passphrase acceptable, which rows get
sealed and which file key belongs to which collection are product judgements and
stay with the module that asks. What is here is the part that must not be
reimplemented per module: a second passphrase KDF is a second thing to get wrong,
and the wire formats here are permanent — a wrapper somebody already has has to
open after every upgrade, or the upgrade is data loss.

`app.modules.vault.crypto` re-exports all of it, so existing imports and the
`except VaultUnlockError` in the tree keep working unchanged.
"""

from app.core.crypto.asymmetric import (
    INBOX_BINDING_PREFIX,
    INBOX_HKDF_SALT,
    INBOX_PREFIX,
    INBOX_PREFIX_V2,
    INBOX_V2_FIXED_SIZE,
    INBOX_VERSION_V2,
    PUB_MAC_INFO,
    SealedWrite,
    generate_inbox_keypair,
    inbox_binding,
    inbox_pub_fingerprint,
    inbox_pub_mac,
    inbox_write_version,
    open_from_inbox,
    open_inbox_key,
    rewrap_inbox_key,
    seal_for_inbox,
    verify_inbox_pub_mac,
)
from app.core.crypto.codec import decode_b64, encode_b64
from app.core.crypto.envelope import (
    PAYLOAD_PREFIX,
    context_for,
    derive_subkey,
    is_sealed,
    seal,
    unseal,
)
from app.core.crypto.kdf import (
    ARGON2_M_COST,
    ARGON2_PARALLELISM,
    ARGON2_T_COST,
    DEK_BYTES,
    DEK_CONTEXT,
    KDF_NAME,
    LEGACY_KDF_NAME,
    SALT_BYTES,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    SUPPORTED_KDFS,
    WRAP_AAD_PREFIX,
    WRAP_VERSION,
    WRAPPED_PREFIX,
    UnlockError,
    WrappedKey,
    canonical_params,
    derive_kek,
    kek_for_wrapper,
    new_data_key,
    unwrap_data_key,
    unwrap_with_kek,
    wrap_aad,
    wrap_data_key,
)

__all__ = [
    "ARGON2_M_COST",
    "ARGON2_PARALLELISM",
    "ARGON2_T_COST",
    "DEK_BYTES",
    "DEK_CONTEXT",
    "INBOX_BINDING_PREFIX",
    "INBOX_HKDF_SALT",
    "INBOX_PREFIX",
    "INBOX_PREFIX_V2",
    "INBOX_V2_FIXED_SIZE",
    "INBOX_VERSION_V2",
    "KDF_NAME",
    "LEGACY_KDF_NAME",
    "PAYLOAD_PREFIX",
    "PUB_MAC_INFO",
    "SALT_BYTES",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "SUPPORTED_KDFS",
    "WRAPPED_PREFIX",
    "WRAP_AAD_PREFIX",
    "WRAP_VERSION",
    "SealedWrite",
    "UnlockError",
    "WrappedKey",
    "canonical_params",
    "context_for",
    "decode_b64",
    "derive_kek",
    "derive_subkey",
    "encode_b64",
    "generate_inbox_keypair",
    "inbox_binding",
    "inbox_pub_fingerprint",
    "inbox_pub_mac",
    "inbox_write_version",
    "is_sealed",
    "kek_for_wrapper",
    "new_data_key",
    "open_from_inbox",
    "open_inbox_key",
    "rewrap_inbox_key",
    "seal",
    "seal_for_inbox",
    "unseal",
    "unwrap_data_key",
    "unwrap_with_kek",
    "verify_inbox_pub_mac",
    "wrap_aad",
    "wrap_data_key",
]
