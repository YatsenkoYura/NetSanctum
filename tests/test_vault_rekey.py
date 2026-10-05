"""Rotating a vault's inbox key, as a deliberate separate operation.

A leaked key is a reason to rotate; it is not a reason to rotate quietly on the
next unlock. So this is its own operation, it needs the vault unlocked (the old
private key is what everything is re-wrapped from), and it reports exactly what
it could not do — above all the files, because the file key is derived from the
inbox private key and a new one means they have to be re-encrypted by someone
holding a vault key.

The property that matters most: no payload is decrypted and no payload is
rewritten. Only the small wrapper around each item key changes.
"""

import asyncio
import base64
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.modules.vault import sealing
from app.modules.vault.crypto import (
    SealedWrite,
    generate_inbox_keypair,
    inbox_pub_fingerprint,
    inbox_write_version,
    open_from_inbox,
)
from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.sealing import (
    VaultRekeyError,
    context_for,
    file_key_for,
    open_item,
    rekey_collection,
    seal_item,
    store_wrapper,
    unlock_collection,
    wrap_data_key,
)

PASSPHRASE = "правильная лошадь, скрепка"
FAST = {"t_cost": 1, "m_cost": 8, "parallelism": 1}


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def getdel(self, key):
        return self.store.pop(key, None)

    async def mget(self, keys):
        return [self.store.get(key) for key in keys]

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def setex(self, key, seconds, value):
        self.store[key] = value

    async def incr(self, key):
        self.store[key] = int(self.store.get(key) or 0) + 1
        return self.store[key]

    async def expire(self, key, seconds):
        return True

    async def delete(self, key):
        self.store.pop(key, None)


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    def add(self, instance):
        self.session.add(instance)

    async def execute(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {})

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def flush(self):
        self.session.flush()

    async def refresh(self, instance):
        self.session.refresh(instance)

    async def get(self, model, identity):
        return self.session.get(model, identity)


class RekeyTests(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)
        self.db = AsyncSessionAdapter(self.session)
        self.redis = FakeRedis()
        entered = patch.object(sealing, "redis_client", self.redis)
        entered.start()
        self.addCleanup(entered.stop)

    def _collection(self, collection_id=5) -> VaultCollection:
        private, public = generate_inbox_keypair()
        row = VaultCollection(id=collection_id, name="Приватное", is_encrypted=True, public_name="Н")
        row.inbox_public_key = base64.b64encode(public).decode()
        store_wrapper(
            row,
            wrap_data_key(private, PASSPHRASE, context=context_for("collection", collection_id), **FAST),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def _sealed_item(self, collection_id=5, item_id=7, title="Личное название") -> VaultItem:
        row = self.session.get(VaultCollection, collection_id)
        assert row is not None and row.inbox_public_key is not None
        item = VaultItem(
            id=item_id,
            entry_type="bookmark",
            title=title,
            content="Личный текст",
            tags=["secret"],
            collection_id=collection_id,
            image_path=f"vault/images/{item_id}.png.enc",
            media_path=f"vault/videos/{item_id}.mp4.enc",
        )
        self.session.add(item)
        self.session.commit()
        seal_item(item, base64.b64decode(row.inbox_public_key))
        self.session.commit()
        return item

    def _rekey(self, row, token="tab"):
        return asyncio.run(rekey_collection(self.db, row, PASSPHRASE, unlock_token=token))

    def test_a_locked_vault_is_refused(self):
        row = self._collection()

        with self.assertRaises(VaultRekeyError):
            asyncio.run(rekey_collection(self.db, row, PASSPHRASE, unlock_token=""))

    def test_the_published_key_changes(self):
        row = self._collection()
        self._sealed_item()
        old_public = row.inbox_public_key
        token = asyncio.run(unlock_collection(row, PASSPHRASE, "tab", session=self.db))

        result = self._rekey(row, token)

        self.assertNotEqual(old_public, row.inbox_public_key)
        # Blind writes now have to be sealed to the new key, and the report says
        # which one it is so the extension can be checked against it.
        self.assertEqual(
            inbox_pub_fingerprint(base64.b64decode(row.inbox_public_key)),
            result["inbox_fingerprint"],
        )

    def test_every_card_opens_under_the_new_key(self):
        row = self._collection()
        item = self._sealed_item()
        token = asyncio.run(unlock_collection(row, PASSPHRASE, "tab", session=self.db))
        old_payload = item.sealed_payload

        result = self._rekey(row, token)

        self.assertEqual(1, result["rewrapped_items"])
        # Unlocking again proves the new pair is the one the passphrase wraps.
        fresh = asyncio.run(unlock_collection(row, PASSPHRASE, "tab-2", session=self.db))
        assert fresh
        private = asyncio.run(file_key_for(row, fresh))
        assert private is not None
        reloaded = self.session.get(VaultItem, 7)
        assert reloaded is not None
        from app.modules.vault.sealing import data_key_for

        key = asyncio.run(data_key_for(self.session.get(VaultCollection, 5), fresh))
        assert key is not None
        open_item(key, reloaded)
        self.assertEqual("Личное название", reloaded.title)
        # The payload itself was never rewritten.
        self.assertEqual(old_payload, reloaded.sealed_payload)

    def test_the_old_private_key_no_longer_opens_anything(self):
        row = self._collection()
        item = self._sealed_item()
        token = asyncio.run(unlock_collection(row, PASSPHRASE, "tab", session=self.db))
        old_private = asyncio.run(sealing.data_key_for(row, token))
        assert old_private is not None

        self._rekey(row, token)

        with self.assertRaises(ValueError):
            open_from_inbox(
                SealedWrite(payload=item.sealed_payload, wrapped_key=item.wrapped_key),
                old_private,
                collection_id=5,
                kind="item",
                row_id=7,
            )

    def test_the_mac_is_recomputed_for_the_new_key(self):
        """Leaving the old MAC would make the next unlock refuse this vault."""
        from app.modules.vault.crypto import verify_inbox_pub_mac

        row = self._collection()
        token = asyncio.run(unlock_collection(row, PASSPHRASE, "tab", session=self.db))
        old_mac = row.inbox_pub_mac

        self._rekey(row, token)

        self.assertNotEqual(old_mac, row.inbox_pub_mac)
        wrapped = sealing.wrapper_for(row)
        kek, _salt = sealing.kek_for_wrapper(wrapped, PASSPHRASE)
        self.assertTrue(
            verify_inbox_pub_mac(kek, base64.b64decode(row.inbox_public_key), row.id, row.inbox_pub_mac)
        )

    def test_the_file_key_changes_so_the_report_says_how_many_files_wait(self):
        row = self._collection()
        self._sealed_item(item_id=7)
        self._sealed_item(item_id=8)
        token = asyncio.run(unlock_collection(row, PASSPHRASE, "tab", session=self.db))
        old_file_key = asyncio.run(file_key_for(row, token))
        assert old_file_key is not None

        result = self._rekey(row, token)

        self.assertEqual(2, result["files_awaiting_reencryption"])
        fresh = asyncio.run(unlock_collection(row, PASSPHRASE, "tab-2", session=self.db))
        assert fresh
        self.assertNotEqual(old_file_key, asyncio.run(file_key_for(row, fresh)))

    def test_the_session_that_held_the_old_key_is_dropped(self):
        row = self._collection()
        token = asyncio.run(unlock_collection(row, PASSPHRASE, "tab", session=self.db))
        self.assertIsNotNone(asyncio.run(file_key_for(row, token)))

        self._rekey(row, token)

        self.assertIsNone(asyncio.run(file_key_for(row, token)))

    def test_a_row_with_an_unknown_wrapper_is_left_and_counted(self):
        row = self._collection()
        item = self._sealed_item()
        item.wrapped_key = "not-an-envelope"
        self.session.commit()
        token = asyncio.run(unlock_collection(row, PASSPHRASE, "tab", session=self.db))

        result = self._rekey(row, token)

        self.assertEqual(0, result["rewrapped_items"])
        self.assertEqual(1, result["skipped_items"])
        self.session.expire_all()
        stored = self.session.get(VaultItem, 7)
        assert stored is not None
        self.assertEqual("not-an-envelope", stored.wrapped_key)

    def test_the_collection_payload_is_rewrapped_too(self):
        row = self._collection()
        public = base64.b64decode(row.inbox_public_key)
        row.description = "скрытое описание"
        sealing.seal_collection_fields(row, public)
        self.session.commit()
        old_wrapper = row.sealed_wrapped_key
        assert old_wrapper is not None, "the collection payload has to exist to be re-wrapped"
        token = asyncio.run(unlock_collection(row, PASSPHRASE, "tab", session=self.db))

        self._rekey(row, token)

        self.assertNotEqual(old_wrapper, row.sealed_wrapped_key)
        self.assertEqual(2, inbox_write_version(row.sealed_wrapped_key))
        fresh = asyncio.run(unlock_collection(row, PASSPHRASE, "tab-2", session=self.db))
        assert fresh
        private = asyncio.run(sealing.data_key_for(row, fresh))
        assert private is not None
        fields = sealing.open_collection_fields(row, private)
        self.assertIn("description", fields)

    def test_rekeying_twice_is_two_operations_not_a_no_op(self):
        row = self._collection()
        self._sealed_item()
        token = asyncio.run(unlock_collection(row, PASSPHRASE, "tab", session=self.db))

        self._rekey(row, token)
        second_token = asyncio.run(unlock_collection(row, PASSPHRASE, "tab-2", session=self.db))
        second = self._rekey(row, second_token)

        self.assertEqual(1, second["rewrapped_items"])
        reopened = asyncio.run(unlock_collection(row, PASSPHRASE, "tab-3", session=self.db))
        assert reopened


if __name__ == "__main__":
    unittest.main()
