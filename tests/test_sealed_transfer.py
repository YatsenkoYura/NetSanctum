"""Sealed offline packages must open byte-for-byte what was sealed — and nothing else.

The vectors in `tests/fixtures/sealed_transfer_vectors.json` were written once with
pinned randomness, and are read here through `app.core.crypto`. The Rust desktop
client reads the same file: field names are the cross-implementation contract, so
a rename here breaks the other side too.
"""

import io
import json
import unittest
from pathlib import Path

from app.core.crypto import transfer as t
from app.core.crypto.kdf import UnlockError

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "sealed_transfer_vectors.json").read_text())


def unhex(value: str) -> bytes:
    return bytes.fromhex(value)


class WireCompatibilityTests(unittest.TestCase):
    def test_the_kek_matches_the_pinned_derivation(self):
        self.assertEqual(
            VECTORS["kek_hex"],
            t.derive_transfer_kek(
                VECTORS["passphrase"], unhex(VECTORS["salt_hex"]), **VECTORS["kdf_params"]
            ).hex(),
        )

    def test_the_wrap_aad_is_byte_for_byte_the_same(self):
        self.assertEqual(
            VECTORS["wrap_aad_hex"],
            t.transfer_wrap_aad(
                VECTORS["package_id"], VECTORS["kdf_params"], unhex(VECTORS["salt_hex"])
            ).hex(),
        )

    def test_a_wrapped_package_key_opens_with_its_passphrase(self):
        dek = t.unwrap_package_dek(VECTORS["sealing"], VECTORS["passphrase"], VECTORS["package_id"])
        self.assertEqual(VECTORS["dek_hex"], dek.hex())

    def test_a_wrong_passphrase_is_refused_without_distinction(self):
        with self.assertRaises(UnlockError):
            t.unwrap_package_dek(VECTORS["sealing"], VECTORS["wrong_passphrase"], VECTORS["package_id"])

    def test_a_wrapper_cannot_move_to_another_package(self):
        with self.assertRaises(UnlockError):
            t.unwrap_package_dek(VECTORS["sealing"], VECTORS["passphrase"], "music_all")

    def test_resource_keys_are_bound_to_their_url(self):
        dek = unhex(VECTORS["dek_hex"])
        self.assertEqual(
            VECTORS["items_key_hex"],
            t.transfer_resource_key(dek, VECTORS["package_id"], VECTORS["items_url"]).hex(),
        )
        self.assertEqual(
            VECTORS["media_key_hex"],
            t.transfer_resource_key(dek, VECTORS["package_id"], VECTORS["media_url"]).hex(),
        )
        self.assertNotEqual(VECTORS["items_key_hex"], VECTORS["media_key_hex"])

    def test_a_small_resource_opens(self):
        blob = unhex(VECTORS["small"]["blob_hex"])
        key = unhex(VECTORS["items_key_hex"])
        self.assertEqual(
            unhex(VECTORS["small"]["plaintext_hex"]),
            t.open_small_resource(key, VECTORS["items_url"], blob),
        )

    def test_a_small_resource_is_bound_to_its_url(self):
        blob = unhex(VECTORS["small"]["blob_hex"])
        with self.assertRaises(UnlockError):
            t.open_small_resource(unhex(VECTORS["items_key_hex"]), VECTORS["media_url"], blob)

    def test_the_chunked_header_parses(self):
        stored = VECTORS["chunked"]
        file_nonce, chunk_size, plaintext_len, chunk_count = t.parse_chunked_header(
            unhex(stored["header_hex"])
        )
        self.assertEqual(stored["file_nonce_hex"], file_nonce.hex())
        self.assertEqual(stored["chunk_size"], chunk_size)
        self.assertEqual(stored["plaintext_len"], plaintext_len)
        self.assertEqual(stored["chunk_count"], chunk_count)

    def test_resealing_reproduces_the_pinned_chunks(self):
        """Determinism is the wire proof: same inputs, same bytes, both sides."""
        stored = VECTORS["chunked"]
        media_plain = bytes((i * 7) % 256 for i in range(stored["plaintext_len"]))
        resealed = t.seal_chunked_resource(
            unhex(VECTORS["media_key_hex"]),
            VECTORS["media_url"],
            media_plain,
            file_nonce=unhex(stored["file_nonce_hex"]),
        )
        self.assertEqual(stored["header_hex"], resealed[: t.TRANSFER_HEADER_SIZE].hex())
        for probe in stored["probes"]:
            index = probe["index"]
            start = t.TRANSFER_HEADER_SIZE + index * (stored["chunk_size"] + 16)
            self.assertEqual(probe["stored_hex"], resealed[start : start + 64].hex(), f"chunk {index}")
            self.assertEqual(probe["plain_hex"], media_plain[index * stored["chunk_size"] :][:64].hex())


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.key = unhex(VECTORS["media_key_hex"])
        self.url = VECTORS["media_url"]
        media_plain = bytes((i * 7) % 256 for i in range(100_000))
        self.blob = t.seal_chunked_resource(self.key, self.url, media_plain)

    def test_a_flipped_ciphertext_byte_is_refused(self):
        tampered = bytearray(self.blob)
        tampered[-20] ^= 0x01
        with self.assertRaises(UnlockError):
            list(t.open_chunked_range(self.key, self.url, bytes(tampered), 0, 100_000))

    def test_a_truncated_envelope_is_refused(self):
        with self.assertRaises(UnlockError):
            list(t.open_chunked_range(self.key, self.url, self.blob[:-10], 0, 100_000))

    def test_a_rewritten_length_is_refused_not_served_short(self):
        header = bytearray(self.blob[: t.TRANSFER_HEADER_SIZE])
        header[16:24] = (10).to_bytes(8, "big")
        with self.assertRaises(UnlockError):
            list(t.open_chunked_range(self.key, self.url, bytes(header) + self.blob[32:], 0, 100))

    def test_a_range_past_the_end_is_refused(self):
        with self.assertRaises(UnlockError):
            list(t.open_chunked_range(self.key, self.url, self.blob, 200_000, 10))

    def test_an_absurd_kdf_cost_is_refused_before_allocating(self):
        with self.assertRaises(UnlockError):
            t.derive_transfer_kek("pass", b"\x00" * 16, m_cost=2**30, t_cost=3, parallelism=1)

    def test_an_unknown_package_version_is_refused(self):
        sealing = dict(VECTORS["sealing"], version=99)
        with self.assertRaises(UnlockError):
            t.unwrap_package_dek(sealing, VECTORS["passphrase"], VECTORS["package_id"])

    def test_a_malformed_descriptor_is_refused(self):
        with self.assertRaises(UnlockError):
            t.unwrap_package_dek({"version": 1}, VECTORS["passphrase"], VECTORS["package_id"])


class FreshRoundTripTests(unittest.TestCase):
    def test_small_resources_round_trip(self):
        dek = t.new_transfer_dek()
        key = t.transfer_resource_key(dek, "pkg", "/res")
        for plaintext in (b"", b"x", bytes(range(256)) * 100):
            self.assertEqual(
                plaintext, t.open_small_resource(key, "/res", t.seal_small_resource(key, "/res", plaintext))
            )

    def test_chunked_edges_round_trip(self):
        dek = t.new_transfer_dek()
        key = t.transfer_resource_key(dek, "pkg", "/media")
        for size in (0, 1, t.TRANSFER_CHUNK_SIZE, t.TRANSFER_CHUNK_SIZE + 1, 2 * t.TRANSFER_CHUNK_SIZE + 7):
            plaintext = bytes((i * 13) % 256 for i in range(size))
            blob = t.seal_chunked_resource(key, "/media", plaintext)
            opened = b"".join(t.open_chunked_range(key, "/media", blob, 0, size or 1))
            self.assertEqual(plaintext, opened, f"size {size}")

    def test_stream_writer_and_stream_reader_agree(self):
        dek = t.new_transfer_dek()
        key = t.transfer_resource_key(dek, "pkg", "/media")
        plaintext = bytes((i * 11) % 256 for i in range(2 * t.TRANSFER_CHUNK_SIZE + 999))
        sink = io.BytesIO()
        t.seal_chunked_stream(key, "/media", io.BytesIO(plaintext), len(plaintext), sink)
        blob = sink.getvalue()
        self.assertEqual(
            plaintext[12345 : 12345 + 5000],
            b"".join(t.read_sealed_range(key, "/media", io.BytesIO(blob), 12345, 5000)),
        )
        self.assertEqual(
            plaintext, b"".join(t.read_sealed_range(key, "/media", io.BytesIO(blob), 0, len(plaintext)))
        )

    def test_a_short_stream_voids_the_envelope(self):
        dek = t.new_transfer_dek()
        key = t.transfer_resource_key(dek, "pkg", "/media")
        sink = io.BytesIO()
        with self.assertRaises(UnlockError):
            t.seal_chunked_stream(key, "/media", io.BytesIO(b"short"), 1_000_000, sink)

    def test_nfc_passphrases_agree_across_spellings(self):
        composed, decomposed = "café vault пароль", "café vault пароль"
        self.assertNotEqual(composed, decomposed)
        salt = b"\x01" * 16
        self.assertEqual(
            t.derive_transfer_kek(composed, salt).hex(),
            t.derive_transfer_kek(decomposed, salt).hex(),
        )


if __name__ == "__main__":
    unittest.main()
