"""The chunked envelope must allow seeking without decrypting the whole object.

AES-GCM over one blob cannot be seeked, which is why large media uses a chunked
envelope here. These tests hold it to that: the range has to be correct, and it
has to cost a bounded number of chunks rather than the file.
"""

import io
import os
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.storage import (
    ENCRYPTED_FILE_MAGIC,
    SEEKABLE_CHUNK_SIZE,
    SEEKABLE_FILE_NONCE_SIZE,
    SEEKABLE_HEADER_V2_SIZE,
    SEEKABLE_MAGIC,
    SEEKABLE_MAGIC_V2,
    LocalStorage,
    _s3_is_missing,
    _S3RangeStream,
)


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


class SeekableV2Tests(unittest.TestCase):
    """The current envelope: the header's sizes are authenticated, not trusted.

    v1 bound each chunk to the path and the file nonce, and the plaintext length
    in the header was a promise nobody checked. v2 binds the length, the chunk
    size and the chunk count into every chunk, so editing the header voids the
    chunks. New writes use v2; v1 objects keep reading as they always did.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(str(self.root))

    def _write(self, name: str, payload: bytes, *, key=None) -> str:
        self.storage.save_file_encrypted_seekable(io.BytesIO(payload), name, key=key)
        return name

    def _stored(self, path: str) -> bytearray:
        return bytearray((self.root / path).read_bytes())

    def test_new_writes_carry_the_v2_magic(self):
        path = self._write("v2.bin.enc", b"versioned" * 100)

        self.assertTrue(self._stored(path).startswith(SEEKABLE_MAGIC_V2))
        self.assertTrue(self.storage.is_seekable_encrypted(path))
        self.assertEqual(len(b"versioned" * 100), self.storage.get_seekable_plaintext_size(path))

    def test_a_v2_round_trip_matches_at_every_offset(self):
        payload = os.urandom(SEEKABLE_CHUNK_SIZE * 2 + 1234)
        path = self._write("v2seek.bin.enc", payload)

        self.assertEqual(payload, self.storage.get_file_decrypted(path))
        for start in (0, 4096, SEEKABLE_CHUNK_SIZE - 1, SEEKABLE_CHUNK_SIZE, len(payload) - 50):
            chunk = b"".join(self.storage.read_seekable_range(path, start, 2000))
            self.assertEqual(payload[start : start + 2000], chunk, f"range at {start}")

    def test_a_rewritten_length_voids_the_chunks(self):
        """The v1 hole, closed: the length is in the AAD now, so editing it fails."""
        payload = os.urandom(5000)
        path = self._write("v2len.bin.enc", payload)
        stored = self.root / path
        raw = self._stored(path)
        # Claim twice the plaintext without touching a chunk.
        raw[21:29] = (len(payload) * 2).to_bytes(8, "big")
        stored.write_bytes(bytes(raw))

        with self.assertRaises(ValueError):
            list(self.storage.read_seekable_range(path, 0, 100))

    def test_a_rewritten_chunk_count_is_refused_before_any_chunk(self):
        payload = os.urandom(5000)
        path = self._write("v2count.bin.enc", payload)
        stored = self.root / path
        raw = self._stored(path)
        raw[29:33] = (999).to_bytes(4, "big")
        stored.write_bytes(bytes(raw))

        with self.assertRaises(ValueError):
            list(self.storage.read_seekable_range(path, 0, 100))

    def test_a_truncated_object_is_damage_not_a_short_file(self):
        payload = os.urandom(SEEKABLE_CHUNK_SIZE + 100)
        path = self._write("v2cut.bin.enc", payload)
        stored = self.root / path
        raw = bytes(self._stored(path))
        stored.write_bytes(raw[: SEEKABLE_HEADER_V2_SIZE + 100])

        with self.assertRaises(ValueError):
            list(self.storage.read_seekable_range(path, 0, len(payload)))

    def test_a_read_past_the_end_still_ends(self):
        payload = os.urandom(5000)
        path = self._write("v2tail.bin.enc", payload)

        chunk = b"".join(self.storage.read_seekable_range(path, len(payload) - 10, 5000))

        self.assertEqual(payload[-10:], chunk)

    def test_an_empty_object_still_round_trips(self):
        path = self._write("v2empty.bin.enc", b"")

        self.assertEqual(b"", b"".join(self.storage.read_seekable_range(path, 0, 10)))
        self.assertEqual(0, self.storage.get_seekable_plaintext_size(path))

    def test_a_chunk_moved_to_another_file_is_rejected(self):
        payload = os.urandom(5000)
        path = self._write("v2bound.bin.enc", payload)
        self.storage.save_file_from_path(self.root / path, "v2elsewhere.bin.enc")

        with self.assertRaises(ValueError):
            list(self.storage.read_seekable_range("v2elsewhere.bin.enc", 0, 10))

    def test_an_explicit_key_opens_only_with_that_key(self):
        """A per-collection file key stands alone: no legacy rotation applies."""
        file_key = os.urandom(32)
        payload = os.urandom(5000)
        path = self._write("v2fk.bin.enc", payload, key=file_key)

        self.assertEqual(payload, self.storage.get_file_decrypted(path, key=file_key))
        with self.assertRaises(ValueError):
            self.storage.get_file_decrypted(path)
        with self.assertRaises(ValueError):
            self.storage.get_file_decrypted(path, key=os.urandom(32))

    def test_a_v1_object_still_reads_after_the_upgrade(self):
        """The writer moved on; the reader did not leave v1 behind."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        from app.core.storage import SEEKABLE_FILE_NONCE_SIZE

        payload = os.urandom(5000)
        file_nonce = os.urandom(SEEKABLE_FILE_NONCE_SIZE)
        aesgcm = AESGCM(self.storage._get_encryption_key())
        aad = SEEKABLE_MAGIC + b"\x00" + file_nonce + b"v1old.bin.enc"
        stored = (
            SEEKABLE_MAGIC
            + file_nonce
            + SEEKABLE_CHUNK_SIZE.to_bytes(4, "big")
            + len(payload).to_bytes(8, "big")
            + aesgcm.encrypt(file_nonce + (0).to_bytes(4, "big"), payload, aad + (0).to_bytes(4, "big"))
        )
        (self.root / "v1old.bin.enc").write_bytes(stored)

        self.assertEqual(payload, self.storage.get_file_decrypted("v1old.bin.enc"))
        self.assertEqual(len(payload), self.storage.get_seekable_plaintext_size("v1old.bin.enc"))


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


class ChunkSizeForwardCompatibilityTests(unittest.TestCase):
    """The chunk size a v2 object authenticates is its own, not this build's.

    The associated data used to carry `SEEKABLE_CHUNK_SIZE` — the module constant —
    rather than the size the object was written with. Nothing exploitable came of
    it, because the header's size steers the reads and a rewritten header still
    fails the tag. But it made two claims false. The header's chunk size was not
    authenticated, which is what v2 exists to do. And the next person to tune the
    constant would have invalidated every stored object at once: its authenticator
    would name a size it was never sealed with. These two tests are that second
    failure, written before it happens.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(self._tmp.name)
        self.payload = bytes(range(256)) * 40_000  # ~10 MB, so several chunks

    def test_an_object_written_at_one_chunk_size_survives_another_build(self):
        path = self.storage.save_file_encrypted_seekable(
            io.BytesIO(self.payload), "vault/movie.mp4.enc", length=len(self.payload)
        )
        stored = (Path(self._tmp.name) / path).read_bytes()
        offset = len(SEEKABLE_MAGIC_V2) + SEEKABLE_FILE_NONCE_SIZE
        self.assertEqual(SEEKABLE_CHUNK_SIZE, int.from_bytes(stored[offset : offset + 4], "big"))

        # A later build with a different constant. Nothing else about the reader
        # changes, so the object must still open byte for byte.
        with unittest.mock.patch("app.core.storage.SEEKABLE_CHUNK_SIZE", 4 * 1024 * 1024):
            self.assertEqual(self.payload, self.storage.get_file_decrypted(path))

    def test_a_forged_chunk_size_in_the_header_is_refused(self):
        path = self.storage.save_file_encrypted_seekable(
            io.BytesIO(self.payload), "vault/movie.mp4.enc", length=len(self.payload)
        )
        target = Path(self._tmp.name) / path
        stored = bytearray(target.read_bytes())
        offset = len(SEEKABLE_MAGIC_V2) + SEEKABLE_FILE_NONCE_SIZE
        stored[offset : offset + 4] = (SEEKABLE_CHUNK_SIZE * 2).to_bytes(4, "big")
        target.write_bytes(bytes(stored))

        with self.assertRaises(ValueError):
            self.storage.get_file_decrypted(path)


class S3SeekAndErrorTests(unittest.TestCase):
    """A remote object has to be seekable, and 'denied' is not 'absent'.

    The chunked reader seeks to the chunk covering the byte it wants, so a
    forward-only `StreamingBody` broke range reads on S3 outright. And both
    `file_exists` and `delete_file` used to answer from `except Exception`, which
    turns an AccessDenied into "the file is gone" — a page that claims a delete
    that never happened.
    """

    class _Client:
        def __init__(self, body: bytes):
            self.body = body
            self.ranges: list[tuple[int, int]] = []

        def head_object(self, Bucket, Key):  # noqa: N803 - boto3's keyword names
            return {"ContentLength": len(self.body)}

        def get_object(self, Bucket, Key, Range=None):  # noqa: N803
            if Range:
                start, _, end = Range.partition("=")[2].partition("-")
                self.ranges.append((int(start), int(end)))
                return {"Body": io.BytesIO(self.body[int(start) : int(end) + 1])}
            return {"Body": io.BytesIO(self.body)}

    def test_the_stream_seeks_backwards_and_forwards(self):
        payload = bytes(range(256)) * 4096
        client = self._Client(payload)
        stream = _S3RangeStream(client, "bucket", "vault/movie.mp4.enc")

        # Mid-object, then back to the start: the chunk reader jumps, it does not
        # walk. A forward-only pipe cannot do either of these.
        stream.seek(1000)
        self.assertEqual(payload[1000:1010], stream.read(10))
        stream.seek(0)
        self.assertEqual(payload[:10], stream.read(10))
        stream.seek(-16, io.SEEK_END)
        self.assertEqual(payload[-16:], stream.read(16))
        self.assertEqual(b"", stream.read(10))
        stream.seek(5000)
        self.assertEqual(payload[5000:5016], stream.read(16))
        self.assertEqual(payload[5016:5032], stream.read(16))

    def test_a_missing_object_says_so_and_a_denied_one_raises(self):
        missing = type("E", (Exception,), {"response": {"Error": {"Code": "NoSuchKey"}}})()
        denied = type("E", (Exception,), {"response": {"Error": {"Code": "AccessDenied"}}})()
        other = Exception("network")

        self.assertTrue(_s3_is_missing(missing))
        self.assertFalse(_s3_is_missing(denied))
        self.assertFalse(_s3_is_missing(other))

        stream = _S3RangeStream(
            type("C", (), {"head_object": staticmethod(lambda **k: (_ for _ in ()).throw(denied))})(),
            "b",
            "k",
        )
        with self.assertRaises(Exception) as caught:
            stream.read(1)
        self.assertIs(caught.exception, denied)

    def test_the_current_magic_is_not_mistaken_for_a_legacy_object(self):
        """The migration's first test, and the reason it read a prefix at all."""
        self.assertNotEqual(ENCRYPTED_FILE_MAGIC, SEEKABLE_MAGIC_V2[: len(ENCRYPTED_FILE_MAGIC)])
        self.assertNotEqual(ENCRYPTED_FILE_MAGIC, SEEKABLE_MAGIC[: len(ENCRYPTED_FILE_MAGIC)])


if __name__ == "__main__":
    unittest.main()
