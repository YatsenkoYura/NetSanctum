"""Storage folder manager: path safety, listing order and backend limits."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.storage import LocalStorage
from app.modules.system.storage.browse import (
    StoragePathError,
    guess_media_type,
    list_local,
    normalize_folder,
    parent_of,
    read_object,
    resolve_local,
    safe_segment,
)


class StoragePathTests(unittest.TestCase):
    def test_root_is_the_empty_string(self):
        self.assertEqual("", normalize_folder(None))
        self.assertEqual("", normalize_folder(""))
        self.assertEqual("", normalize_folder("/"))

    def test_redundant_separators_collapse(self):
        self.assertEqual("vault/images", normalize_folder("vault//images/"))
        self.assertEqual("vault", normalize_folder("./vault"))

    def test_traversal_is_refused_not_normalized(self):
        for hostile in ("../etc", "vault/../../etc", "a/../.."):
            with self.assertRaises(StoragePathError):
                normalize_folder(hostile)

    def test_absolute_paths_are_refused(self):
        with self.assertRaises(StoragePathError):
            normalize_folder("/etc/passwd")

    def test_backslashes_and_nul_are_refused(self):
        with self.assertRaises(StoragePathError):
            normalize_folder("vault\\..\\..\\etc")
        with self.assertRaises(StoragePathError):
            normalize_folder("vault\x00.png")

    def test_a_similarly_named_sibling_is_outside_the_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "storage"
            sibling = Path(tmp) / "storage-evil"
            root.mkdir()
            sibling.mkdir()
            with patch("app.modules.system.storage.browse.storage_root", return_value=root.resolve()):
                with self.assertRaises(StoragePathError):
                    resolve_local("../storage-evil")

    def test_parent_of_is_the_containing_folder(self):
        # For a file this is the folder holding it; "up" navigation always
        # starts from a folder, where it yields that folder's parent.
        self.assertEqual("vault/images", parent_of("vault/images/1.png"))
        self.assertEqual("vault", parent_of("vault/images"))
        self.assertEqual("", parent_of("vault"))
        self.assertEqual("", parent_of(""))

    def test_segments_cannot_smuggle_separators_or_traversal(self):
        self.assertEqual("file", safe_segment("../.."))
        self.assertEqual("a-b", safe_segment("a/b"))
        self.assertEqual("file", safe_segment("   "))
        self.assertNotIn("/", safe_segment("../../etc/passwd"))
        self.assertNotIn("/", safe_segment("a/b/c"))

    def test_media_type_is_guessed_from_the_suffix(self):
        self.assertEqual("video/mp4", guess_media_type("clip.mp4"))
        self.assertEqual("application/octet-stream", guess_media_type("blob.unknown"))


class StorageListingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "storage"
        (self.root / "vault" / "images").mkdir(parents=True)
        (self.root / "vault" / "notes.txt").write_bytes(b"hello")
        (self.root / "music").mkdir()
        (self.root / "music" / "song.enc").write_bytes(b"\x00" * 32)
        # A loose file at the root, so directory-first ordering is observable.
        (self.root / "zzz.txt").write_bytes(b"x")
        self._patcher = patch(
            "app.modules.system.storage.browse.storage_root", return_value=self.root.resolve()
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_directories_come_before_files_each_alphabetically(self):
        listing = list_local("")
        self.assertEqual(
            [("music", True), ("vault", True), ("zzz.txt", False)],
            [(entry.name, entry.is_dir) for entry in listing.entries],
        )

    def test_a_folder_lists_only_its_own_children(self):
        listing = list_local("vault")
        self.assertEqual(["images", "notes.txt"], [entry.name for entry in listing.entries])

    def test_sizes_are_reported_for_files_and_zero_for_folders(self):
        listing = list_local("vault")
        sizes = {entry.name: entry.size for entry in listing.entries}
        self.assertEqual(5, sizes["notes.txt"])
        self.assertEqual(0, sizes["images"])

    def test_listing_reports_totals_and_windowing(self):
        first = list_local("", limit=2)
        self.assertEqual(2, len(first.entries))
        self.assertEqual(3, first.total)
        self.assertTrue(first.truncated)
        last = list_local("", limit=2, offset=2)
        self.assertFalse(last.truncated)

    def test_a_missing_folder_is_an_error_not_an_empty_listing(self):
        with self.assertRaises(StoragePathError):
            list_local("nope")

    def test_a_file_is_not_a_folder(self):
        with self.assertRaises(StoragePathError):
            list_local("vault/notes.txt")


class StorageDownloadTests(unittest.TestCase):
    """A download hands over the plaintext under a name the OS understands."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.storage = LocalStorage(str(self.root))
        patcher = patch("app.core.storage.get_storage", return_value=self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _body(self, path):
        stream, _size, _media_type, _name = read_object(path)
        try:
            return b"".join(stream if not hasattr(stream, "read") else iter(lambda: stream.read(65536), b""))
        finally:
            closer = getattr(stream, "close", None)
            if callable(closer):
                closer()

    def test_an_encrypted_video_downloads_as_the_video_it_is(self):
        self.storage.save_file_encrypted(b"plain mp4 bytes", "vault/videos/clip.mp4.enc")

        _stream, _size, media_type, name = read_object("vault/videos/clip.mp4.enc")

        self.assertEqual("clip.mp4", name)
        self.assertEqual("video/mp4", media_type)
        self.assertEqual(b"plain mp4 bytes", self._body("vault/videos/clip.mp4.enc"))

    def test_a_seekable_envelope_downloads_under_its_inner_name(self):
        import io

        self.storage.save_file_encrypted_seekable(
            io.BytesIO(b"seekable mp4 bytes"), "vault/videos/big.mp4.enc"
        )

        _stream, size, media_type, name = read_object("vault/videos/big.mp4.enc")

        self.assertEqual(("big.mp4", len(b"seekable mp4 bytes"), "video/mp4"), (name, size, media_type))
        self.assertEqual(b"seekable mp4 bytes", self._body("vault/videos/big.mp4.enc"))

    def test_a_plain_file_keeps_its_own_name(self):
        self.storage.save_file(b"plain", "vault/notes.txt")

        stream, _size, media_type, name = read_object("vault/notes.txt")

        self.assertEqual(("notes.txt", "text/plain; charset=utf-8"), (name, media_type))
        stream.close()

    def test_a_name_that_is_only_an_encryption_suffix_is_left_alone(self):
        self.storage.save_file(b"x", "vault/notes.enc")

        self.assertEqual("notes.enc", read_object("vault/notes.enc")[3])


class StorageBrowserTemplateTests(unittest.TestCase):
    """The folder manager must expose the same controls it can actually apply."""

    ROOT = Path(__file__).resolve().parents[1]

    def _render(self, **overrides):
        from jinja2 import Environment, FileSystemLoader
        from starlette.requests import Request

        environment = Environment(
            loader=FileSystemLoader(
                [
                    str(self.ROOT / "app/modules/system/storage/templates"),
                    str(self.ROOT / "app/core/templates"),
                ]
            ),
            autoescape=True,
        )
        environment.globals["active_modules"] = lambda: []
        environment.globals["url_for"] = lambda *a, **k: "#"
        stats = {
            "is_s3": False,
            "bucket_name": None,
            "used_human": "1 KB",
            "used_percent": 1,
            "total_human": "1 MB",
            "free_human": "999 KB",
            "modules": [],
            "large_files": [],
        }
        context = {
            "request": Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/storage/dashboard",
                    "query_string": b"",
                    "headers": [],
                }
            ),
            "stats": stats,
            "folder": "",
            "breadcrumbs": [("/", "")],
            "parent_path": "",
            "entries": [],
            "listing_total": 0,
            "remote_backend": False,
            "browse_error": None,
            "package_mode": False,
            "is_readonly": False,
            "lang": "ru",
            "user": object(),
            "_": lambda _module, key, **kwargs: key,
        }
        context.update(overrides)
        return environment.get_template("storage_dashboard.html").render(**context)

    def test_online_view_offers_folder_upload_and_entry_actions(self):
        html = self._render()
        self.assertIn('<button id="storage-mkdir-btn"', html)
        self.assertIn('<button id="storage-upload-btn"', html)
        self.assertIn('id="storage-entries"', html)

    def test_package_view_hides_every_mutation_control(self):
        html = self._render(is_readonly=True, package_mode=True)
        self.assertNotIn('<button id="storage-mkdir-btn"', html)
        self.assertNotIn('<button id="storage-upload-btn"', html)
        # Browsing stays available in a package: it is a read-only viewer.
        self.assertIn('id="storage-entries"', html)

    def test_stats_swap_keeps_the_folder_manager_out_of_the_replacement(self):
        html = self._render(only_stats=True)
        self.assertNotIn('id="storage-entries"', html)
        self.assertIn('id="storage-stats"', html)


if __name__ == "__main__":
    unittest.main()
