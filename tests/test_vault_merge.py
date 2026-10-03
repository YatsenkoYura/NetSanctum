"""Merging workspaces: cards move, the emptied workspace is deleted."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.modules.vault.models import VaultCollection
from app.modules.vault.services import (
    VaultCollectionNotFoundError,
    VaultMergeError,
    merge_collections,
)


def make_session(*, collections, items_in_source):
    """An async session double: get/delete/commit are real, execute replays one select."""
    by_id = {collection.id: collection for collection in collections}
    session = SimpleNamespace(
        get=AsyncMock(side_effect=lambda model, ident: by_id.get(ident)),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )

    async def fake_execute(stmt):
        del stmt
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(items_in_source)))

    session.execute = fake_execute
    return session


def collection(cid, *, sealed=False):
    return VaultCollection(id=cid, name=f"ws-{cid}", is_encrypted=sealed)


class MergeCollectionsTests(unittest.TestCase):
    def test_cards_move_and_source_is_deleted(self):
        src, dst = collection(1), collection(2)
        cards = [SimpleNamespace(id=11, collection_id=1), SimpleNamespace(id=12, collection_id=1)]
        session = make_session(collections=[src, dst], items_in_source=cards)

        moved = asyncio.run(merge_collections(session, 1, 2))

        self.assertEqual(2, moved)
        self.assertEqual([2, 2], [card.collection_id for card in cards])
        session.delete.assert_awaited_once_with(src)
        session.commit.assert_awaited_once()

    def test_merge_into_all_unfiles_cards(self):
        src = collection(1)
        cards = [SimpleNamespace(id=11, collection_id=1)]
        session = make_session(collections=[src], items_in_source=cards)

        moved = asyncio.run(merge_collections(session, 1, None))

        self.assertEqual(1, moved)
        self.assertIsNone(cards[0].collection_id)
        session.delete.assert_awaited_once_with(src)

    def test_merge_with_itself_is_refused(self):
        session = make_session(collections=[collection(1)], items_in_source=[])
        with self.assertRaises(VaultMergeError):
            asyncio.run(merge_collections(session, 1, 1))

    def test_sealed_source_is_refused(self):
        session = make_session(collections=[collection(1, sealed=True), collection(2)], items_in_source=[])
        with self.assertRaises(VaultMergeError):
            asyncio.run(merge_collections(session, 1, 2))

    def test_sealed_target_is_refused(self):
        session = make_session(collections=[collection(1), collection(2, sealed=True)], items_in_source=[])
        with self.assertRaises(VaultMergeError):
            asyncio.run(merge_collections(session, 1, 2))

    def test_missing_source_is_not_found(self):
        session = make_session(collections=[collection(2)], items_in_source=[])
        with self.assertRaises(VaultCollectionNotFoundError):
            asyncio.run(merge_collections(session, 1, 2))

    def test_missing_target_is_not_found(self):
        session = make_session(collections=[collection(1)], items_in_source=[])
        with self.assertRaises(VaultCollectionNotFoundError):
            asyncio.run(merge_collections(session, 1, 2))


if __name__ == "__main__":
    unittest.main()
