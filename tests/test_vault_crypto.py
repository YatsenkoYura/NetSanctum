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
FAST = {"n": 2**8, "r": 8, "p": 1}


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
