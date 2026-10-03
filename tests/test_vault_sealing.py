"""A sealed item must leave nothing readable behind."""

import asyncio
import datetime
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.modules.vault.crypto import generate_inbox_keypair
from app.modules.vault.models import Base, VaultCollection, VaultItem
from app.modules.vault.schemas import VaultItemUpdate
from app.modules.vault.sealing import (
    SEALED_FIELDS,
    item_is_sealed,
    open_item,
    seal_item,
    update_sealed_item,
)


def make_item(collection_id: int | None = None) -> VaultItem:
    now = datetime.datetime(2026, 1, 1)
    return VaultItem(
        id=7,
        entry_type="bookmark",
        node_type="image",
        title="Личное название",
        content="Личный текст",
        url="https://example.com/secret",
        og_title="Личное og",
        og_description="Личное описание",
        og_image="data:image/png;base64,AAAA",
        tags=["secret-tag"],
        canvas_data={"drawing": [{"x": 1}]},
        category="private",
        score=9.5,
        status="planned",
        collection_id=collection_id,
        media_mime="video/mp4",
        is_pinned=True,
        created_at=now,
        updated_at=now,
    )


class SealItemTests(unittest.TestCase):
    def setUp(self):
        self.private_key, self.public_key = generate_inbox_keypair()
        self.item = make_item()
        seal_item(self.item, self.public_key)

    def test_every_owner_field_is_blanked(self):
        for field in SEALED_FIELDS:
            value = getattr(self.item, field)
            self.assertIn(value, (None, "", [], {}), f"{field} still holds {value!r}")

    def test_nothing_readable_survives_in_the_row(self):
        # The strongest statement of the promise: no column of the row, and no
        # substring of the payload, leaks the content.
        for needle in ("Личное название", "Личный текст", "secret-tag", "example.com/secret"):
            for column, value in vars(self.item).items():
                if isinstance(value, str):
                    self.assertNotIn(needle, value, f"{needle} leaked through {column}")

    def test_structural_columns_stay_readable(self):
        # The grid needs these to lay out a tile at all.
        self.assertEqual("image", self.item.node_type)
        self.assertEqual("bookmark", self.item.entry_type)
        self.assertTrue(self.item.is_pinned)
        self.assertEqual(7, self.item.id)

    def test_the_payload_is_marked_sealed(self):
        self.assertTrue(item_is_sealed(self.item))
        self.assertTrue(self.item.sealed_payload.startswith("nsp:v1:"))

    def test_opening_restores_everything(self):
        open_item(self.private_key, self.item)
        self.assertEqual("Личное название", self.item.title)
        self.assertEqual("Личный текст", self.item.content)
        self.assertEqual(["secret-tag"], self.item.tags)
        self.assertEqual({"drawing": [{"x": 1}]}, self.item.canvas_data)
        self.assertEqual(9.5, self.item.score)
        self.assertEqual("video/mp4", self.item.media_mime)
        # The blob stays: it is the state at rest, and dropping it here would let
        # the next save write the item back in the clear.
        self.assertTrue(item_is_sealed(self.item))

    def test_another_vaults_key_cannot_open_it(self):
        with self.assertRaises(ValueError):
            open_item(generate_inbox_keypair()[0], self.item)

    def test_the_payload_cannot_be_moved_to_another_row(self):
        moved = make_item()
        moved.id = 8
        moved.sealed_payload = self.item.sealed_payload
        with self.assertRaises(ValueError):
            open_item(self.private_key, moved)

    def test_re_sealing_is_stable(self):
        opened = open_item(self.private_key, self.item)
        first = opened.sealed_payload
        seal_item(opened, self.public_key)
        self.assertNotEqual(first, opened.sealed_payload)
        open_item(self.private_key, opened)
        self.assertEqual("Личное название", opened.title)

    def test_an_item_with_nothing_to_hide_still_seals(self):
        empty = make_item()
        empty.title = None
        empty.content = None
        empty.tags = []
        seal_item(empty, self.public_key)
        self.assertTrue(item_is_sealed(empty))
        open_item(self.private_key, empty)
        self.assertEqual("", empty.title)
        self.assertEqual([], empty.tags)


class SealedCollectionModelTests(unittest.TestCase):
    def test_a_sealed_collection_defaults_to_plain(self):
        engine = create_engine("sqlite://")
        from app.core.database import Base

        Base.metadata.create_all(engine, tables=[VaultCollection.__table__, VaultItem.__table__])
        session = Session(engine)
        plain = VaultCollection(name="Обычный", color="teal")
        sealed = VaultCollection(name="Зашифрованный", color="teal", is_encrypted=True)
        session.add_all([plain, sealed])
        session.commit()
        self.assertFalse(plain.is_encrypted)
        self.assertTrue(sealed.is_encrypted)
        self.assertIsNone(plain.wrapped_key)
        self.assertIsNone(sealed.wrapped_key)

    def test_seal_survives_a_round_trip_through_the_database(self):
        engine = create_engine("sqlite://")
        from app.core.database import Base

        Base.metadata.create_all(engine, tables=[VaultCollection.__table__, VaultItem.__table__])
        session = Session(engine, expire_on_commit=False)
        private_key, public_key = generate_inbox_keypair()
        item = make_item()
        session.add(item)
        session.commit()
        seal_item(item, public_key)
        session.commit()

        session.expire_all()
        stored = session.get(VaultItem, 7)
        self.assertTrue(item_is_sealed(stored))
        with self.assertRaises(ValueError):
            open_item(generate_inbox_keypair()[0], stored)
        open_item(private_key, stored)
        self.assertEqual("Личное название", stored.title)


def _sealed_collection() -> VaultCollection:
    collection = VaultCollection(name="Скрытое", is_encrypted=True, public_name="Проект Б", color="teal")
    collection.inbox_public_key = ""
    return collection


class AsyncSessionAdapter:
    """`update_sealed_item` is async; this drives a real sync session through it."""

    def __init__(self, session: Session):
        self.session = session

    async def commit(self):
        self.session.commit()

    async def refresh(self, instance):
        self.session.refresh(instance)

    async def get(self, model, identity):
        return self.session.get(model, identity)


class SealedUpdateTests(unittest.TestCase):
    """Editing a sealed item must never write a plaintext copy back to disk."""

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()
        self.db = AsyncSessionAdapter(self.session)
        self.private_key, self.public_key = generate_inbox_keypair()
        self.item = VaultItem(
            entry_type="bookmark", title="Личное название", content="Личный текст", tags=["secret-tag"]
        )
        self.collection = _sealed_collection()
        self.session.add(self.collection)
        self.session.add(self.item)
        self.session.flush()
        self.item.collection_id = self.collection.id
        seal_item(self.item, self.public_key)
        self.session.commit()

    def test_an_edit_is_written_back_sealed(self):
        update = VaultItemUpdate(title="Новое название", content="новый текст")
        asyncio.run(update_sealed_item(self.db, self.item, update, self.private_key, self.public_key))

        self.session.expire_all()
        stored = self.session.get(VaultItem, self.item.id)
        self.assertEqual("", stored.title)
        self.assertIsNone(stored.content)
        self.assertTrue(stored.sealed_payload)
        opened = open_item(self.private_key, stored)
        self.assertEqual("Новое название", opened.title)
        self.assertEqual("новый текст", opened.content)

    def test_the_alias_is_the_only_thing_left_readable(self):
        self.item.public_title = "Прежний псевдоним"
        self.session.commit()
        asyncio.run(
            update_sealed_item(
                self.db, self.item, VaultItemUpdate(title="Правка"), self.private_key, self.public_key
            )
        )

        self.session.expire_all()
        stored = self.session.get(VaultItem, self.item.id)
        self.assertEqual("Прежний псевдоним", stored.public_title)
        for needle in ("Правка", "Личное название", "Личный текст", "secret-tag"):
            self.assertNotIn(needle, str(vars(stored)))

    def test_a_sealed_edit_survives_a_reload(self):
        asyncio.run(
            update_sealed_item(
                self.db,
                self.item,
                VaultItemUpdate(title="После перезагрузки"),
                self.private_key,
                self.public_key,
            )
        )

        self.session.expire_all()
        stored = self.session.get(VaultItem, self.item.id)
        self.assertEqual("", stored.title)
        self.assertEqual("После перезагрузки", open_item(self.private_key, stored).title)

    def test_a_wrong_dek_cannot_rewrite_a_sealed_item(self):
        from app.modules.vault.crypto import generate_inbox_keypair

        with self.assertRaises(ValueError):
            asyncio.run(
                update_sealed_item(
                    self.db,
                    self.item,
                    VaultItemUpdate(title="Взлом"),
                    generate_inbox_keypair()[0],
                    self.public_key,
                )
            )
