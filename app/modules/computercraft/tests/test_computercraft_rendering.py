import unittest
from io import BytesIO
from typing import cast
from unittest.mock import patch

from app.core.security import get_current_user
from app.core.storage import LocalStorage, StorageInterface
from app.modules.computercraft.rendering import (
    MAX_PALETTE_PIXELS,
    extract_media_frame,
    materialize_media,
    quantize_terminal_frame,
    validate_frame_dimensions,
)
from app.modules.computercraft.router import router


class ComputerCraftRenderingTests(unittest.TestCase):
    def test_quantizes_exact_cc_palette_colors(self):
        rgb = bytes((240, 240, 240, 242, 178, 51, 17, 17, 17))
        self.assertEqual(["01f"], quantize_terminal_frame(rgb, 3, 1))

    def test_rejects_incomplete_ffmpeg_frame(self):
        with self.assertRaisesRegex(ValueError, "incomplete frame"):
            quantize_terminal_frame(b"\x00\x00\x00", 2, 1)

    def test_palette_limit_is_based_on_total_cells(self):
        validate_frame_dimensions(200, 100, "cc-palette")
        with self.assertRaisesRegex(ValueError, str(MAX_PALETTE_PIXELS)):
            validate_frame_dimensions(MAX_PALETTE_PIXELS + 1, 1, "cc-palette")

    def test_local_extraction_uses_requested_format_size_and_fit(self):
        storage = LocalStorage("/tmp/netsanctum-computercraft-frame-test")
        storage.save_file(b"video", "video.mp4")
        completed = type("Completed", (), {"returncode": 0, "stdout": b"png", "stderr": b""})()
        with patch("app.modules.computercraft.rendering.subprocess.run", return_value=completed) as run:
            frame = extract_media_frame(storage, "video.mp4", 12.5, 320, 180, "png", "cover")
        self.assertEqual(b"png", frame)
        command = run.call_args.args[0]
        self.assertIn("12.500", command)
        self.assertIn("scale=320:180:force_original_aspect_ratio=increase,crop=320:180", command)

    def test_frame_endpoint_requires_owner_session(self):
        route = next(route for route in router.routes if route.path.endswith("/{item_id}/frame"))
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        query_aliases = {parameter.alias for parameter in route.dependant.query_params}
        self.assertIn(get_current_user, dependencies)
        self.assertLessEqual({"format", "width", "height"}, query_aliases)

    def test_remote_media_is_materialized_as_a_seekable_cache_file(self):
        payload = b"remote-media"

        class RemoteStorage:
            def get_file_size(self, path):
                return len(payload)

            def get_file_stream(self, path):
                return BytesIO(payload)

        cached = materialize_media(cast(StorageInterface, RemoteStorage()), "video/test.mp4")
        self.assertEqual(payload, cached.read_bytes())
        self.assertEqual(0o600, cached.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
