import unittest
from pathlib import Path

from app.core.packages_router import make_hybrid_manifest, make_package_manifest
from app.modules.video_archiver.module import MODULE as VIDEO_MODULE

ROOT = Path(__file__).resolve().parents[1]


class PackageContractTests(unittest.TestCase):
    def test_manifest_contains_explicit_module_metadata(self):
        manifest = make_package_manifest(
            module_id="music",
            package_id="song_1",
            package_title="Song: Example",
            root_url="/music/dashboard?package_id=song_1",
            resources=[{"url": "/music/audio/1", "type": "binary"}],
        )

        self.assertEqual(1, manifest["schema_version"])
        self.assertEqual("music", manifest["module"]["id"])
        self.assertEqual("/music/dashboard", manifest["module"]["root_url"])

    def test_hybrid_manifest_keeps_binary_and_adds_container(self):
        manifest = make_package_manifest(
            module_id="music",
            package_id="song_1",
            package_title="Song: Example",
            root_url="/music/dashboard?package_id=song_1",
            resources=[
                {"url": "/music/dashboard", "type": "html"},
                {"url": "/music/audio/1", "type": "binary"},
            ],
        )

        hybrid = make_hybrid_manifest("song_1", manifest)

        self.assertEqual(
            [
                {"url": "/music/audio/1", "type": "binary"},
                {"url": "/api/packages/song_1/nsp", "type": "container"},
            ],
            hybrid["resources"],
        )

    def test_desktop_bridge_uses_manifest_url_not_manifest_payload(self):
        base = (ROOT / "app/core/templates/base.html").read_text()

        self.assertIn("__NETSANCTUM_DESKTOP__?.requestDownload", base)
        self.assertIn("requestDownload(manifestUrl)", base)

    def test_video_module_declares_package_provider(self):
        self.assertEqual(("video_playlist_", "video_"), VIDEO_MODULE.package_prefixes)
        self.assertEqual(
            "app.modules.video_archiver.capabilities:resolve_package_resources",
            VIDEO_MODULE.package_resolver,
        )


if __name__ == "__main__":
    unittest.main()
