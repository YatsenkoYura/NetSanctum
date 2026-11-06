"""A sealed Vault must accept a write from a client that has no passphrase.

The browser extension cannot be given a passphrase: it would sit in a profile
on disk. So a capture into a locked Vault is sealed under the collection's public
inbox key instead. These tests pin the two halves of that promise — the write
goes in unreadable, and it stays unreadable until the passphrase is supplied.
"""

import base64
import json
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.modules.vault.crypto import (
    SealedWrite,
    generate_inbox_keypair,
    open_from_inbox,
    seal_for_inbox,
)
from app.modules.vault.models import Base, VaultCollection, VaultItem
from app.modules.vault.sealing import (
    SEALED_FIELDS,
    inbox_public_key,
    open_item,
    seal_item,
)


class _Adapter:
    """`create_sealed_collection` and friends are async."""

    def __init__(self, session):
        self.session = session

    async def add(self, instance):
        self.session.add(instance)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def refresh(self, instance):
        self.session.refresh(instance)


class InboxCryptoTests(unittest.TestCase):
    def test_a_public_key_alone_can_seal(self):
        private_key, public_key = generate_inbox_keypair()
        write = seal_for_inbox(b"capture bytes", public_key, collection_id=7, kind="item", row_id=1)

        self.assertNotIn(b"capture bytes", write.payload.encode())
        self.assertEqual(
            b"capture bytes", open_from_inbox(write, private_key, collection_id=7, kind="item", row_id=1)
        )

    def test_a_different_private_key_cannot_open_it(self):
        _, public_key = generate_inbox_keypair()
        other_private, _ = generate_inbox_keypair()
        write = seal_for_inbox(b"capture bytes", public_key, collection_id=7, kind="item", row_id=1)

        with self.assertRaises(ValueError):
            open_from_inbox(write, other_private, collection_id=7, kind="item", row_id=1)

    def test_the_public_key_alone_opens_nothing(self):
        _, public_key = generate_inbox_keypair()
        write = seal_for_inbox(b"secret", public_key, collection_id=7, kind="item", row_id=1)

        with self.assertRaises(ValueError):
            open_from_inbox(write, public_key, collection_id=7, kind="item", row_id=1)

    def test_two_writes_to_one_collection_use_different_keys(self):
        _, public_key = generate_inbox_keypair()
        first = seal_for_inbox(b"one", public_key, collection_id=7, kind="item", row_id=1)
        second = seal_for_inbox(b"two", public_key, collection_id=7, kind="item", row_id=2)

        self.assertNotEqual(first.wrapped_key, second.wrapped_key)
        self.assertNotEqual(first.payload, second.payload)

    def test_a_write_cannot_be_moved_to_another_item(self):
        private_key, public_key = generate_inbox_keypair()
        write = seal_for_inbox(b"capture", public_key, collection_id=7, kind="item", row_id=1)

        with self.assertRaises(ValueError):
            open_from_inbox(write, private_key, collection_id=7, kind="item", row_id=2)

    def test_a_tampered_payload_is_refused(self):
        private_key, public_key = generate_inbox_keypair()
        write = seal_for_inbox(b"capture", public_key, collection_id=7, kind="item", row_id=1)
        raw = bytearray(base64.urlsafe_b64decode(write.payload.removeprefix("nsp:v1:")))
        raw[-1] ^= 0xFF
        forged = SealedWrite(
            payload="nsp:v1:" + base64.urlsafe_b64encode(bytes(raw)).decode(),
            wrapped_key=write.wrapped_key,
        )

        with self.assertRaises(ValueError):
            open_from_inbox(forged, private_key, collection_id=7, kind="item", row_id=1)


class BlindWriteTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()
        self.db = _Adapter(self.session)
        self.private_key, self.public_key = generate_inbox_keypair()

        self.collection = VaultCollection(
            name="Скрытое", is_encrypted=True, public_name="Проект Б", color="teal"
        )
        self.collection.inbox_public_key = base64.b64encode(self.public_key).decode("ascii")
        self.collection.inbox_private_key = "nsk:v1:sealed"
        self.session.add(self.collection)
        self.session.flush()

    def _captured_item(self) -> VaultItem:
        """An item as the extension would have left it before being sealed."""
        item = VaultItem(
            entry_type="bookmark",
            title="Скриншот из расширения",
            content="приватная заметка",
            url="https://example.com/private",
            tags=["секретный-тег"],
            collection_id=self.collection.id,
            public_title="Проект Б",
        )
        self.session.add(item)
        self.session.flush()
        return item

    def test_the_public_key_is_readable_so_a_client_can_seal(self):
        self.assertEqual(self.public_key, inbox_public_key(self.collection))

    def test_a_capture_is_stored_unreadable_without_a_passphrase(self):
        item = self._captured_item()
        authored = {field: getattr(item, field, None) for field in SEALED_FIELDS}
        # There really was content to lose: otherwise this proves nothing.
        self.assertEqual("Скриншот из расширения", authored["title"])

        seal_item(item, inbox_public_key(self.collection))
        self.session.commit()

        self.session.expire_all()
        stored = self.session.get(VaultItem, item.id)
        self.assertEqual("", stored.title)
        self.assertEqual([], stored.tags)
        self.assertIsNone(stored.content)
        self.assertIsNone(stored.url)
        self.assertEqual({}, stored.canvas_data)
        for needle in (
            "Скриншот из расширения",
            "приватная заметка",
            "example.com/private",
            "секретный-тег",
        ):
            self.assertNotIn(needle, str(vars(stored)))

    def test_the_alias_stays_readable_so_the_owner_can_recognise_it(self):
        item = self._captured_item()
        seal_item(item, inbox_public_key(self.collection))
        self.session.commit()

        self.session.expire_all()
        self.assertEqual("Проект Б", self.session.get(VaultItem, item.id).public_title)

    def test_the_capture_opens_once_the_passphrase_has_been_supplied(self):
        item = self._captured_item()
        seal_item(item, inbox_public_key(self.collection))
        self.session.commit()

        self.session.expire_all()
        stored = self.session.get(VaultItem, item.id)
        opened = open_item(self.private_key, stored)

        self.assertEqual("Скриншот из расширения", opened.title)
        self.assertEqual("приватная заметка", opened.content)
        self.assertEqual(["секретный-тег"], opened.tags)

    def test_a_sealed_write_survives_a_reload_of_every_row(self):
        item = self._captured_item()
        seal_item(item, inbox_public_key(self.collection))
        self.session.commit()

        self.session.expire_all()
        rows = self.session.execute(select(VaultItem)).scalars().all()
        for row in rows:
            self.assertEqual("Скриншот из расширения", open_item(self.private_key, row).title)

    def test_the_payload_on_disk_is_the_same_json_the_extension_sent(self):
        item = self._captured_item()
        seal_item(item, inbox_public_key(self.collection))
        self.session.commit()

        self.session.expire_all()
        stored = self.session.get(VaultItem, item.id)
        decoded = json.loads(
            open_from_inbox(
                SealedWrite(payload=stored.sealed_payload, wrapped_key=stored.wrapped_key),
                self.private_key,
                collection_id=stored.collection_id,
                kind="item",
                row_id=stored.id,
            )
        )
        self.assertEqual("приватная заметка", decoded["content"])


if __name__ == "__main__":
    unittest.main()
