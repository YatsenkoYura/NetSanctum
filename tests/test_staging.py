"""Plaintext must not sit on a disk longer than it has to.

Every write path has a moment where the bytes are not encrypted yet: a video the
worker just downloaded, a spooled chunk envelope, a staged screenshot. Those
moments used to live in `tempfile`'s default directory, which is `/tmp` and a real
disk on most deployments — which makes it the most sensitive thing on the machine,
in the clear, for as long as the write takes.

These tests pin the three properties that make staging a decision rather than a
default: the directory is `0700`, it can be required to be memory-backed, and
leftovers from a crash are collected without ever touching a write in progress.
"""

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.core.staging import (
    StagingError,
    collect_orphans,
    is_memory_backed,
    require_staging,
    staging_dir,
    staging_tempfile,
    staging_workdir,
)


class StagingDirectoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "staging"
        self.settings = patch("app.core.config.get_settings")
        self.addCleanup(self.settings.stop)
        self.patcher = patch("app.core.staging.get_settings")
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _settings(self, **overrides):
        from app.core.config import Settings

        defaults = {
            "STAGING_DIR": str(self.root),
            "STAGING_REQUIRE_TMPFS": False,
            "STAGING_ORPHAN_MINUTES": 60,
        }
        defaults.update(overrides)
        return Settings(**defaults)

    def test_the_directory_is_created_private(self):
        with patch("app.core.staging.get_settings", return_value=self._settings()):
            directory = staging_dir()

        self.assertTrue(directory.is_dir())
        self.assertEqual(0o700, directory.stat().st_mode & 0o777)

    def test_an_existing_directory_is_tightened(self):
        """A leftover from an earlier run must not keep a wider mode."""
        self.root.mkdir(parents=True)
        self.root.chmod(0o755)

        with patch("app.core.staging.get_settings", return_value=self._settings()):
            directory = staging_dir()

        self.assertEqual(0o700, directory.stat().st_mode & 0o777)

    def test_a_workdir_and_a_tempfile_are_private_too(self):
        with patch("app.core.staging.get_settings", return_value=self._settings()):
            workdir = staging_workdir("job_")
            handle = staging_tempfile("chunk_")

        self.assertEqual(0o700, workdir.stat().st_mode & 0o777)
        self.assertEqual(0o600, handle.stat().st_mode & 0o777)

    def test_requiring_a_tmpfs_fails_loudly_on_a_disk(self):
        """Falling back quietly is how the file ends up somewhere worse."""
        with patch("app.core.staging.get_settings", return_value=self._settings(STAGING_REQUIRE_TMPFS=True)):
            with patch("app.core.staging.is_memory_backed", return_value=False):
                with self.assertRaises(StagingError) as caught:
                    require_staging()

        self.assertIn("tmpfs", str(caught.exception))

    def test_requiring_a_tmpfs_passes_when_it_is_one(self):
        with (
            patch("app.core.staging.get_settings", return_value=self._settings(STAGING_REQUIRE_TMPFS=True)),
            patch("app.core.staging.is_memory_backed", return_value=True),
        ):
            self.assertTrue(require_staging().is_dir())

    def test_orphans_are_collected_and_fresh_files_are_not(self):
        with patch("app.core.staging.get_settings", return_value=self._settings()):
            old = staging_tempfile("old_")
            fresh = staging_tempfile("fresh_")
            old_dir = staging_workdir("old_dir_")
            ancient = time.time() - 7200
            os.utime(old, (ancient, ancient))
            os.utime(old_dir, (ancient, ancient))

            removed = collect_orphans(min_age_minutes=60)

        self.assertEqual(2, removed)
        self.assertFalse(old.exists())
        self.assertFalse(old_dir.exists())
        self.assertTrue(fresh.exists(), "a write in progress must never be collected")

    def test_the_filesystem_check_answers_from_a_mount_table(self):
        """Which filesystem a path is on, read from the kernel.

        The table is faked rather than the host: whether `/tmp` happens to be a
        tmpfs differs between this checkout and the application image, and a
        test that depended on it would be asserting the environment instead of
        the code. (It is not a tmpfs in the image, which is exactly why the
        staging directory exists and why `STAGING_REQUIRE_TMPFS` refuses.)
        """
        mounts = "/dev/root / ext4 rw 0 0\ntmpfs /tmp tmpfs rw,nosuid 0 0\ntmpfs /run tmpfs rw,nosuid 0 0\n"
        with patch.object(Path, "read_text", return_value=mounts):
            self.assertTrue(is_memory_backed(Path("/tmp")))
            self.assertTrue(is_memory_backed(Path("/tmp/netsanctum-staging")))
            self.assertFalse(is_memory_backed(Path("/var/lib/thing")))

    def test_a_similar_prefix_is_not_mistaken_for_a_mount(self):
        """`/tmpfoo` is not on `/tmp`, and getting that wrong would pass a disk."""
        mounts = "tmpfs /tmp tmpfs rw 0 0\n/dev/root / ext4 rw 0 0\n"
        with patch.object(Path, "read_text", return_value=mounts):
            self.assertFalse(is_memory_backed(Path("/tmpfoo/staging")))

    def test_no_procfs_means_cannot_tell_rather_than_yes(self):
        with patch.object(Path, "read_text", side_effect=OSError):
            self.assertFalse(is_memory_backed(Path("/tmp")))


class StagingIsUsedByTheWritersTests(unittest.TestCase):
    """The writers, not just the module: this is the part that actually matters."""

    def test_a_seekable_write_with_a_known_length_writes_no_plaintext_file(self):
        import io

        from app.core.storage import LocalStorage

        with TemporaryDirectory() as tmp:
            storage = LocalStorage(str(Path(tmp) / "store"))
            storage.save_file_encrypted_seekable(io.BytesIO(b"payload " * 100), "a.bin.enc", length=800)

            self.assertEqual(b"payload " * 100, storage.get_file_decrypted("a.bin.enc"))
            self.assertEqual(800, storage.get_seekable_plaintext_size("a.bin.enc"))

    def test_an_unknown_length_still_works_and_leaves_nothing_behind(self):
        import io

        from app.core.staging import staging_dir
        from app.core.storage import LocalStorage

        class UnknownLength(io.RawIOBase):
            """A stream that refuses to say how long it is."""

            def __init__(self, payload):
                self._buffer = io.BytesIO(payload)

            def read(self, size=-1):
                return self._buffer.read(size)

            def readable(self):
                return True

        with TemporaryDirectory() as tmp:
            storage = LocalStorage(str(Path(tmp) / "store"))
            storage.save_file_encrypted_seekable(UnknownLength(b"x" * 5000), "b.bin.enc")

            self.assertEqual(b"x" * 5000, storage.get_file_decrypted("b.bin.enc"))
            leftovers = list(staging_dir().glob("seekenc_*"))
            self.assertEqual([], leftovers, f"the spool was left behind: {leftovers}")

    def test_an_image_is_stored_without_a_staged_copy(self):
        from app.core.staging import staging_dir
        from app.modules.vault.images import store_image_bytes

        with TemporaryDirectory() as tmp:
            with (
                patch(
                    "app.core.storage.get_storage",
                    return_value=__import__("app.core.storage", fromlist=["LocalStorage"]).LocalStorage(
                        str(Path(tmp) / "store")
                    ),
                ),
                patch(
                    "app.modules.vault.images.get_storage",
                    return_value=__import__("app.core.storage", fromlist=["LocalStorage"]).LocalStorage(
                        str(Path(tmp) / "store")
                    ),
                ),
                patch("app.modules.vault.paths.storage_root", return_value=Path(tmp)),
                patch("app.modules.vault.images.storage_root", return_value=Path(tmp)),
            ):
                path = store_image_bytes(b"\x89PNG" + b"x" * 500, "image/png", 5, sealed=True)

            self.assertTrue(path.endswith(".png.enc"))
            leftovers = list(staging_dir().glob("*"))
            self.assertEqual([], leftovers, f"staging was used for an in-memory payload: {leftovers}")


if __name__ == "__main__":
    unittest.main()
