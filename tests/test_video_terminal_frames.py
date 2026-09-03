import unittest
from unittest.mock import patch

from app.core.security import get_current_user
from app.core.storage import LocalStorage
from app.modules.video_archiver.router import router
from app.modules.video_archiver.terminal_frames import (
    MAX_PALETTE_PIXELS,
    extract_video_frame,
    quantize_terminal_frame,
    validate_frame_dimensions,
)


class VideoFrameTests(unittest.TestCase):
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
        storage = LocalStorage("/tmp/netsanctum-video-frame-test")
        storage.save_file(b"video", "video.mp4")
        completed = type("Completed", (), {"returncode": 0, "stdout": b"png", "stderr": b""})()

        with patch(
            "app.modules.video_archiver.terminal_frames.subprocess.run", return_value=completed
        ) as run:
            frame = extract_video_frame(storage, "video.mp4", 12.5, 320, 180, "png", "cover")

        self.assertEqual(b"png", frame)
        command = run.call_args.args[0]
        self.assertIn("12.500", command)
        self.assertIn(
            "scale=320:180:force_original_aspect_ratio=increase,crop=320:180",
            command,
        )
        self.assertIn("png", command)
        self.assertEqual(20, run.call_args.kwargs["timeout"])

    def test_frame_endpoint_requires_owner_session_and_exposes_format_query(self):
        route = next(route for route in router.routes if route.path.endswith("/{video_id}/frame"))
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        query_aliases = {parameter.alias for parameter in route.dependant.query_params}

        self.assertIn(get_current_user, dependencies)
        self.assertIn("format", query_aliases)
        self.assertIn("width", query_aliases)
        self.assertIn("height", query_aliases)


if __name__ == "__main__":
    unittest.main()
