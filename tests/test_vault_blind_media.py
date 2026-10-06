"""Blind media writes: a sealed collection's video, stored with no vault key.

The download worker has no passphrase, and the application key is not a place
to park a sealed file — so the worker encrypts the video under a random item
key minted for that one download, and wraps that key under the collection's
inbox public key, exactly like a blind card write. The next unlock opens the
wrap and re-seals the file under the collection's file key.

These tests pin the three halves: the wrap opens only for the passphrase
holder and only for its own row, the blind envelope opens only under its own
key (never the file key, never the application key), and a blind row is
reported as unplayable everywhere — never as a resource nobody can open.
"""

import asyncio
import base64
import io
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.modules.vault.models import VaultCollection, VaultItem


class MintBlindKeyTests(unittest.TestCase):
    def test_a_wrap_opens_for_the_passphrase_holder(self):
        from app.core.crypto.asymmetric import SealedWrite, generate_inbox_keypair, open_inbox_key
        from app.modules.vault.tasks import MEDIA_KEY_KIND, mint_blind_media_key

        private, public = generate_inbox_keypair()
        minted = mint_blind_media_key(5, 91, base64.b64encode(public).decode())
        self.assertIsNotNone(minted)
        blind_key, wrap = minted
        self.assertEqual(32, len(blind_key))
        opened = open_inbox_key(
            SealedWrite(payload="", wrapped_key=wrap),
            private,
            collection_id=5,
            kind=MEDIA_KEY_KIND,
            row_id=91,
        )
        self.assertEqual(blind_key, opened)

    def test_a_wrap_is_bound_to_its_row(self):
        """A wrap lifted from one card cannot be offered to another."""
        from app.core.crypto.asymmetric import SealedWrite, generate_inbox_keypair, open_inbox_key
        from app.modules.vault.tasks import MEDIA_KEY_KIND, mint_blind_media_key

        private, public = generate_inbox_keypair()
        minted = mint_blind_media_key(5, 91, base64.b64encode(public).decode())
        self.assertIsNotNone(minted)
        _, wrap = minted
        with self.assertRaises(ValueError):
            open_inbox_key(
                SealedWrite(payload="", wrapped_key=wrap),
                private,
                collection_id=5,
                kind=MEDIA_KEY_KIND,
                row_id=92,
            )

    def test_no_usable_inbox_key_means_no_blind_write(self):
        from app.modules.vault.tasks import mint_blind_media_key

        self.assertIsNone(mint_blind_media_key(5, 91, None))
        self.assertIsNone(mint_blind_media_key(5, 91, ""))
        self.assertIsNone(mint_blind_media_key(5, 91, "!!!not-base64!!!"))
        self.assertIsNone(mint_blind_media_key(5, 91, base64.b64encode(b"too short").decode()))

    def test_two_downloads_mint_different_keys(self):
        from app.core.crypto.asymmetric import generate_inbox_keypair
        from app.modules.vault.tasks import mint_blind_media_key

        _, public = generate_inbox_keypair()
        encoded = base64.b64encode(public).decode()
        first_minted = mint_blind_media_key(5, 91, encoded)
        second_minted = mint_blind_media_key(5, 91, encoded)
        self.assertIsNotNone(first_minted)
        self.assertIsNotNone(second_minted)
        first, _ = first_minted
        second, _ = second_minted
        self.assertNotEqual(first, second)


class BlindEnvelopeTests(unittest.TestCase):
    def setUp(self):
        from pathlib import Path

        from app.core.storage import LocalStorage

        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(str(Path(self._tmp.name) / "storage"))

    def test_the_blind_key_opens_what_it_sealed(self):
        from app.core.crypto.asymmetric import generate_inbox_keypair
        from app.modules.vault.tasks import mint_blind_media_key

        _, public = generate_inbox_keypair()
        minted = mint_blind_media_key(5, 91, base64.b64encode(public).decode())
        self.assertIsNotNone(minted)
        blind_key, _ = minted
        payload = bytes((i * 7) % 256 for i in range(1024 * 1024 + 13))
        self.storage.save_file_encrypted_seekable(
            io.BytesIO(payload), "vault/5/91/abc.mp4.enc", key=blind_key, length=len(payload)
        )
        self.assertEqual(
            payload,
            b"".join(
                self.storage.read_seekable_range("vault/5/91/abc.mp4.enc", 0, len(payload), key=blind_key)
            ),
        )

    def test_no_other_key_opens_the_blind_file(self):
        """Not the file key, not another blind key, not the application key."""
        from app.core.crypto.asymmetric import generate_inbox_keypair
        from app.core.crypto.kdf import new_data_key
        from app.modules.vault.crypto import derive_file_key
        from app.modules.vault.tasks import mint_blind_media_key

        _, public = generate_inbox_keypair()
        minted = mint_blind_media_key(5, 91, base64.b64encode(public).decode())
        self.assertIsNotNone(minted)
        blind_key, _ = minted
        payload = b"blind bytes " * 1000
        self.storage.save_file_encrypted_seekable(
            io.BytesIO(payload), "vault/5/91/abc.mp4.enc", key=blind_key, length=len(payload)
        )
        from cryptography.exceptions import InvalidTag

        for wrong in (derive_file_key(new_data_key(), 5), new_data_key(), None):
            with self.assertRaises((InvalidTag, ValueError), msg=f"key {wrong!r} must fail"):
                b"".join(
                    self.storage.read_seekable_range("vault/5/91/abc.mp4.enc", 0, len(payload), key=wrong)
                )

    def test_the_finalize_re_seal_is_a_byte_for_byte_handoff(self):
        """Decrypt under the blind key, seal under the file key: the plaintext
        is what the worker downloaded, whatever the keys around it."""
        from app.core.crypto.asymmetric import generate_inbox_keypair
        from app.core.crypto.kdf import new_data_key
        from app.modules.vault.crypto import derive_file_key
        from app.modules.vault.tasks import mint_blind_media_key

        _, public = generate_inbox_keypair()
        minted = mint_blind_media_key(5, 91, base64.b64encode(public).decode())
        self.assertIsNotNone(minted)
        blind_key, _ = minted
        file_key = derive_file_key(new_data_key(), 5)
        payload = bytes((i * 5) % 256 for i in range(300000))
        self.storage.save_file_encrypted_seekable(
            io.BytesIO(payload), "vault/5/91/blind.mp4.enc", key=blind_key, length=len(payload)
        )
        with self.storage.get_file_stream_decrypted("vault/5/91/blind.mp4.enc", key=blind_key) as plain:
            self.storage.save_file_encrypted_seekable(
                plain, "vault/5/91/final.mp4.enc", key=file_key, length=len(payload)
            )
        self.assertEqual(
            payload,
            b"".join(
                self.storage.read_seekable_range("vault/5/91/final.mp4.enc", 0, len(payload), key=file_key)
            ),
        )


class BlindAttachmentTests(unittest.TestCase):
    def make_item(self, **overrides):
        values = {
            "id": 91,
            "entry_type": "bookmark",
            "title": "",
            "content": None,
            "url": None,
            "og_title": None,
            "og_description": None,
            "og_image": None,
            "tags": [],
            "canvas_data": {},
            "category": None,
            "score": None,
            "status": None,
            "media_mime": None,
            "public_title": None,
        }
        values.update(overrides)
        item = VaultItem()
        for field, value in values.items():
            setattr(item, field, value)
        return item

    def test_a_blind_write_reports_blind_not_completed(self):
        from app.modules.vault.tasks import MEDIA_BLIND_STATUS, attach_downloaded_media

        item = self.make_item()
        attach_downloaded_media(
            item,
            sealed=True,
            media_path="vault/5/91/abc.mp4.enc",
            thumbnail_path="vault/5/91/def.jpg.enc",
            size=4096,
            mime="video/mp4",
            title="Секретный выпуск",
            info={"duration": 1800.0},
            blind_wrap="nsi:v2:blind",
        )
        self.assertEqual(MEDIA_BLIND_STATUS, item.media_status)
        self.assertEqual("blind", item.media_status)
        self.assertEqual("nsi:v2:blind", item.media_key_wrap)
        self.assertIsNone(item.media_duration, "the worker still has no key for metadata")

    def test_an_unlocked_write_leaves_no_wrap_behind(self):
        from app.modules.vault.tasks import attach_downloaded_media

        item = self.make_item()
        attach_downloaded_media(
            item,
            sealed=True,
            media_path="vault/5/91/abc.mp4.enc",
            thumbnail_path=None,
            size=4096,
            mime="video/mp4",
            title="Секретный выпуск",
            info={},
        )
        self.assertEqual("completed", item.media_status)
        self.assertIsNone(item.media_key_wrap)


class BlindServingTests(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)

    def test_a_blind_row_ships_no_package_resource(self):
        import asyncio

        from app.modules.vault.sealed_package import (
            sealed_media_url,
            sealed_package_id,
            sealed_package_resources,
        )

        row = VaultCollection(id=5, name="x", is_encrypted=True)
        self.db.add(row)
        item = VaultItem(
            id=21,
            entry_type="bookmark",
            title="",
            tags=[],
            collection_id=5,
            media_path="vault/5/21/abc.mp4.enc",
            media_key_wrap="nsi:v2:blind",
        )
        self.db.add(item)
        self.db.commit()
        resources = asyncio.run(sealed_package_resources(_Async(self.db), row))
        self.assertNotIn(sealed_media_url(21, sealed_package_id(5)), [r["url"] for r in resources])

    def test_re_sealing_a_blind_row_refuses_loudly(self):
        from app.modules.vault.sealed_package import iter_sealed_media

        item = VaultItem(id=21, media_path="vault/5/21/abc.mp4.enc", media_key_wrap="nsi:v2:blind")
        with self.assertRaises(ValueError):
            list(iter_sealed_media(b"\x00" * 32, "vault_sealed_5", item, b"\x01" * 32))


class UnlockQueuesFinalizeTests(unittest.TestCase):
    """An unlock is the one moment a blind file can be re-sealed.

    The worker has no vault key and the owner's tab does, so the finalize runs
    exactly where a session exists. A blind row that outlives its own unlock
    is a card nobody can ever play, which is why the queue lives on this route
    rather than in the dashboard's hands.
    """

    def setUp(self):
        from app.modules.vault import router, sealing
        from tests.test_vault_file_keys import AsyncSessionAdapter, FakeRedis

        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)
        self.session = AsyncSessionAdapter(self.db)
        self.redis = FakeRedis()
        for target, attr in (
            ("redis_client", self.redis),
            ("unlock_backoff_seconds", AsyncMock(return_value=0)),
            ("clear_unlock_failures", AsyncMock(return_value=None)),
        ):
            entered = patch.object(sealing, target, attr)
            entered.start()
            self.addCleanup(entered.stop)
        self.queued = AsyncMock(return_value="task-1")
        entered = patch.object(router, "queue_finalize_blind_media", self.queued)
        entered.start()
        self.addCleanup(entered.stop)

    def _collection(self):
        from tests.test_vault_file_keys import make_sealed_row

        row, _private = make_sealed_row(self.db, collection_id=5)
        return row

    def _unlock(self):
        from unittest.mock import Mock

        from app.modules.vault.router import unlock_collection_route
        from app.modules.vault.schemas import VaultUnlockRequest

        request = Mock()
        request.client = Mock(host="127.0.0.1")
        body = VaultUnlockRequest(passphrase="правильная лошадь, скрепка")
        return asyncio.run(
            unlock_collection_route(5, body, request, db=self.session, user=None, unlock_token="")
        )

    def test_an_unlock_with_a_blind_row_queues_the_finalize(self):
        from app.modules.vault.models import VaultItem

        self._collection()
        self.db.add(
            VaultItem(
                id=91,
                entry_type="bookmark",
                title="",
                tags=[],
                collection_id=5,
                media_path="vault/5/91/blind.mp4.enc",
                media_key_wrap="nsi:v2:blind",
            )
        )
        self.db.commit()

        self._unlock()

        self.queued.assert_awaited_once()
        self.assertEqual(5, self.queued.await_args.args[0])

    def test_an_unlock_with_nothing_blind_queues_nothing(self):
        self._collection()

        self._unlock()

        self.queued.assert_not_awaited()


class _Async:
    """The slice of AsyncSession the sealed producer touches, over sqlite."""

    def __init__(self, session):
        self.session = session

    async def execute(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {})

    async def get(self, model, ident):
        return self.session.get(model, ident)

    async def scalar(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {}).scalar()
