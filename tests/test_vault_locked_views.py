"""A locked sealed item must be reachable without ever exposing what is inside it.

These tests drive the serialization helpers directly, because that is the code
that decides what leaves the process. Anything that leaks here leaks through
every list, package and capture response that reuses it.
"""

import datetime
import unittest

from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.router import _apply_lock_state, _serialize_collection
from app.modules.vault.sealing import DEFAULT_ITEM_ALIAS, DEFAULT_SEALED_ALIAS


def make_item(**overrides) -> VaultItem:
    now = datetime.datetime(2026, 1, 1, 12, 0, 0)
    values = {
        "color": "teal",
        "icon": None,
        "created_at": now,
        "updated_at": now,
        "is_folder": False,
        "rewatch_count": 0,
        "node_type": "note",
        "progress_current": 0,
        "progress_total": 0,
        "id": 1,
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
        "sealed_payload": "vsealed:abc",
        "collection_id": 7,
        "media_path": None,
        "is_pinned": False,
        "is_archived": False,
    }
    values.update(overrides)
    item = VaultItem()
    for field, value in values.items():
        setattr(item, field, value)
    return item


def serialize(item, *, locked: bool) -> dict:
    return _apply_lock_state(_serialize_dict(item), item, locked=locked)


def _collection(**overrides) -> VaultCollection:
    now = datetime.datetime(2026, 1, 1, 12, 0, 0)
    values = {
        "id": 1,
        "name": "Коллекция",
        "description": None,
        "color": "teal",
        "icon": None,
        "created_at": now,
        "items_count": 0,
        "is_encrypted": False,
        "public_name": None,
    }
    values.update(overrides)
    collection = VaultCollection()
    for field, value in values.items():
        setattr(collection, field, value)
    return collection


def _serialize_dict(item) -> dict:
    from app.modules.vault.router import _serialize_full_item

    return _serialize_full_item(item)


class LockedItemTests(unittest.TestCase):
    def test_a_locked_sealed_item_shows_its_alias_not_its_title(self):
        item = make_item(public_title="Проект А")
        payload = serialize(item, locked=True)

        self.assertEqual("Проект А", payload["title"])
        self.assertEqual("Проект А", payload["public_title"])

    def test_a_locked_item_leaks_no_confidential_field(self):
        item = make_item(
            public_title="Проект А",
            content="строгое содержимое",
            url="https://example.com/secret",
            og_title="заголовок ссылки",
            og_description="описание ссылки",
            og_image="https://example.com/secret.png",
            tags=["секретный-тег"],
            canvas_data={"text": "черновик", "media_status": "done"},
            category="личное",
            score=9.0,
            status="watching",
            media_mime="video/mp4",
            media_path="vault/7/1.mp4",
        )
        payload = serialize(item, locked=True)

        # The readable columns are blanked on disk too; a leak here would mean the
        # serializer is rehydrating something it should not have.
        self.assertEqual("", item.title)
        for needle in (
            "строгое содержимое",
            "example.com/secret",
            "заголовок ссылки",
            "описание ссылки",
            "секретный-тег",
            "черновик",
            "личное",
            "vault/7/1.mp4",
        ):
            self.assertNotIn(needle, str(payload))
        self.assertEqual([], payload["tags"])
        self.assertEqual({}, payload["canvas_data"])
        self.assertFalse(payload["has_media"])
        self.assertFalse(payload["has_image"])

    def test_a_locked_item_advertises_that_it_is_locked(self):
        payload = serialize(make_item(public_title="Проект А"), locked=True)

        self.assertTrue(payload["is_locked"])
        self.assertTrue(payload["is_sealed"])

    def test_an_unlocked_sealed_item_keeps_its_real_title(self):
        item = make_item(public_title="Проект А", title="Настоящее название", content="открытое")
        payload = serialize(item, locked=False)

        self.assertEqual("Настоящее название", payload["title"])
        self.assertEqual("открытое", payload["content"])
        self.assertFalse(payload["is_locked"])
        self.assertTrue(payload["is_sealed"])

    def test_a_plain_item_is_never_treated_as_locked(self):
        item = make_item(sealed_payload=None, title="Обычная запись", content="текст")
        payload = serialize(item, locked=True)

        self.assertEqual("Обычная запись", payload["title"])
        self.assertFalse(payload["is_locked"])
        self.assertFalse(payload["is_sealed"])
        self.assertIsNone(payload["public_title"])

    def test_a_sealed_item_without_an_alias_falls_back(self):
        payload = serialize(make_item(public_title=None), locked=True)

        self.assertEqual(DEFAULT_ITEM_ALIAS, payload["title"])


class LockedCollectionTests(unittest.TestCase):
    def test_a_locked_collection_shows_its_alias(self):
        collection = _collection(
            id=3,
            name="Личные финансы",
            description="квартальные отчёты",
            is_encrypted=True,
            public_name="Проект Б",
        )

        payload = _serialize_collection(collection, locked=True)

        self.assertEqual("Проект Б", payload["name"])
        self.assertIsNone(payload["description"])
        self.assertTrue(payload["is_locked"])

    def test_a_locked_collection_without_an_alias_falls_back(self):
        collection = _collection(id=4, name="Личные финансы", is_encrypted=True, public_name=None)

        self.assertEqual(DEFAULT_SEALED_ALIAS, _serialize_collection(collection, locked=True)["name"])

    def test_a_plain_collection_is_returned_untouched(self):
        collection = _collection(id=5, name="Закладки", description="обычные")

        payload = _serialize_collection(collection, locked=True)

        self.assertEqual("Закладки", payload["name"])
        self.assertEqual("обычные", payload["description"])
        self.assertFalse(payload["is_locked"])
        self.assertFalse(payload["is_encrypted"])


if __name__ == "__main__":
    unittest.main()
