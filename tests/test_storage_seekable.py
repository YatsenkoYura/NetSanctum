"""The chunked envelope must allow seeking without decrypting the whole object.

AES-GCM over one blob cannot be seeked, which is why large media uses a chunked
envelope here. These tests hold it to that: the range has to be correct, and it
has to cost a bounded number of chunks rather than the file.
"""

import io
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.storage import SEEKABLE_CHUNK_SIZE, LocalStorage


class _CountingStream(io.RawIOBase):
    """Counts the bytes a caller actually pulls, so a seek's cost is measurable."""

    def __init__(self, handle, test):
        self._handle = handle
        self._test = test

    def seek(self, offset, whence=0):
        return self._handle.seek(offset, whence)

    def read(self, size=-1):
        data = self._handle.read(size)
        self._test.read_bytes += len(data)
        return data

    def readable(self):
        return True

    def close(self):
        self._handle.close()


class SeekableEnvelopeTests(unittest.TestCase):
    read_bytes = 0

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(str(self.root))

    def _write(self, name: str, payload: bytes) -> str:
        self.storage.save_file_encrypted_seekable(io.BytesIO(payload), name)
        return name

    def test_a_round_trip_returns_the_original_bytes(self):
        payload = os.urandom(SEEKABLE_CHUNK_SIZE * 2 + 1234)
        path = self._write("video.mp4.enc", payload)

        self.assertEqual(payload, self.storage.get_file_decrypted(path))

    def test_the_stored_object_is_not_the_plaintext(self):
        payload = b"A" * 5000
        path = self._write("plain.bin.enc", payload)

        self.assertNotIn(payload, (self.root / path).read_bytes())

    def test_the_plaintext_size_is_reported_without_decrypting(self):
        payload = os.urandom(SEEKABLE_CHUNK_SIZE * 3 + 77)
        path = self._write("sized.bin.enc", payload)

        self.assertEqual(len(payload), self.storage.get_encrypted_plaintext_size(path))
        self.assertEqual(len(payload), self.storage.get_seekable_plaintext_size(path))

    def test_every_offset_of_a_range_matches_the_plaintext(self):
        payload = os.urandom(SEEKABLE_CHUNK_SIZE + 5000)
        path = self._write("seek.bin.enc", payload)

        for start in (
            0,
            1,
            4096,
            SEEKABLE_CHUNK_SIZE - 1,
            SEEKABLE_CHUNK_SIZE,
            SEEKABLE_CHUNK_SIZE + 10,
            len(payload) - 50,
        ):
            length = 2000
            chunk = b"".join(self.storage.read_seekable_range(path, start, length))
            self.assertEqual(payload[start : start + length], chunk, f"range at {start}")

    def test_a_range_at_the_very_end_of_the_file_is_short(self):
        payload = os.urandom(SEEKABLE_CHUNK_SIZE + 100)
        path = self._write("tail.bin.enc", payload)

        chunk = b"".join(self.storage.read_seekable_range(path, len(payload) - 10, 5000))

        self.assertEqual(payload[-10:], chunk)

    def test_a_seek_reads_only_the_chunks_it_covers(self):
        """The whole point: a seek must not cost the size of the file."""
        chunk_count = 8
        payload = os.urandom(SEEKABLE_CHUNK_SIZE * chunk_count)
        path = self._write("big.bin.enc", payload)
        self.read_bytes = 0
        real_get_file_stream = self.storage.get_file_stream

        def counting(path_arg):
            return _CountingStream(real_get_file_stream(path_arg), self)

        self.storage.get_file_stream = counting
        try:
            target = SEEKABLE_CHUNK_SIZE * 5 + 10
            chunk = b"".join(self.storage.read_seekable_range(path, target, 100))
        finally:
            self.storage.get_file_stream = real_get_file_stream

        self.assertEqual(payload[target : target + 100], chunk)
        # Two chunks at most: the one the range starts in and the one it ends in.
        self.assertLessEqual(
            self.read_bytes,
            2 * (SEEKABLE_CHUNK_SIZE + 16) + 4096,
            f"seek read {self.read_bytes} bytes of a {len(payload)} byte file",
        )

    def test_a_tampered_chunk_fails_to_decrypt(self):
        payload = os.urandom(SEEKABLE_CHUNK_SIZE + 100)
        path = self._write("tampered.bin.enc", payload)
        stored = self.root / path
        raw = bytearray(stored.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        stored.write_bytes(bytes(raw))

        with self.assertRaises(ValueError):
            list(self.storage.read_seekable_range(path, 0, len(payload)))

    def test_a_chunk_moved_to_another_file_is_rejected(self):
        """The path is bound into every chunk, so a file cannot be swapped out."""
        payload = os.urandom(SEEKABLE_CHUNK_SIZE)
        path = self._write("bound.bin.enc", payload)
        self.storage.save_file_from_path(self.root / path, "elsewhere.bin.enc")

        with self.assertRaises(ValueError):
            list(self.storage.read_seekable_range("elsewhere.bin.enc", 0, 10))

    def test_a_plain_file_is_not_mistaken_for_the_seekable_envelope(self):
        self.storage.save_file(b"just bytes", "plain.txt")

        self.assertFalse(self.storage.is_seekable_encrypted("plain.txt"))
        self.assertTrue(self.storage.is_seekable_encrypted(self._write("enc.bin.enc", b"x" * 10)))

    def test_an_empty_object_round_trips(self):
        path = self._write("empty.bin.enc", b"")

        self.assertEqual(b"", b"".join(self.storage.read_seekable_range(path, 0, 10)))


class LegacyFormatTests(unittest.TestCase):
    """The old single-blob format must keep working; three modules depend on it."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(str(self.root))

    def test_the_existing_encrypted_format_still_decrypts(self):
        payload = b"legacy content" * 100
        path = self.storage.save_file_encrypted(payload, "legacy.bin.enc")

        self.assertEqual(payload, self.storage.get_file_decrypted(path))
        self.assertFalse(self.storage.is_seekable_encrypted(path))

    def test_the_existing_format_still_reports_its_plaintext_size(self):
        payload = b"x" * 4096
        path = self.storage.save_file_encrypted(payload, "legacy-size.bin.enc")

        self.assertEqual(len(payload), self.storage.get_encrypted_plaintext_size(path))

    def test_the_size_helper_is_only_meaningful_for_encrypted_objects(self):
        """Callers gate on the `.enc` suffix; for a plain file it assumes an envelope."""
        self.storage.save_file(b"y" * 1234, "plain.bin")

        # Unchanged pre-existing behaviour: the helper subtracts envelope overhead
        # and so under-reports a file that was never encrypted.
        self.assertEqual(1234 - 28, self.storage.get_encrypted_plaintext_size("plain.bin"))
        self.assertEqual(1234, self.storage.get_file_size("plain.bin"))


if __name__ == "__main__":
    unittest.main()
