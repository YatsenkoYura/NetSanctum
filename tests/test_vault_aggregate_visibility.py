"""A sealed Vault must not surface through the board that aggregates every card.

Sealing protected the row itself, but the "Все карточки" view and the sidebar
statistics still counted it. That leaked the existence of a private record, its
alias and its rating to a board that needs no passphrase. These tests hold the
aggregate paths closed while leaving the explicit collection view open.
"""

import asyncio
import datetime
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.modules.vault.crypto import generate_inbox_keypair
from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.sealing import seal_item
from app.modules.vault.services import get_vault_stats, list_vault_items


class _Shim:
    """The service layer is async; this drives a real sync session through it."""

    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)


class AggregateViewTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()
        self.private_key, self.public_key = generate_inbox_keypair()
        now = datetime.datetime(2026, 1, 1)

        self.sealed_collection = VaultCollection(
            name="Скрытое", is_encrypted=True, public_name="Проект Б", color="teal", created_at=now
        )
        self.open_collection = VaultCollection(name="Обычные", is_encrypted=False, created_at=now)
        self.session.add_all([self.sealed_collection, self.open_collection])
        self.session.flush()
        self.private_item = VaultItem(
            entry_type="thought",
            title="Очень личное",
            content="секрет",
            score=10.0,
            tags=["секретный-тег"],
            collection_id=self.sealed_collection.id,
            public_title="Проект Б",
            created_at=now,
            updated_at=now,
        )
        self.public_item = VaultItem(
            entry_type="bookmark",
            title="Обычная закладка",
            content="публично",
            score=5.0,
            collection_id=self.open_collection.id,
            created_at=now,
            updated_at=now,
        )
        self.session.add_all([self.private_item, self.public_item])
        self.session.flush()
        seal_item(self.private_item, self.public_key)
        self.session.commit()

    def _list(self, **kwargs):
        return asyncio.run(list_vault_items(_Shim(self.session), **kwargs))

    def _stats(self):
        return asyncio.run(get_vault_stats(_Shim(self.session)))

    def test_the_all_cards_view_leaves_out_the_sealed_vault(self):
        items = self._list(collection_id=None)

        self.assertEqual([self.public_item.id], [item.id for item in items])

    def test_the_sealed_alias_does_not_appear_either(self):
        items = self._list(collection_id=None)

        for item in items:
            self.assertNotEqual("Проект Б", item.title)
            self.assertNotEqual("Проект Б", item.public_title)

    def test_opening_the_sealed_collection_by_hand_still_works(self):
        """Only the aggregate board is closed; the Vault itself must open."""
        items = self._list(collection_id=self.sealed_collection.id)

        self.assertEqual([self.private_item.id], [item.id for item in items])

    def test_the_sidebar_count_excludes_the_sealed_records(self):
        stats = self._stats()

        self.assertEqual(1, stats["total_items"])

    def test_the_average_score_cannot_be_moved_by_a_private_record(self):
        stats = self._stats()

        # The sealed record scores 10.0; only the public 5.0 may count.
        self.assertEqual(5.0, stats["avg_score"])

    def test_a_search_inside_the_aggregate_view_cannot_find_it(self):
        items = self._list(collection_id=None, q="личное")

        self.assertEqual([], items)

    def test_pinned_sealed_records_are_not_counted(self):
        self.private_item.is_pinned = True
        self.session.commit()

        stats = self._stats()

        self.assertEqual(0, stats["pinned_count"])


if __name__ == "__main__":
    unittest.main()
