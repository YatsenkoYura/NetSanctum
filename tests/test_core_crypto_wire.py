"""Core must open what Vault wrote before the KDF moved.

`app/core/crypto` is the same code that used to live in
`app/modules/vault/crypto.py`, relocated and nothing else — but "nothing else" is
a claim, and a sealed vault that stops opening is not a bug report, it is a loss.
So the vectors in `tests/fixtures/crypto_wire_vectors.json` were written by the
implementation *before* the move, with its randomness pinned so every salt, nonce
and ephemeral key is a published constant rather than a value this test would
have to re-derive.

Read them here through `app.core.crypto`, and a single changed byte in the AAD
construction, the binding layout, the MAC or a stored wrapper fails a test instead
of failing somebody's vault on their next unlock.
"""

import json
import unittest
from pathlib import Path

from app.core.crypto import (
    SealedWrite,
    WrappedKey,
    canonical_params,
    context_for,
    decode_b64,
    inbox_binding,
    inbox_pub_fingerprint,
    inbox_pub_mac,
    open_from_inbox,
    open_inbox_key,
    seal,
    unseal,
    unwrap_data_key,
    verify_inbox_pub_mac,
    wrap_aad,
)

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "crypto_wire_vectors.json").read_text())


def unhex(value: str) -> bytes:
    return bytes.fromhex(value)


class WireCompatibilityTests(unittest.TestCase):
    def test_a_wrapper_written_before_the_move_still_unwraps(self):
        stored = VECTORS["wrapped_key"]
        wrapper = WrappedKey(
            salt=stored["salt"],
            wrapped=stored["wrapped"],
            kdf=stored["kdf"],
            wrap_version=stored["wrap_version"],
            t_cost=stored["params"]["t_cost"],
            m_cost=stored["params"]["m_cost"],
            parallelism=stored["params"]["parallelism"],
        )

        dek = unwrap_data_key(wrapper, VECTORS["passphrase"], context=context_for("collection", 5))

        self.assertEqual(VECTORS["dek_hex"], dek.hex())

    def test_a_wrong_passphrase_is_still_refused(self):
        stored = VECTORS["wrapped_key"]
        wrapper = WrappedKey(
            salt=stored["salt"],
            wrapped=stored["wrapped"],
            kdf=stored["kdf"],
            wrap_version=stored["wrap_version"],
            t_cost=stored["params"]["t_cost"],
            m_cost=stored["params"]["m_cost"],
            parallelism=stored["params"]["parallelism"],
        )

        with self.assertRaises(ValueError):
            unwrap_data_key(wrapper, stored["wrong_passphrase"], context=context_for("collection", 5))

    def test_the_aad_is_byte_for_byte_the_same(self):
        stored = VECTORS["wrapped_key"]

        aad = wrap_aad(
            context_for("collection", 5),
            kdf="argon2id",
            params=stored["params"],
            salt=decode_b64(stored["salt"]),
        )

        self.assertEqual(stored["aad_hex"], aad.hex())

    def test_the_pure_functions_are_byte_for_byte_the_same(self):
        pure = VECTORS["pure_functions"]

        self.assertEqual(
            decode_b64(pure["canonical_params"]),
            canonical_params(VECTORS["wrapped_key"]["params"]),
        )
        self.assertEqual(decode_b64(pure["context_for_item_91"]), context_for("item", 91))
        self.assertEqual(
            decode_b64(pure["inbox_binding_5_item_91"]),
            inbox_binding(
                5,
                "item",
                91,
                unhex(VECTORS["pub_mac"]["public_hex"]),
                unhex(VECTORS["pub_mac"]["public_hex"]),
            ),
        )

    def test_a_sealed_payload_written_before_the_move_still_opens(self):
        stored = VECTORS["sealed_payload"]

        self.assertEqual(
            b"a payload in the clear once",
            unseal(unhex(stored["dek_hex"]), stored["value"], context=unhex(stored["context_hex"])),
        )

    def test_a_blind_write_written_before_the_move_still_opens(self):
        stored = VECTORS["blind_write"]
        write = SealedWrite(payload=stored["payload"], wrapped_key=stored["wrapped_key"])

        self.assertEqual(
            b"the payload nobody else may read",
            open_from_inbox(
                write,
                unhex(stored["private_hex"]),
                collection_id=stored["collection_id"],
                kind=stored["kind"],
                row_id=stored["row_id"],
            ),
        )

    def test_the_item_key_alone_is_recoverable(self):
        """A rotation needs the key without the payload, so this path is separate."""
        stored = VECTORS["blind_write"]
        write = SealedWrite(payload=stored["payload"], wrapped_key=stored["wrapped_key"])

        item_key = open_inbox_key(
            write,
            unhex(stored["private_hex"]),
            collection_id=stored["collection_id"],
            kind=stored["kind"],
            row_id=stored["row_id"],
        )

        self.assertEqual(
            b"the payload nobody else may read",
            unseal(item_key, write.payload, context=context_for(stored["kind"], stored["row_id"])),
        )

    def test_the_public_key_mac_is_byte_for_byte_the_same(self):
        stored = VECTORS["pub_mac"]
        kek = unhex(stored["kek_hex"])
        public = unhex(stored["public_hex"])

        self.assertEqual(stored["mac"], inbox_pub_mac(kek, public, stored["collection_id"]))
        self.assertEqual(stored["fingerprint"], inbox_pub_fingerprint(public))
        self.assertTrue(verify_inbox_pub_mac(kek, public, stored["collection_id"], stored["mac"]))

    def test_the_mac_still_refuses_another_collection(self):
        stored = VECTORS["pub_mac"]

        self.assertFalse(
            verify_inbox_pub_mac(unhex(stored["kek_hex"]), unhex(stored["public_hex"]), 6, stored["mac"])
        )

    def test_a_payload_cannot_be_pasted_into_another_row(self):
        """The row binding is the point of `context_for`; a moved blob must fail."""
        stored = VECTORS["sealed_payload"]

        with self.assertRaises(ValueError):
            unseal(unhex(stored["dek_hex"]), stored["value"], context=context_for("item", 92))

    def test_a_write_cannot_be_offered_to_another_row(self):
        stored = VECTORS["blind_write"]
        write = SealedWrite(payload=stored["payload"], wrapped_key=stored["wrapped_key"])

        with self.assertRaises(ValueError):
            open_from_inbox(
                write,
                unhex(stored["private_hex"]),
                collection_id=stored["collection_id"],
                kind=stored["kind"],
                row_id=stored["row_id"] + 1,
            )


class FreshWriteTests(unittest.TestCase):
    """And core still writes what core reads."""

    def test_a_round_trip_through_core(self):
        from app.core.crypto import seal_for_inbox, wrap_data_key

        passphrase = "a passphrase long enough to pass"
        wrapped = wrap_data_key(b"\x01" * 32, passphrase, context=context_for("collection", 1))
        self.assertEqual(
            b"\x01" * 32, unwrap_data_key(wrapped, passphrase, context=context_for("collection", 1))
        )

        from app.core.crypto import generate_inbox_keypair

        private, public = generate_inbox_keypair()
        write = seal_for_inbox(b"fresh", public, collection_id=1, kind="item", row_id=2)
        self.assertEqual(b"fresh", open_from_inbox(write, private, collection_id=1, kind="item", row_id=2))

    def test_seal_and_unseal_round_trip(self):
        dek = b"\x02" * 32
        value = seal(dek, b"payload", context=context_for("item", 1))

        self.assertEqual(b"payload", unseal(dek, value, context=context_for("item", 1)))


if __name__ == "__main__":
    unittest.main()
