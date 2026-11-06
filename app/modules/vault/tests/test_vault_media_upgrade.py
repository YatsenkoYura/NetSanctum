"""The media envelope migration: safe to interrupt, safe to run twice.

v1 stored a plaintext length in a header nobody authenticated. Fixing that means
rewriting files measured in gigabytes, so the properties that matter are not
cryptographic: does an interrupted run leave anything unreadable, does a second
pass do nothing, and does a file that cannot be read stop being retried forever.

The unit of work is one file, in this order: write the new object, verify it
reads back, point the row at it, commit, delete the old file. Every crash point
leaves a file that still opens.
"""

import asyncio
import io
import pathlib
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.storage import SEEKABLE_CHUNK_SIZE, LocalStorage
from app.modules.vault.media_upgrade import (
    MEDIA_FILE_COLUMNS,
    STATE_DONE,
    STATE_FAILED,
    retry_failed,
    upgrade_media_batch,
    upgrade_summary,
)
from app.modules.vault.models import VaultItem, VaultMediaUpgrade


def write_v1_object(storage, path: str, payload: bytes) -> str:
    """Write a genuine v1 chunked object, exactly the way the old writer did.

    Built by hand rather than by calling the current writer, so "v1 still reads"
    and "the upgrade moves it" are tested against the real old format instead of
    against whatever the code happens to produce today.
    """
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from app.core.storage import SEEKABLE_FILE_NONCE_SIZE, SEEKABLE_MAGIC

    file_nonce = os.urandom(SEEKABLE_FILE_NONCE_SIZE)
    aesgcm = AESGCM(storage._get_encryption_key())
    aad = SEEKABLE_MAGIC + b"\x00" + file_nonce + path.encode()
    stored = SEEKABLE_MAGIC + file_nonce + SEEKABLE_CHUNK_SIZE.to_bytes(4, "big")
    stored += len(payload).to_bytes(8, "big")
    chunks = max(1, (len(payload) + SEEKABLE_CHUNK_SIZE - 1) // SEEKABLE_CHUNK_SIZE)
    for index in range(chunks):
        piece = payload[index * SEEKABLE_CHUNK_SIZE : (index + 1) * SEEKABLE_CHUNK_SIZE]
        stored += aesgcm.encrypt(file_nonce + index.to_bytes(4, "big"), piece, aad + index.to_bytes(4, "big"))
    storage.save_file(stored, path)
    return path


class StorageUpgradeTests(unittest.TestCase):
    """The storage-level primitive, without the database around it."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(self._tmp.name)

    def _v1_object(self, path: str, payload: bytes) -> str:
        return write_v1_object(self.storage, path, payload)

    def test_the_version_is_read_from_the_header_not_the_name(self):
        path = self._v1_object("vault/videos/1-old.mp4.enc", b"x" * 100)
        self.storage.save_file_encrypted_seekable(io.BytesIO(b"y" * 100), "vault/videos/2-new.mp4.enc")

        self.assertEqual(1, self.storage.seekable_envelope_version(path))
        self.assertEqual(2, self.storage.seekable_envelope_version("vault/videos/2-new.mp4.enc"))
        self.assertEqual(0, self.storage.seekable_envelope_version(self._plain()))

    def _plain(self) -> str:
        self.storage.save_file(b"just bytes", "vault/videos/plain.mp4")
        return "vault/videos/plain.mp4"

    def test_an_upgrade_keeps_the_bytes_and_moves_the_name(self):
        payload = b"video bytes " * 500
        path = self._v1_object("vault/videos/1-old.mp4.enc", payload)

        target = self.storage.upgrade_seekable_envelope(path)

        self.assertEqual("vault/videos/1-old.mp4.v2.enc", target)
        self.assertEqual(2, self.storage.seekable_envelope_version(target))
        self.assertEqual(payload, self.storage.get_file_decrypted(target))
        # The old file is the caller's to delete, not this function's.
        self.assertTrue(self.storage.file_exists(path))

    def test_a_file_of_the_wrong_version_is_refused(self):
        self._v1_object("vault/videos/1-old.mp4.enc", b"x" * 10)
        upgraded = self.storage.upgrade_seekable_envelope("vault/videos/1-old.mp4.enc")

        # A v2 object and a plain object are both somebody else's business.
        with self.assertRaises(ValueError):
            self.storage.upgrade_seekable_envelope(upgraded)
        self.storage.save_file(b"plain video", "vault/videos/plain.mp4")
        with self.assertRaises(ValueError):
            self.storage.upgrade_seekable_envelope("vault/videos/plain.mp4")

    def test_an_unreadable_object_is_refused_and_left_alone(self):
        path = self._v1_object("vault/videos/1-old.mp4.enc", b"x" * 100)
        stored = pathlib.Path(self._tmp.name, path)
        raw = bytearray(stored.read_bytes())
        raw[-1] ^= 0xFF
        stored.write_bytes(bytes(raw))

        with self.assertRaises(ValueError):
            self.storage.upgrade_seekable_envelope(path)

        self.assertTrue(self.storage.file_exists(path))
        self.assertFalse(self.storage.file_exists(self.storage.upgraded_envelope_path(path)))


class AsyncSessionAdapter:
    """The slice of AsyncSession the upgrade driver touches."""

    def __init__(self, session):
        self.session = session

    def add(self, instance):
        self.session.add(instance)

    async def execute(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {})

    async def get(self, model, identity):
        return self.session.get(model, identity)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()


class MediaUpgradeBatchTests(unittest.TestCase):
    """The driver: one row, one file, verifiable interruption points."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(self._tmp.name)
        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.db = AsyncSessionAdapter(self.session)
        self.addCleanup(self.engine.dispose)

        import app.core.storage as storage_module
        import app.modules.vault.media_upgrade as module

        for target in (module, storage_module):
            patcher = mock.patch.object(target, "get_storage", return_value=self.storage)
            patcher.start()
            self.addCleanup(patcher.stop)

    def v1_file(self, path: str, payload: bytes = b"video " * 400) -> str:
        return write_v1_object(self.storage, path, payload)

    def add_item(self, item_id=1, column="media_path", path="vault/videos/1-old.mp4.enc"):
        item = VaultItem(
            id=item_id, entry_type="bookmark", title="", tags=[], node_type="video", **{column: path}
        )
        self.db.add(item)
        self.db.commit()
        return item

    def test_a_v1_file_moves_and_the_row_follows(self):
        payload = b"video " * 400
        self.v1_file("vault/videos/1-old.mp4.enc", payload)
        self.add_item()

        async def run():
            return await upgrade_media_batch(self.db)

        result = asyncio.run(run())

        self.assertEqual(1, result.upgraded)
        self.session.expire_all()
        stored = self.session.get(VaultItem, 1)
        assert stored is not None and stored.media_path is not None
        self.assertEqual("vault/videos/1-old.mp4.v2.enc", stored.media_path)
        self.assertEqual(payload, self.storage.get_file_decrypted(stored.media_path))
        # The old file goes last, and only once the row points at the new one.
        self.assertFalse(self.storage.file_exists("vault/videos/1-old.mp4.enc"))

    def test_a_second_pass_does_nothing(self):
        self.v1_file("vault/videos/1-old.mp4.enc")
        self.add_item()

        async def run():
            await upgrade_media_batch(self.db)
            return await upgrade_media_batch(self.db)

        result = asyncio.run(run())

        self.assertEqual(0, result.upgraded)
        self.assertEqual(1, result.already_current)

    def test_a_plain_object_is_left_alone(self):
        self.storage.save_file(b"plain video", "vault/videos/2.mp4")
        self.add_item(item_id=2, path="vault/videos/2.mp4")

        async def run():
            return await upgrade_media_batch(self.db)

        result = asyncio.run(run())

        self.assertEqual(0, result.upgraded)
        self.assertEqual(1, result.already_current)
        self.assertTrue(self.storage.file_exists("vault/videos/2.mp4"))

    def test_every_file_column_is_covered(self):
        self.v1_file("vault/videos/3.mp4.enc")
        self.v1_file("vault/thumbnails/4.jpg.enc", b"poster " * 100)
        self.v1_file("vault/images/5.png.enc", b"\x89PNG" + b"pic " * 100)
        item = VaultItem(
            id=3,
            entry_type="bookmark",
            title="",
            tags=[],
            media_path="vault/videos/3.mp4.enc",
            media_thumbnail_path="vault/thumbnails/4.jpg.enc",
            image_path="vault/images/5.png.enc",
        )
        self.db.add(item)
        self.db.commit()

        async def run():
            return await upgrade_media_batch(self.db)

        result = asyncio.run(run())

        self.assertEqual(3, result.upgraded)
        self.session.expire_all()
        stored = self.session.get(VaultItem, 3)
        assert stored is not None
        for column in MEDIA_FILE_COLUMNS:
            path = getattr(stored, column)
            self.assertTrue(path.endswith(".v2.enc"), f"{column} was not moved: {path}")
            self.assertEqual(2, self.storage.seekable_envelope_version(path))

    def test_an_unreadable_file_is_recorded_and_not_retried(self):
        path = self.v1_file("vault/videos/6-old.mp4.enc")
        raw = bytearray(pathlib.Path(self._tmp.name, path).read_bytes())
        raw[-1] ^= 0xFF
        pathlib.Path(self._tmp.name, path).write_bytes(bytes(raw))
        self.add_item(item_id=6, path=path)

        async def run():
            first = await upgrade_media_batch(self.db)
            second = await upgrade_media_batch(self.db)
            return first, second

        first, second = asyncio.run(run())

        self.assertEqual(1, first.failed)
        self.assertEqual(1, second.skipped_failed, "a file that failed must not be retried every pass")
        ledger = self.session.get(VaultMediaUpgrade, path)
        assert ledger is not None
        self.assertEqual(STATE_FAILED, ledger.state)
        self.assertEqual(2, ledger.attempts)
        self.assertTrue(ledger.last_error)
        # The row still points at the old file, which is where the data is.
        item = self.session.get(VaultItem, 6)
        assert item is not None and item.media_path is not None
        self.assertEqual(path, item.media_path)

    def test_retry_failed_clears_the_record(self):
        path = self.v1_file("vault/videos/7-old.mp4.enc")
        raw = bytearray(pathlib.Path(self._tmp.name, path).read_bytes())
        raw[-1] ^= 0xFF
        pathlib.Path(self._tmp.name, path).write_bytes(bytes(raw))
        self.add_item(item_id=7, path=path)

        async def run():
            await upgrade_media_batch(self.db)
            cleared = await retry_failed(self.db)
            await upgrade_media_batch(self.db)
            return cleared

        cleared = asyncio.run(run())

        self.assertEqual(1, cleared)

    def test_a_missing_file_is_reported_not_silently_skipped(self):
        self.add_item(item_id=8, path="vault/videos/gone.mp4.enc")

        async def run():
            return await upgrade_media_batch(self.db)

        result = asyncio.run(run())

        self.assertEqual(1, result.missing)
        summary = asyncio.run(upgrade_summary(self.db))
        self.assertEqual(1, summary[STATE_FAILED])

    def test_the_ledger_records_what_it_did(self):
        self.v1_file("vault/videos/9-old.mp4.enc")
        self.add_item(item_id=9, path="vault/videos/9-old.mp4.enc")

        async def run():
            await upgrade_media_batch(self.db)
            return await upgrade_summary(self.db)

        summary = asyncio.run(run())

        self.assertEqual(1, summary[STATE_DONE])


if __name__ == "__main__":
    unittest.main()
