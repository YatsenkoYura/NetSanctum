"""Spaces nest, and cards stay where they were put.

Both features rest on a fractional `position`: a drop between two neighbours
stores the midpoint, so ordering one card costs one row write instead of
renumbering the whole level. The tests below pin the parts that are easy to get
subtly wrong — what happens when the neighbours have been squeezed together,
what a sealed space is allowed to contain, and that a drop cannot invent a loop.
"""

import asyncio
import unittest

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.services import (
    POSITION_MIN_GAP,
    VaultOrderError,
    _midpoint,
    dissolve_space,
    list_child_spaces,
    list_vault_items,
    reorder_card,
    reorder_space,
    update_vault_item,
)


class AsyncSessionAdapter:
    """The slice of AsyncSession the ordering code touches."""

    def __init__(self, session):
        self.session = session

    async def execute(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {})

    async def scalars(self, statement):
        return self.session.scalars(statement)

    async def get(self, model, ident):
        return self.session.get(model, ident)

    async def commit(self):
        self.session.commit()

    async def flush(self):
        self.session.flush()

    async def refresh(self, instance):
        self.session.refresh(instance)

    async def delete(self, instance):
        self.session.delete(instance)


def run(coro):
    return asyncio.run(coro)


class OrderingTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.session = AsyncSessionAdapter(self.db)
        self.addCleanup(self.engine.dispose)

    def add_card(self, card_id, position, collection_id=1):
        self.db.add(
            VaultItem(
                id=card_id,
                entry_type="bookmark",
                title=f"Карточка {card_id}",
                tags=[],
                position=position,
                collection_id=collection_id,
            )
        )
        self.db.commit()

    def add_space(self, space_id, parent_id=None, position=None, sealed=False):
        self.db.add(
            VaultCollection(
                id=space_id,
                name=f"Пространство {space_id}",
                parent_id=parent_id,
                position=position,
                is_encrypted=sealed,
            )
        )
        self.db.commit()

    def ordered_ids(self, **kwargs):
        return [item.id for item in run(list_vault_items(self.session, **kwargs))]


class MidpointTests(unittest.TestCase):
    def test_the_middle_of_two_neighbours_lies_between_them(self):
        self.assertEqual(150.0, _midpoint(100.0, 200.0))

    def test_a_missing_neighbour_lands_outside_the_one_that_is_there(self):
        # `before` is the card above, `after` the one below, so dropping at the
        # end has to land past `before` and at the start before `after`.
        self.assertEqual(2024.0, _midpoint(1000.0, None))
        self.assertEqual(0.0, _midpoint(None, 1024.0))

    def test_an_empty_level_starts_at_zero(self):
        self.assertEqual(0.0, _midpoint(None, None))


class CardOrderTests(OrderingTestCase):
    def test_cards_come_back_in_their_saved_order(self):
        for card_id, position in ((1, 30.0), (2, 10.0), (3, 20.0)):
            self.add_card(card_id, position)

        self.assertEqual([2, 3, 1], self.ordered_ids())

    def test_a_card_with_no_position_sorts_after_the_ones_that_have_one(self):
        self.add_card(1, 10.0)
        self.add_card(2, None)

        self.assertEqual([1, 2], self.ordered_ids())

    def test_a_drop_between_two_neighbours_lands_between_them(self):
        for card_id, position in ((1, 10.0), (2, 30.0), (3, 90.0)):
            self.add_card(card_id, position)
        self.assertEqual([1, 2, 3], self.ordered_ids())

        run(reorder_card(self.session, 3, 1, 2))
        self.db.expire_all()

        self.assertEqual([1, 3, 2], self.ordered_ids())

    def test_a_drop_onto_the_front_or_the_back_uses_the_missing_side(self):
        for card_id, position in ((1, 10.0), (2, 20.0)):
            self.add_card(card_id, position)

        run(reorder_card(self.session, 1, None, 2))  # to the very front
        run(reorder_card(self.session, 1, 2, None))  # and back to the end
        self.db.expire_all()

        self.assertEqual([2, 1], self.ordered_ids())

    def test_a_card_cannot_be_dropped_between_its_own_neighbours(self):
        self.add_card(1, 10.0)
        self.add_card(2, 20.0)

        with self.assertRaises(VaultOrderError):
            run(reorder_card(self.session, 1, 1, 2))

    def test_neighbours_from_another_space_are_refused(self):
        self.add_card(1, 10.0, collection_id=1)
        self.add_card(2, 20.0, collection_id=2)

        with self.assertRaises(VaultOrderError):
            run(reorder_card(self.session, 2, 1, None))

    def test_a_neighbour_that_does_not_exist_is_refused(self):
        self.add_card(1, 10.0)

        with self.assertRaises(VaultOrderError):
            run(reorder_card(self.session, 1, 404, None))

    def test_the_order_survives_repeatedly_squeezing_two_neighbours_together(self):
        # Each drop halves the gap. Without a renumber the float would run out of
        # room and the card would land on top of a neighbour.
        self.add_card(1, 0.0)
        self.add_card(2, POSITION_MIN_GAP * 4)
        self.add_card(3, 1.0)
        for _ in range(12):
            run(reorder_card(self.session, 3, 1, 2))

        self.db.expire_all()
        positions = {item.id: item.position for item in self.db.query(VaultItem).all()}
        self.assertLess(positions[1], positions[3])
        self.assertLess(positions[3], positions[2])

    def test_a_pinned_card_keeps_the_top_whatever_its_position_says(self):
        self.add_card(1, 99.0)
        self.add_card(2, 1.0)
        self.db.query(VaultItem).filter_by(id=2).one().is_pinned = True
        self.db.commit()

        self.assertEqual([2, 1], self.ordered_ids())


class SpaceTreeTests(OrderingTestCase):
    def test_a_space_can_be_nested_under_another(self):
        self.add_space(1)
        self.add_space(2)

        run(reorder_space(self.session, 2, 1))
        self.db.expire_all()

        self.assertEqual(1, self.db.get(VaultCollection, 2).parent_id)

    def test_children_come_back_in_their_saved_order(self):
        self.add_space(1)
        self.add_space(2, parent_id=1, position=20.0)
        self.add_space(3, parent_id=1, position=10.0)

        children = run(list_child_spaces(self.session, 1))

        self.assertEqual([3, 2], [child.id for child in children])

    def test_a_space_cannot_be_its_own_parent(self):
        self.add_space(1)

        with self.assertRaises(VaultOrderError):
            run(reorder_space(self.session, 1, 1))

    def test_a_space_cannot_be_dropped_into_its_own_descendant(self):
        self.add_space(1)
        self.add_space(2, parent_id=1)
        self.add_space(3, parent_id=2)

        # 1 -> 3 would make 3 its own grandparent's child.
        with self.assertRaises(VaultOrderError):
            run(reorder_space(self.session, 1, 3))

    def test_a_sealed_space_can_be_nested_under_a_sealed_one(self):
        self.add_space(1, sealed=True)
        self.add_space(2, sealed=True)

        run(reorder_space(self.session, 2, 1))
        self.db.expire_all()

        self.assertEqual(1, self.db.get(VaultCollection, 2).parent_id)

    def test_a_plain_space_can_be_nested_inside_a_sealed_one(self):
        self.add_space(1, sealed=True)
        self.add_space(2)

        run(reorder_space(self.session, 2, 1))
        self.db.expire_all()

        self.assertEqual(1, self.db.get(VaultCollection, 2).parent_id)

    def test_a_sealed_space_can_be_nested_under_a_plain_one(self):
        # Its name is hidden while locked, so a plain parent leaks nothing.
        self.add_space(1)
        self.add_space(2, sealed=True)

        run(reorder_space(self.session, 2, 1))
        self.db.expire_all()

        self.assertEqual(1, self.db.get(VaultCollection, 2).parent_id)

    def test_a_sealed_space_can_still_be_moved_among_the_roots(self):
        self.add_space(1, position=10.0)
        self.add_space(2, position=20.0, sealed=True)

        # Dropped after space 1: the root level takes it, sealed or not.
        run(reorder_space(self.session, 2, None, 1, None))
        self.db.expire_all()

        self.assertEqual([1, 2], [space.id for space in run(list_child_spaces(self.session, None))])

    def test_nesting_into_a_missing_space_is_a_not_found(self):
        from app.modules.vault.services import VaultCollectionNotFoundError

        self.add_space(1)

        with self.assertRaises(VaultCollectionNotFoundError):
            run(reorder_space(self.session, 1, 404))

    def test_sibling_neighbours_from_another_branch_are_refused(self):
        self.add_space(1)
        self.add_space(2, parent_id=1, position=10.0)
        self.add_space(3)

        # Card 3 is a root; it cannot be dropped next to a child of 1.
        with self.assertRaises(VaultOrderError):
            run(reorder_space(self.session, 3, None, 2, None))


if __name__ == "__main__":
    unittest.main()


class DissolveSpaceTests(OrderingTestCase):
    """A folder's contents move up and take the folder's own place.

    The place is the whole point: a dissolve that appended to the end would
    silently reorder the sidebar, which is the arrangement the owner built.
    """

    def test_a_folder_lifts_its_contents_into_the_parent(self):
        self.add_space(1)
        self.add_space(2, parent_id=1)
        self.add_space(3, parent_id=2)
        self.add_card(10, 0.0, collection_id=2)

        moved = run(dissolve_space(self.session, 2))

        self.assertEqual({"spaces": 1, "cards": 1, "index": 0}, moved)
        self.db.expire_all()
        self.assertIsNone(self.db.get(VaultCollection, 2))
        self.assertEqual(1, self.db.get(VaultCollection, 3).parent_id)
        self.assertEqual(1, self.db.get(VaultItem, 10).collection_id)

    def test_the_lifted_spaces_take_the_folders_slot_not_the_end(self):
        # У родителя 1: ПАПКА(0), X(1024), Y(2048). Внутри папки: B1(0), B2(1024).
        self.add_space(1)
        self.add_space(2, parent_id=1, position=0.0)
        self.add_space(3, parent_id=1, position=1024.0)
        self.add_space(4, parent_id=1, position=2048.0)
        self.add_space(5, parent_id=2, position=0.0)
        self.add_space(6, parent_id=2, position=1024.0)

        run(dissolve_space(self.session, 2))
        self.db.expire_all()

        # B1 и B2 занимают слот папки — до X и Y, а не после них.
        self.assertEqual([5, 6, 3, 4], [space.id for space in run(list_child_spaces(self.session, 1))])

    def test_the_lifted_spaces_keep_their_own_order(self):
        self.add_space(1)
        self.add_space(2, parent_id=1)
        self.add_space(3, parent_id=2, position=1024.0)
        self.add_space(4, parent_id=2, position=0.0)

        run(dissolve_space(self.session, 2))
        self.db.expire_all()

        self.assertEqual([4, 3], [space.id for space in run(list_child_spaces(self.session, 1))])

    def test_a_folder_in_the_middle_keeps_the_ones_around_it_in_place(self):
        # У родителя 1: X(0), ПАПКА(1024), Y(2048).
        self.add_space(1)
        self.add_space(2, parent_id=1, position=1024.0)
        self.add_space(3, parent_id=1, position=0.0)
        self.add_space(4, parent_id=2, position=0.0)
        self.add_space(5, parent_id=1, position=2048.0)

        moved = run(dissolve_space(self.session, 2))
        self.db.expire_all()

        self.assertEqual(1, moved["index"])
        self.assertEqual([3, 4, 5], [space.id for space in run(list_child_spaces(self.session, 1))])

    def test_an_empty_folder_just_disappears(self):
        self.add_space(1)
        self.add_space(2, parent_id=1)

        moved = run(dissolve_space(self.session, 2))

        self.assertEqual({"spaces": 0, "cards": 0, "index": 0}, moved)
        self.db.expire_all()
        self.assertIsNone(self.db.get(VaultCollection, 2))

    def test_a_root_space_has_nothing_to_dissolve_into(self):
        from app.modules.vault.services import VaultDissolveError

        self.add_space(1)

        with self.assertRaises(VaultDissolveError):
            run(dissolve_space(self.session, 1))

    def test_an_empty_sealed_folder_can_be_dissolved(self):
        # The case that prompted it: a private space nested in another private one,
        # with nothing in it. It carries nothing, so it goes like any other.
        self.add_space(1, sealed=True)
        self.add_space(2, parent_id=1, sealed=True)

        moved = run(dissolve_space(self.session, 2))

        self.assertEqual({"spaces": 0, "cards": 0, "index": 0}, moved)
        self.db.expire_all()
        self.assertIsNone(self.db.get(VaultCollection, 2))

    def test_a_sealed_folder_that_holds_cards_is_kept(self):
        """Its cards are sealed under its key; lifting them would leave ciphertext
        the parent space cannot open."""
        from app.modules.vault.services import VaultDissolveError

        self.add_space(1, sealed=True)
        self.add_space(2, parent_id=1, sealed=True)
        self.add_card(10, 0.0, collection_id=2)

        with self.assertRaises(VaultDissolveError):
            run(dissolve_space(self.session, 2))

        self.assertIsNotNone(self.db.get(VaultCollection, 2))


if __name__ == "__main__":
    unittest.main()


class MoveCardBetweenSpacesTests(OrderingTestCase):
    """A card can change spaces, but not into a state it can never be opened from.

    Merging refused these moves already; the patch path did not, so any client
    could walk a card into a sealed space (where no read path shows it) or out of
    one (where its own wrapped key no longer opens anywhere).
    """

    def test_a_card_cannot_be_moved_into_a_space_that_does_not_exist(self):
        """SQLite does not enforce the foreign key, so without this the write
        reached the database and came back as a 500."""
        from app.modules.vault.schemas import VaultItemUpdate
        from app.modules.vault.services import VaultMoveError

        self.add_space(1)
        self.add_card(10, 0.0, collection_id=1)

        with self.assertRaises(VaultMoveError):
            run(
                update_vault_item(
                    self.session, self.db.get(VaultItem, 10), VaultItemUpdate(collection_id=404)
                )
            )

    def test_a_plain_card_moves_between_plain_spaces(self):
        from app.modules.vault.schemas import VaultItemUpdate

        self.add_space(1)
        self.add_space(2)
        self.add_card(10, 0.0, collection_id=1)
        item = self.db.get(VaultItem, 10)

        run(update_vault_item(self.session, item, VaultItemUpdate(collection_id=2)))
        self.db.expire_all()

        self.assertEqual(2, self.db.get(VaultItem, 10).collection_id)

    def test_a_plain_card_can_be_unfiled(self):
        from app.modules.vault.schemas import VaultItemUpdate

        self.add_space(1)
        self.add_card(10, 0.0, collection_id=1)
        item = self.db.get(VaultItem, 10)

        run(update_vault_item(self.session, item, VaultItemUpdate(collection_id=None)))
        self.db.expire_all()

        self.assertIsNone(self.db.get(VaultItem, 10).collection_id)

    def test_a_plain_card_cannot_move_into_a_sealed_space(self):
        from app.modules.vault.schemas import VaultItemUpdate
        from app.modules.vault.services import VaultMoveError

        self.add_space(1, sealed=True)
        self.add_card(10, 0.0, collection_id=None)
        item = self.db.get(VaultItem, 10)

        with self.assertRaises(VaultMoveError):
            run(update_vault_item(self.session, item, VaultItemUpdate(collection_id=1)))

    def test_a_sealed_card_cannot_be_carried_into_another_space(self):
        from app.modules.vault.schemas import VaultItemUpdate
        from app.modules.vault.services import VaultMoveError

        self.add_space(1)
        self.add_space(2)
        self.add_card(10, 0.0, collection_id=1)
        item = self.db.get(VaultItem, 10)
        item.sealed_payload = "nsp:v1:blob"
        self.db.commit()

        with self.assertRaises(VaultMoveError):
            run(update_vault_item(self.session, item, VaultItemUpdate(collection_id=2)))

    def test_a_stack_goes_with_its_cover_into_the_other_space(self):
        """Children hang off the cover by `parent_id`. Moving only the cover left
        them behind as orphans pointing into another space."""
        from app.modules.vault.schemas import VaultItemUpdate

        self.add_space(1)
        self.add_space(2)
        self.add_card(10, 0.0, collection_id=1)  # обложка
        self.add_card(11, 0.0, collection_id=1)
        self.db.get(VaultItem, 11).parent_id = 10
        self.db.commit()

        run(update_vault_item(self.session, self.db.get(VaultItem, 10), VaultItemUpdate(collection_id=2)))
        self.db.expire_all()

        self.assertEqual(2, self.db.get(VaultItem, 10).collection_id)
        self.assertEqual(2, self.db.get(VaultItem, 11).collection_id)

    def test_a_card_without_a_stack_moves_alone(self):
        from app.modules.vault.schemas import VaultItemUpdate

        self.add_space(1)
        self.add_space(2)
        self.add_card(10, 0.0, collection_id=1)
        self.add_card(11, 0.0, collection_id=1)

        run(update_vault_item(self.session, self.db.get(VaultItem, 10), VaultItemUpdate(collection_id=2)))
        self.db.expire_all()

        self.assertEqual(1, self.db.get(VaultItem, 11).collection_id)

    def test_setting_the_same_space_again_is_not_a_move(self):
        from app.modules.vault.schemas import VaultItemUpdate

        self.add_space(1, sealed=True)
        self.add_card(10, 0.0, collection_id=1)
        item = self.db.get(VaultItem, 10)
        item.sealed_payload = "nsp:v1:blob"
        self.db.commit()

        # A drag onto the space it already lives in must not raise just because
        # that space is sealed.
        run(update_vault_item(self.session, item, VaultItemUpdate(collection_id=1)))
        self.assertEqual(1, self.db.get(VaultItem, 10).collection_id)
