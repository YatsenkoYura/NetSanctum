import io
import tempfile
import unittest
from pathlib import Path

from app.core.storage import LocalStorage, S3Storage


class ChunkOnlyStream(io.BytesIO):
    def read(self, size: int | None = -1):
        if size is None or size < 0:
            raise AssertionError("stream must not be read into memory at once")
        return super().read(size)


class StorageStreamingTests(unittest.TestCase):
    def test_local_storage_streams_and_atomically_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalStorage(directory)
            storage.save_file(b"old", "videos/item.mp4")

            class FailingStream(ChunkOnlyStream):
                reads = 0

                def read(self, size=-1):
                    self.reads += 1
                    if self.reads > 1:
                        raise OSError("source failed")
                    return super().read(2)

            with self.assertRaises(OSError):
                storage.save_stream(FailingStream(b"replacement"), "videos/item.mp4")

            self.assertEqual(b"old", storage._full_path("videos/item.mp4").read_bytes())
            self.assertEqual([], list(storage._full_path("videos").glob("*.tmp")))

            storage.save_stream(ChunkOnlyStream(b"new"), "videos/item.mp4")
            self.assertEqual(b"new", storage._full_path("videos/item.mp4").read_bytes())

    def test_s3_storage_uses_managed_stream_upload(self):
        calls = []

        class Client:
            def upload_fileobj(self, stream, bucket, key, **kwargs):
                payload = bytearray()
                while chunk := stream.read(3):
                    payload.extend(chunk)
                calls.append((bucket, key, bytes(payload), kwargs["Config"].multipart_threshold))

        storage = S3Storage.__new__(S3Storage)
        storage._client = Client()
        storage._bucket = "archive"

        result = storage.save_stream(ChunkOnlyStream(b"large-video"), "videos/item.mp4")

        self.assertEqual("videos/item.mp4", result)
        self.assertEqual(("archive", "videos/item.mp4", b"large-video", 8 * 1024 * 1024), calls[0])

    def test_save_file_from_path_is_streamed(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
            source = Path(source_dir) / "source.bin"
            source.write_bytes(b"payload")
            storage = LocalStorage(target_dir)

            storage.save_file_from_path(source, "nested/target.bin")

            self.assertEqual(b"payload", storage._full_path("nested/target.bin").read_bytes())


if __name__ == "__main__":
    unittest.main()
