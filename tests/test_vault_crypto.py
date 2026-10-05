"""The sealed-Vault primitives must fail loudly rather than quietly."""

import base64
import unittest

from app.modules.vault.crypto import (
    KDF_NAME,
    PAYLOAD_PREFIX,
    WRAPPED_PREFIX,
    VaultUnlockError,
    context_for,
    is_sealed,
    new_data_key,
    seal,
    unwrap_data_key,
    wrap_data_key,
)

# Cheap KDF parameters: the real cost is validated separately, these keep the
# suite fast without changing the code path.
FAST = {"t_cost": 1, "m_cost": 8, "parallelism": 1}


class DataKeyTests(unittest.TestCase):
    def test_a_wrapped_key_round_trips(self):
        dek = new_data_key()
        wrapped = wrap_data_key(dek, "correct horse", **FAST)
        self.assertTrue(wrapped.wrapped.startswith(WRAPPED_PREFIX))
        self.assertEqual(KDF_NAME, wrapped.kdf)
        self.assertEqual(dek, unwrap_data_key(wrapped, "correct horse"))

    def test_a_wrong_passphrase_is_refused(self):
        wrapped = wrap_data_key(new_data_key(), "correct horse", **FAST)
        with self.assertRaises(VaultUnlockError):
            unwrap_data_key(wrapped, "battery staple")

    def test_an_empty_passphrase_is_refused(self):
        wrapped = wrap_data_key(new_data_key(), "correct horse", **FAST)
        with self.assertRaises(VaultUnlockError):
            unwrap_data_key(wrapped, "")

    def test_a_wrapper_cannot_be_moved_to_another_collection(self):
        # The collection id is bound in as AEAD associated data, so a wrapper
        # copied onto a different vault must not unwrap.
        dek = new_data_key()
        wrapped = wrap_data_key(dek, "pass", **FAST)
        with self.assertRaises(VaultUnlockError):
            unwrap_data_key(wrapped, "pass", context=context_for("collection", 7))

    def test_the_wrapper_never_contains_the_key(self):
        dek = new_data_key()
        wrapped = wrap_data_key(dek, "pass", **FAST)
        blob = base64.urlsafe_b64decode(wrapped.wrapped.removeprefix(WRAPPED_PREFIX).encode())
        self.assertNotIn(dek, blob)
        self.assertNotIn(dek, base64.urlsafe_b64decode(wrapped.salt.encode()))

    def test_two_vaults_get_different_keys_from_one_passphrase(self):
        first = wrap_data_key(new_data_key(), "same passphrase", **FAST)
        second = wrap_data_key(new_data_key(), "same passphrase", **FAST)
        self.assertNotEqual(first.salt, second.salt)
        self.assertNotEqual(first.wrapped, second.wrapped)

    def test_a_malformed_wrapper_is_refused_not_crashed(self):
        dek = new_data_key()
        for broken in ("", "garbage", WRAPPED_PREFIX, WRAPPED_PREFIX + "AAAA"):
            with self.subTest(value=broken):
                wrapped = wrap_data_key(dek, "pass", **FAST)
                object.__setattr__(wrapped, "wrapped", broken)
                with self.assertRaises(VaultUnlockError):
                    unwrap_data_key(wrapped, "pass")

    def test_an_unknown_kdf_is_refused(self):
        wrapped = wrap_data_key(new_data_key(), "pass", **FAST)
        object.__setattr__(wrapped, "kdf", "rot13")
        with self.assertRaises(VaultUnlockError):
            unwrap_data_key(wrapped, "pass")


class Argon2idTests(unittest.TestCase):
    """Argon2id is the one and only KDF. Anything else is refused, not migrated."""

    def test_a_new_wrapper_says_argon2id(self):
        wrapped = wrap_data_key(new_data_key(), "pass", **FAST)
        self.assertEqual("argon2id", wrapped.kdf)

    def test_it_stores_argon2_parameters_and_only_those(self):
        wrapped = wrap_data_key(new_data_key(), "pass", **FAST)
        self.assertEqual(
            {"t_cost": 1, "m_cost": 8, "parallelism": 1},
            wrapped.params(),
        )

    def test_a_retired_scrypt_wrapper_is_refused(self):
        wrapped = wrap_data_key(new_data_key(), "old passphrase", **FAST)
        object.__setattr__(wrapped, "kdf", "scrypt")
        with self.assertRaises(VaultUnlockError):
            unwrap_data_key(wrapped, "old passphrase")

    def test_the_default_cost_is_the_one_we_chose(self):
        """Not a performance test: it pins the parameters a vault is sealed at.

        Quietly lowering these would make every new vault cheaper to crack and no
        test anywhere would notice.
        """
        wrapped = wrap_data_key(new_data_key(), "pass")
        self.assertEqual(64 * 1024, wrapped.m_cost)
        self.assertEqual(3, wrapped.t_cost)
        self.assertEqual(4, wrapped.parallelism)


class WrapAadTests(unittest.TestCase):
    """The wrapper is bound to what it was sealed with, not just to the passphrase."""

    def test_a_wrapper_from_before_the_versioned_aad_is_refused(self):
        """Rows sealed before the versioned AAD bound only `context`. The old
        standard was deleted with its data, so they are refused, not opened."""
        import base64

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        from app.modules.vault.crypto import (
            DEK_CONTEXT,
            WRAPPED_PREFIX,
            WrappedKey,
            derive_kek,
            new_data_key,
            unwrap_data_key,
        )

        dek = new_data_key()
        salt = b"\x00" * 16
        kek = derive_kek("old words", salt, kdf="argon2id", t_cost=1, m_cost=8, parallelism=1)
        nonce = b"\x01" * 12
        blob = nonce + AESGCM(kek).encrypt(nonce, dek, DEK_CONTEXT)
        legacy = WrappedKey(
            salt=base64.urlsafe_b64encode(salt).decode(),
            wrapped=WRAPPED_PREFIX + base64.urlsafe_b64encode(blob).decode(),
            kdf="argon2id",
            wrap_version=1,
            t_cost=1,
            m_cost=8,
            parallelism=1,
        )
        with self.assertRaises(VaultUnlockError):
            unwrap_data_key(legacy, "old words")

    def test_renaming_the_kdf_breaks_the_wrapper(self):
        wrapped = wrap_data_key(new_data_key(), "pass", **FAST)
        object.__setattr__(wrapped, "kdf", "scrypt")
        with self.assertRaises(VaultUnlockError):
            unwrap_data_key(wrapped, "pass")

    def test_changing_the_stored_cost_breaks_the_wrapper(self):
        """The cost in the database is untrusted: accepting a lowered one would
        derive a different key and report a wrong passphrase for a correct one."""
        # t_cost, not m_cost: halving the memory below argon2's floor is refused
        # by the library itself, which proves nothing about the binding.
        wrapped = wrap_data_key(new_data_key(), "pass", **FAST)
        object.__setattr__(wrapped, "t_cost", wrapped.t_cost + 1)
        with self.assertRaises(VaultUnlockError):
            unwrap_data_key(wrapped, "pass")

    def test_the_canonical_params_have_one_spelling(self):
        from app.modules.vault.crypto import canonical_params

        self.assertEqual(
            canonical_params({"m_cost": 8, "t_cost": 1}),
            canonical_params({"t_cost": 1, "m_cost": 8}),
        )


class InboxKeyMacTests(unittest.TestCase):
    """The public inbox key has no passphrase binding it — until the MAC.

    A rewritten public key diverts every blind write without touching the wrapper,
    so the owner's own unlock keeps working. Nothing to notice, unless someone
    checks.
    """

    def mac(self, kek=None):
        from app.modules.vault.crypto import generate_inbox_keypair, inbox_pub_mac

        _private, public = generate_inbox_keypair()
        kek = kek or b"\x02" * 32
        return public, inbox_pub_mac(kek, public, 7)

    def test_the_mac_verifies(self):
        from app.modules.vault.crypto import verify_inbox_pub_mac

        public, mac = self.mac()
        self.assertTrue(verify_inbox_pub_mac(b"\x02" * 32, public, 7, mac))

    def test_a_swapped_public_key_fails(self):
        from app.modules.vault.crypto import generate_inbox_keypair, verify_inbox_pub_mac

        _private, other = generate_inbox_keypair()
        _public, mac = self.mac()
        self.assertFalse(verify_inbox_pub_mac(b"\x02" * 32, other, 7, mac))

    def test_a_copied_mac_fails_for_another_collection(self):
        from app.modules.vault.crypto import verify_inbox_pub_mac

        public, mac = self.mac()
        self.assertFalse(verify_inbox_pub_mac(b"\x02" * 32, public, 8, mac))

    def test_a_missing_mac_is_not_a_pass(self):
        from app.modules.vault.crypto import verify_inbox_pub_mac

        public, _mac = self.mac()
        self.assertFalse(verify_inbox_pub_mac(b"\x02" * 32, public, 7, None))

    def test_the_fingerprint_is_stable_and_short(self):
        from app.modules.vault.crypto import inbox_pub_fingerprint

        public, _mac = self.mac()
        first, second = inbox_pub_fingerprint(public), inbox_pub_fingerprint(public)
        self.assertEqual(first, second)
        self.assertEqual(16, len(first))


class SealTests(unittest.TestCase):
    def test_a_sealed_value_round_trips(self):
        dek = new_data_key()
        context = context_for("item", 42)
        blob = seal(dek, b"private note", context=context)
        self.assertTrue(is_sealed(blob))
        self.assertTrue(blob.startswith(PAYLOAD_PREFIX))
        self.assertEqual(b"private note", unseal_(dek, blob, context))

    def test_the_same_text_seals_differently_each_time(self):
        dek = new_data_key()
        context = context_for("item", 1)
        self.assertNotEqual(seal(dek, b"same", context=context), seal(dek, b"same", context=context))

    def test_a_payload_cannot_be_moved_to_another_row(self):
        dek = new_data_key()
        blob = seal(dek, b"private", context=context_for("item", 1))
        with self.assertRaises(ValueError):
            unseal_(dek, blob, context_for("item", 2))

    def test_another_vaults_key_cannot_read_it(self):
        blob = seal(new_data_key(), b"private", context=context_for("item", 1))
        with self.assertRaises(ValueError):
            unseal_(new_data_key(), blob, context_for("item", 1))

    def test_a_tampered_payload_is_refused(self):
        dek = new_data_key()
        context = context_for("item", 1)
        blob = seal(dek, b"private", context=context)
        raw = blob.removeprefix(PAYLOAD_PREFIX)
        forged = PAYLOAD_PREFIX + ("B" if raw[0] != "B" else "C") + raw[1:]
        with self.assertRaises(ValueError):
            unseal_(dek, forged, context=context)

    def test_an_unsealed_value_is_not_mistaken_for_a_sealed_one(self):
        self.assertFalse(is_sealed(None))
        self.assertFalse(is_sealed(""))
        self.assertFalse(is_sealed("https://example.com/x"))
        with self.assertRaises(ValueError):
            unseal_(new_data_key(), "plain text", context=context_for("item", 1))


def unseal_(dek, blob, context):
    from app.modules.vault.crypto import unseal

    return unseal(dek, blob, context=context)
