import asyncio
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.contracts.vault_spaces_v1 import VaultSpacesRequest
from app.core.database import Base
from app.core.module_types import IntegrationContext
from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.spaces import list_spaces


class ExecuteOnlySession:
    """The spaces handler only reads, so the adapter only needs execute."""

    def __init__(self, session: Session):
        self.session = session

    async def execute(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {})


class VaultSpacesIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine,
            tables=[VaultCollection.__table__, VaultItem.__table__],
        )
        self.session = Session(self.engine, expire_on_commit=False)
        work = VaultCollection(name="Работа", color="blue", icon="briefcase")
        home = VaultCollection(name="Дом", color="orange", icon="house")
        self.session.add_all([work, home])
        self.session.flush()
        project = VaultItem(
            title="Проект X",
            node_type="folder",
            is_folder=True,
            collection_id=work.id,
        )
        self.session.add(project)
        self.session.flush()
        child = VaultItem(
            title="Фаза 1",
            node_type="folder",
            is_folder=True,
            collection_id=work.id,
            parent_id=project.id,
        )
        archived = VaultItem(
            title="Старое",
            node_type="folder",
            is_folder=True,
            is_archived=True,
            collection_id=home.id,
        )
        note = VaultItem(title="Просто заметка", node_type="note", collection_id=work.id)
        self.session.add_all([child, archived, note])
        self.session.commit()
        self.work_id = work.id
        self.home_id = home.id
        self.context = IntegrationContext(
            session=ExecuteOnlySession(self.session),
            user=SimpleNamespace(id=1),
            registry=SimpleNamespace(),
            consumer_id="planner",
        )

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_collections_and_folders_with_breadcrumbs(self):
        result = asyncio.run(list_spaces(VaultSpacesRequest(), self.context))

        self.assertEqual("completed", result.status)
        by_path = {space.path: space for space in result.spaces}
        self.assertIn("Дом", by_path)
        self.assertIn("Работа", by_path)
        self.assertEqual("blue", by_path["Работа"].color)
        self.assertEqual(3, by_path["Работа"].items_count)
        self.assertIn("Работа / Проект X", by_path)
        self.assertIn("Работа / Проект X / Фаза 1", by_path)
        self.assertNotIn("Дом / Старое", by_path)
        self.assertNotIn("Просто заметка", by_path)
        kinds = [space.kind for space in result.spaces]
        self.assertLess(kinds.index("collection"), kinds.index("folder"))

    def test_archived_folders_opt_in(self):
        result = asyncio.run(list_spaces(VaultSpacesRequest(include_archived=True), self.context))

        paths = {space.path for space in result.spaces}
        self.assertIn("Дом / Старое", paths)

    def test_folders_only_without_collections_scope(self):
        result = asyncio.run(
            list_spaces(
                VaultSpacesRequest(include_folders=True, collection_id=self.home_id),
                self.context,
            )
        )

        paths = {space.path for space in result.spaces}
        # Collections always list (they are the picker roots); folders are scoped.
        self.assertIn("Работа", paths)
        self.assertIn("Дом", paths)
        self.assertNotIn("Работа / Проект X", paths)

    def test_collections_only(self):
        result = asyncio.run(list_spaces(VaultSpacesRequest(include_folders=False), self.context))

        self.assertEqual({"Дом", "Работа"}, {space.path for space in result.spaces})
        self.assertTrue(all(space.kind == "collection" for space in result.spaces))


if __name__ == "__main__":
    unittest.main()
