"""The chunked envelope must allow seeking without decrypting the whole object.

AES-GCM over one blob cannot be seeked, which is why large media uses a chunked
envelope here. These tests hold it to that: the range has to be correct, and it
has to cost a bounded number of chunks rather than the file.
"""

import hashlib
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


LEGACY_FIXTURE_PAYLOAD = b"netsanctum-legacy-aad-fixture-" + bytes(range(256)) * 800


class LegacyWriterCompatibilityTests(unittest.TestCase):
    """Bytes written by the old writer, opened by the current reader.

    The forward-compatibility test above writes with today's code, so on its own it
    only shows that today's code agrees with itself. This one is the test that
    matters: the fixture was produced by the writer as it stood *before* the fix —
    the one that put the module constant into every chunk's associated data rather
    than the object's own chunk size. It is sealed under an explicit public test
    key, because sealing it under the deployment key would put a secret from `.env`
    into a committed file.

    It spans two chunks, since chunk boundaries are what the associated data names.
    """

    KEY = hashlib.sha256(b"netsanctum-test-file-key").digest()
    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "seekable_v2_legacy_aad.bin"
    # The fixture was written at this chunk size, which is deliberately not the
    # current default: a reader that ignored the header and used its own constant
    # would still pass against a fixture that agreed with it.
    FIXTURE_CHUNK_SIZE = 64 * 1024

    def test_the_old_writers_object_still_opens(self):
        payload = LEGACY_FIXTURE_PAYLOAD
        # The digest sits next to the fixture so that regenerating one without the
        # other fails here rather than silently testing nothing.
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            (self.FIXTURE.with_suffix(".sha256")).read_text().strip(),
            "the fixture and this test disagree about the plaintext",
        )

        stored = self.FIXTURE.read_bytes()
        offset = len(SEEKABLE_MAGIC_V2) + SEEKABLE_FILE_NONCE_SIZE
        self.assertEqual(self.FIXTURE_CHUNK_SIZE, int.from_bytes(stored[offset : offset + 4], "big"))
        self.assertNotEqual(
            SEEKABLE_CHUNK_SIZE,
            self.FIXTURE_CHUNK_SIZE,
            "the fixture must disagree with today's constant, or it proves nothing",
        )
        self.assertGreater(len(payload), self.FIXTURE_CHUNK_SIZE, "the fixture must span more than one chunk")

        target = Path(self._tmp.name) / "vault" / "legacy-v2.mp4.enc"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(stored)
        storage = LocalStorage(self._tmp.name)

        self.assertEqual(payload, storage.get_file_decrypted("vault/legacy-v2.mp4.enc", key=self.KEY))
        # And by range, which is how a player reads it: the bytes either side of the
        # first chunk boundary have to come back in the right order.
        edge = self.FIXTURE_CHUNK_SIZE * 2 - 10
        middle = storage.read_seekable_range("vault/legacy-v2.mp4.enc", edge, 32, key=self.KEY)
        self.assertEqual(payload[edge : edge + 32], b"".join(middle))

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)


class IntegrityVerificationTests(unittest.TestCase):
    """A check that only works when there is nothing wrong with it.

    The point of walking every chunk is to fail on damage. A verifier that reports
    a healthy file as broken gets ignored, and then the corrupt chunk three hours
    into a video is found by somebody watching that hour.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(self._tmp.name)
        self.payload = b"integrity-" + bytes(range(256)) * 9000  # ~2.3 MB, three chunks
        self.path = self.storage.save_file_encrypted_seekable(
            io.BytesIO(self.payload), "vault/video.mp4.enc", length=len(self.payload)
        )

    def _stored(self) -> Path:
        return Path(self._tmp.name) / self.path

    def _flip(self, offset: int) -> None:
        raw = bytearray(self._stored().read_bytes())
        raw[offset] ^= 0x01
        self._stored().write_bytes(bytes(raw))

    def test_a_healthy_object_verifies_and_reports_its_size(self):
        report = self.storage.verify_encrypted_object(self.path)

        self.assertEqual("seekable-v2", report["envelope"])
        self.assertEqual(len(self.payload), report["plaintext_bytes"])

    def test_damage_in_the_header_is_caught(self):
        self._flip(20)  # inside the plaintext length

        with self.assertRaises(ValueError):
            self.storage.verify_encrypted_object(self.path)

    def test_damage_in_the_first_chunk_is_caught(self):
        self._flip(SEEKABLE_HEADER_V2_SIZE + 40)

        with self.assertRaises(ValueError):
            self.storage.verify_encrypted_object(self.path)

    def test_damage_in_the_last_chunk_is_caught(self):
        self._flip(self._stored().stat().st_size - 8)

        with self.assertRaises(ValueError):
            self.storage.verify_encrypted_object(self.path)

    def test_a_truncated_object_is_caught_rather_than_read_short(self):
        raw = self._stored().read_bytes()
        self._stored().write_bytes(raw[: len(raw) // 2])

        with self.assertRaises(ValueError):
            self.storage.verify_encrypted_object(self.path)

    def test_a_plain_file_is_reported_as_plaintext_not_as_damage(self):
        """Two real videos in this deployment are plain MP4s.

        Calling them corrupt would be crying wolf over files that play perfectly,
        so the header decides and the answer says `plaintext`.
        """
        self.storage.save_file(b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00" + b"x" * 512, "vault/plain.mp4")

        report = self.storage.verify_encrypted_object("vault/plain.mp4")

        self.assertEqual("plaintext", report["envelope"])

    def test_a_single_blob_object_verifies_under_its_own_key(self):
        """The key is used, not the application key behind it.

        `get_file_decrypted(path, key=…)` used to drop the key for this envelope and
        try the application key with the legacy rotation behind it — the one mixing
        the chunked path is careful never to do.
        """
        key = hashlib.sha256(b"per-collection").digest()
        path = self.storage.save_file_encrypted(b"single blob payload", "vault/one.jpg.enc", key=key)

        report = self.storage.verify_encrypted_object(path, key=key)
        self.assertEqual("single-blob", report["envelope"])
        self.assertEqual(19, report["plaintext_bytes"])

        with self.assertRaises(ValueError):
            self.storage.verify_encrypted_object(path, key=b"\x00" * 32)


class ReencryptionTests(unittest.TestCase):
    """A move is a re-encryption, and the copy is checked before the original goes.

    Every chunk authenticates the object's own path, so the bytes cannot follow the
    name. These pin the two properties that makes safe: the target opens under its
    own path afterwards, and a target that does not read back leaves the source
    untouched rather than eating the file.
    """

    KEY = hashlib.sha256(b"collection-key").digest()

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(self._tmp.name)
        self.payload = b"move-me-" + bytes(range(256)) * 7000

    def test_a_chunked_object_opens_under_its_new_path(self):
        source = self.storage.save_file_encrypted_seekable(
            io.BytesIO(self.payload), "uploads/clip.mp4.enc", key=self.KEY, length=len(self.payload)
        )

        report = self.storage.reencrypt_to_path(
            source, "vault/library/clip.mp4.enc", source_key=self.KEY, target_key=self.KEY
        )

        self.assertEqual("seekable-v2", report["envelope"])
        self.assertEqual(
            self.payload, self.storage.get_file_decrypted("vault/library/clip.mp4.enc", key=self.KEY)
        )
        # The original is untouched: deleting it is the caller's decision, once
        # whatever pointed at the old path points at the new one.
        self.assertEqual(self.payload, self.storage.get_file_decrypted(source, key=self.KEY))
        # And this is why a copy is not a move — the same bytes at a third name do
        # not open, because every chunk authenticated the name it was written under.
        with self.storage.get_file_stream("vault/library/clip.mp4.enc") as raw:
            self.storage.save_stream(raw, "vault/library/clip-copy.mp4.enc")
        with self.assertRaises(ValueError):
            self.storage.get_file_decrypted("vault/library/clip-copy.mp4.enc", key=self.KEY)

    def test_the_target_can_be_sealed_under_a_different_key(self):
        """The case the storage layer cannot decide on its own.

        Moving between two private modules means moving between two file keys. The
        bytes are re-sealed for the destination, so the result opens there and not
        under the key it was written with.
        """
        other = hashlib.sha256(b"another-collection").digest()
        source = self.storage.save_file_encrypted_seekable(
            io.BytesIO(self.payload), "vault/library/a.mp4.enc", key=self.KEY, length=len(self.payload)
        )

        self.storage.reencrypt_to_path(
            source, "vault/library/b.mp4.enc", source_key=self.KEY, target_key=other
        )

        self.assertEqual(self.payload, self.storage.get_file_decrypted("vault/library/b.mp4.enc", key=other))
        with self.assertRaises(ValueError):
            self.storage.get_file_decrypted("vault/library/b.mp4.enc", key=self.KEY)

    def test_a_single_blob_object_moves_too(self):
        source = self.storage.save_file_encrypted(b"small payload", "uploads/note.txt.enc", key=self.KEY)

        report = self.storage.reencrypt_to_path(
            source, "vault/library/note.txt.enc", source_key=self.KEY, target_key=self.KEY
        )

        self.assertEqual("single-blob", report["envelope"])
        self.assertEqual(
            b"small payload", self.storage.get_file_decrypted("vault/library/note.txt.enc", key=self.KEY)
        )

    def test_a_plain_file_is_copied_and_says_so(self):
        self.storage.save_file(b"just bytes", "uploads/plain.txt")

        report = self.storage.reencrypt_to_path("uploads/plain.txt", "uploads/renamed.txt")

        self.assertEqual("plaintext", report["envelope"])
        # Read raw: a plain copy is not an envelope, so the decrypting reader is the
        # wrong tool for checking it.
        with self.storage.get_file_stream("uploads/renamed.txt") as raw:
            self.assertEqual(b"just bytes", raw.read())

    def test_a_source_that_does_not_open_leaves_nothing_behind(self):
        path = "vault/library/broken.mp4.enc"
        self.storage.save_file_encrypted(b"x" * 1000, path, key=self.KEY)
        raw = bytearray((Path(self._tmp.name) / path).read_bytes())
        raw[-1] ^= 0x01
        (Path(self._tmp.name) / path).write_bytes(bytes(raw))

        with self.assertRaises(ValueError):
            self.storage.reencrypt_to_path(
                path, "vault/library/copy.mp4.enc", source_key=self.KEY, target_key=self.KEY
            )

        self.assertFalse((Path(self._tmp.name) / "vault/library/copy.mp4.enc").exists())
        self.assertTrue((Path(self._tmp.name) / path).exists(), "the original must survive a failed move")


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
