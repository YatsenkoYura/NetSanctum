"""A sealed item must leave nothing readable behind."""

import asyncio
import datetime
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.modules.vault.crypto import generate_inbox_keypair
from app.modules.vault.models import Base, VaultCollection, VaultItem
from app.modules.vault.schemas import VaultItemUpdate
from app.modules.vault.sealing import (
    SEALED_FIELDS,
    VaultMoveError,
    item_is_sealed,
    move_sealed_item,
    open_item,
    seal_item,
    update_sealed_item,
)


def make_item(collection_id: int | None = 1) -> VaultItem:
    # A sealed card always belongs to the collection whose public key sealed it:
    # the envelope is bound to that collection, so there is nothing to bind to
    # without one.
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
        item = make_item(collection_id=1)
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


class _Session:
    """Just enough session for move_sealed_item, over a real in-memory database.

    It queries for the cards inside a stack now, so a stub with `commit` and
    `refresh` stopped being enough — the rows that move are the point of the test.
    """

    def __init__(self, db):
        self.db = db

    async def execute(self, statement, parameters=None):
        return self.db.execute(statement, parameters or {})

    async def commit(self):
        self.db.commit()

    async def refresh(self, instance):
        self.db.refresh(instance)


class MoveSealedItemTests(unittest.TestCase):
    """A sealed card can move to another sealed space, and stays readable there.

    The payload is sealed under the *collection's* inbox key, so simply changing
    `collection_id` would deliver a card that nothing in the new space can open.
    The move has to open it with the source key and re-seal under the target's —
    which is why it needs the source Vault unlocked, like any other sealed edit.
    """

    def setUp(self):
        self.source_private, self.source_public = generate_inbox_keypair()
        self.target_private, self.target_public = generate_inbox_keypair()
        self.source = self._collection("Источник", self.source_public, 1)
        self.target = self._collection("Приёмник", self.target_public, 2)
        self.item = make_item(collection_id=1)
        seal_item(self.item, self.source_public)
        self.item.collection_id = 1
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)
        self.db.add_all([self.source, self.target, self.item])
        self.db.commit()
        self.session = _Session(self.db)

    def _collection(self, name, public_key, collection_id):
        from base64 import b64encode

        return VaultCollection(
            id=collection_id,
            name=name,
            is_encrypted=True,
            inbox_public_key=b64encode(public_key).decode("ascii"),
            wrapped_key="nsk:v1:stub",
            key_salt="c2FsdA",
        )

    def test_the_card_is_readable_in_the_space_it_moved_to(self):
        asyncio.run(move_sealed_item(self.session, self.item, self.target, self.source_private))

        self.assertEqual(2, self.item.collection_id)
        open_item(self.target_private, self.item)
        self.assertEqual("Личное название", self.item.title)
        self.assertEqual("Личный текст", self.item.content)

    def test_the_old_space_can_no_longer_open_it(self):
        asyncio.run(move_sealed_item(self.session, self.item, self.target, self.source_private))

        with self.assertRaises(ValueError):
            open_item(self.source_private, self.item)

    def test_the_readable_columns_stay_blank_after_the_move(self):
        asyncio.run(move_sealed_item(self.session, self.item, self.target, self.source_private))

        self.assertTrue(self.item.sealed_payload)
        self.assertEqual("", self.item.title)
        self.assertEqual({}, self.item.canvas_data)

    def test_a_sealed_stack_travels_whole(self):
        """The cards inside the cover are sealed under the space the stack left.

        Moving only the cover left them behind, still pointing at a cover in
        another space — a stack quietly split in two, half of it readable by
        nobody and half by the wrong key.
        """
        inner = make_item(collection_id=1)
        inner.id = 8
        inner.title = "Внутренняя карточка"
        inner.parent_id = self.item.id
        seal_item(inner, self.source_public)
        self.db.add(inner)
        self.db.commit()

        asyncio.run(move_sealed_item(self.session, self.item, self.target, self.source_private))

        self.assertEqual(2, inner.collection_id)
        self.db.refresh(inner)
        open_item(self.target_private, inner)
        self.assertEqual("Внутренняя карточка", inner.title)

    def test_a_sealed_card_may_not_be_unfiled(self):
        """ "Все карточки" would leave it sealed under a key nothing outside the
        space holds. The client no longer offers the option; the server refuses it
        rather than tripping over a missing target."""
        with self.assertRaises(VaultMoveError):
            asyncio.run(move_sealed_item(self.session, self.item, None, self.source_private))

    def test_a_sealed_card_may_not_be_moved_into_a_plain_space(self):
        plain = VaultCollection(name="Обычное", is_encrypted=False)

        with self.assertRaises(VaultMoveError):
            asyncio.run(move_sealed_item(self.session, self.item, plain, self.source_private))
