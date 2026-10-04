"""An unlock must belong to one tab, not to the instance.

The key used to sit under `vault_key:<collection_id>`, so any session that found
it could open a sealed Vault without the passphrase, and one tab's "lock" button
locked everyone. These tests pin the per-tab behaviour.
"""

import asyncio
import base64
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.modules.vault import sealing
from app.modules.vault.crypto import generate_inbox_keypair
from app.modules.vault.models import VaultCollection, VaultItem


def _wrong():
    raise ValueError("wrong passphrase")


class _Shim:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)


class _FakeRedis:
    """Just enough Redis to prove which token can reach which key."""

    def __init__(self):
        self.store = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def expire(self, key, seconds):
        return bool(self.store.get(key))

    async def delete(self, key):
        self.store.pop(key, None)

    async def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [key for key in self.store if key.startswith(prefix)]


class PerTabUnlockTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()
        self.private_key, self.public_key = generate_inbox_keypair()
        self.collection = VaultCollection(
            name="Скрытое", is_encrypted=True, public_name="Проект Б", color="teal"
        )
        self.collection.inbox_public_key = base64.b64encode(self.public_key).decode()
        self.session.add(self.collection)
        self.session.flush()
        self.item = VaultItem(
            entry_type="bookmark",
            title="Очень личное",
            content="секрет",
            collection_id=self.collection.id,
            public_title="Проект Б",
        )
        self.session.add(self.item)
        self.session.flush()
        sealing.seal_item(self.item, sealing.inbox_public_key(self.collection))
        self.session.commit()

        self.redis = _FakeRedis()
        patches = [
            patch.object(sealing, "redis_client", self.redis),
            patch.object(
                sealing,
                "kek_for_wrapper",
                lambda wrapped, passphrase: (
                    (b"\x03" * 32, b"\x04" * 16) if passphrase == "верный" else _wrong()
                ),
            ),
            patch.object(sealing, "_unwrap_with_kek", lambda *args: self.private_key),
        ]
        for entered in patches:
            entered.start()
            self.addCleanup(entered.stop)

    def _unlock(self, token: str = "") -> str:
        return asyncio.run(sealing.unlock_collection(self.collection, "верный", token))

    def _key(self, token: str):
        return asyncio.run(sealing.data_key_for(self.collection, token))

    def test_unlocking_returns_a_token_and_the_key_works(self):
        token = self._unlock()

        self.assertTrue(token)
        self.assertEqual(self.private_key, self._key(token))

    def test_a_tab_without_the_token_gets_nothing(self):
        self._unlock()

        self.assertIsNone(self._key(""))
        self.assertIsNone(self._key("чужой-токен"))

    def test_one_tabs_token_does_not_open_another_tab(self):
        tab_a = self._unlock()
        tab_b = "different-token"

        self.assertEqual(self.private_key, self._key(tab_a))
        self.assertIsNone(self._key(tab_b))

    def test_locking_in_one_tab_leaves_another_tab_working(self):
        tab_a = self._unlock()
        tab_b = self._unlock()

        asyncio.run(sealing.lock_collection(self.collection.id, tab_a))

        self.assertIsNone(self._key(tab_a))
        self.assertEqual(self.private_key, self._key(tab_b))

    def test_locking_without_a_token_removes_nothing(self):
        tab_a = self._unlock()

        asyncio.run(sealing.lock_collection(self.collection.id, ""))

        self.assertEqual(self.private_key, self._key(tab_a))

    def test_the_token_is_reused_so_one_tab_opens_many_collections(self):
        first = self._unlock()
        second = self._unlock(first)

        self.assertEqual(first, second)

    def test_the_redis_key_does_not_contain_the_token(self):
        token = self._unlock()
        self._unlock(token)

        for key in self.redis.store:
            self.assertNotIn(token, key)
            self.assertTrue(key.startswith("vault_key:"))

    def test_locked_state_is_decided_per_token(self):
        tab_a = self._unlock()
        tab_b = "other"

        locked_for_a = asyncio.run(sealing.locked_collection_ids(_Shim(self.session), tab_a))
        locked_for_b = asyncio.run(sealing.locked_collection_ids(_Shim(self.session), tab_b))

        self.assertEqual(set(), locked_for_a)
        self.assertEqual({self.collection.id}, locked_for_b)

    def test_an_open_tab_cannot_read_the_payload_without_its_token(self):
        token = self._unlock()
        items = list(self.session.execute(select(VaultItem)).scalars())

        asyncio.run(sealing.open_items(_Shim(self.session), items, ""))
        self.assertEqual("", items[0].title)

        asyncio.run(sealing.open_items(_Shim(self.session), items, token))
        self.assertEqual("Очень личное", items[0].title)


if __name__ == "__main__":
    unittest.main()
