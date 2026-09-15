import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.modules.music.covers import regenerate_playlist_cover as regenerate_music_cover
from app.modules.video_archiver.covers import regenerate_playlist_cover as regenerate_video_cover


class FakeStorage:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.deleted: list[str] = []

    def get_file_stream(self, path: str):
        return io.BytesIO(self.files[path])

    def save_file(self, data: bytes, path: str) -> str:
        self.files[path] = data
        return path

    def delete_file(self, path: str) -> bool:
        self.deleted.append(path)
        return self.files.pop(path, None) is not None


def image_bytes(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (80, 40), color).save(output, format="PNG")
    return output.getvalue()


class PlaylistCoverTests(unittest.TestCase):
    def test_music_cover_is_a_square_webp_and_reuses_available_sources(self):
        storage = FakeStorage({"one.png": image_bytes("red"), "two.png": image_bytes("blue")})
        playlist = SimpleNamespace(id=12)

        with patch("app.modules.music.covers.get_storage", return_value=storage):
            path = regenerate_music_cover(playlist, ["one.png", "two.png"])

        self.assertEqual("music/playlist-covers/12.webp", path)
        with Image.open(io.BytesIO(storage.files["music/playlist-covers/12.webp"])) as cover:
            self.assertEqual("WEBP", cover.format)
            self.assertEqual((480, 480), cover.size)

    def test_video_cover_has_a_seven_by_three_aspect_ratio(self):
        storage = FakeStorage({"one.png": image_bytes("green")})
        playlist = SimpleNamespace(id=34)

        with patch("app.modules.video_archiver.covers.get_storage", return_value=storage):
            path = regenerate_video_cover(playlist, ["one.png"])

        self.assertEqual("video_archiver/playlist-covers/34.webp", path)
        with Image.open(io.BytesIO(storage.files["video_archiver/playlist-covers/34.webp"])) as cover:
            self.assertEqual("WEBP", cover.format)
            self.assertEqual((1120, 480), cover.size)

    def test_empty_cover_sources_remove_the_previous_generated_cover(self):
        storage = FakeStorage({"music/playlist-covers/12.webp": b"old"})

        with patch("app.modules.music.covers.get_storage", return_value=storage):
            path = regenerate_music_cover(SimpleNamespace(id=12), [])

        self.assertIsNone(path)
        self.assertEqual(["music/playlist-covers/12.webp"], storage.deleted)
