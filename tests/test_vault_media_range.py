"""Video has to be seekable, encrypted or not.

The media endpoint used to answer `Accept-Ranges: none`, so a player could only
scrub after downloading the entire file. These tests pin the Range contract for
all three shapes it has to handle: a plain file, a sealed one, and a malformed
range.
"""

import io
import unittest

from app.modules.vault.router import _iter_media, _parse_byte_range


class ByteRangeParsingTests(unittest.TestCase):
    def test_an_explicit_range_is_inclusive_on_both_ends(self):
        self.assertEqual((100, 199), _parse_byte_range("bytes=100-199", 1000))

    def test_an_open_ended_range_runs_to_the_end(self):
        self.assertEqual((900, 999), _parse_byte_range("bytes=900-", 1000))

    def test_a_suffix_range_takes_the_tail(self):
        self.assertEqual((990, 999), _parse_byte_range("bytes=-10", 1000))

    def test_a_range_longer_than_the_file_is_clamped(self):
        self.assertEqual((0, 999), _parse_byte_range("bytes=0-5000", 1000))

    def test_a_start_past_the_end_is_rejected(self):
        self.assertIsNone(_parse_byte_range("bytes=1000-1100", 1000))

    def test_nonsense_is_rejected_rather_than_guessed(self):
        for header in ("bytes=abc-def", "items=0-10", "bytes=-", "0-10"):
            self.assertIsNone(_parse_byte_range(header, 1000), header)

    def test_a_multi_range_request_is_answered_from_its_first_span(self):
        self.assertEqual((0, 9), _parse_byte_range("bytes=0-9,20-29", 1000))


class MediaIterationTests(unittest.TestCase):
    class FakeStorage:
        def __init__(self, payload):
            self.payload = payload

        def read_seekable_range(self, path, start, length, *, key=None):
            self.last = (path, start, length)
            yield self.payload[start : start + length]

        def looks_encrypted(self, path):
            return False

        def get_file_decrypted(self, path, *, key=None):
            return self.payload

        def get_file_stream(self, path):
            return io.BytesIO(self.payload)

    def test_an_encrypted_range_is_delegated_to_the_seekable_reader(self):
        storage = self.FakeStorage(bytes(range(256)) * 64)
        body = b"".join(_iter_media(storage, "v.mp4.enc", 128, 300, seekable=True))

        self.assertEqual(storage.payload[128:428], body)
        self.assertEqual(("v.mp4.enc", 128, 300), storage.last)

    def test_a_plain_range_seeks_and_streams(self):
        payload = bytes(range(256)) * 64
        storage = self.FakeStorage(payload)
        body = b"".join(_iter_media(storage, "v.mp4", 100, 200, seekable=False))

        self.assertEqual(payload[100:300], body)
        # Nothing was read past the range: a whole-file read would have been 16384.
        self.assertFalse(hasattr(storage, "last"))

    def test_a_plain_range_that_runs_past_the_end_just_stops(self):
        storage = self.FakeStorage(b"short")
        body = b"".join(_iter_media(storage, "v.mp4", 0, 9999, seekable=False))

        self.assertEqual(b"short", body)


if __name__ == "__main__":
    unittest.main()
