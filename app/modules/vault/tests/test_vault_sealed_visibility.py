"""A sealed item must be invisible to every surface that is not the Vault itself.

Search, sharing and spaces all publish Vault data to other modules or to the
public. A sealed item has blank readable columns, so the failure mode is not a
leaked title but a leaked *shape* — an entry with an empty title still tells a
reader that a record exists, and any future attempt to open it for indexing would
publish plaintext outside the vault.
"""

import asyncio
import unittest

from app.contracts.search_documents_v1 import SearchDocumentsRequest
from app.contracts.vault_spaces_v1 import VaultSpacesRequest
from app.modules.vault.crypto import generate_inbox_keypair
from app.modules.vault.models import Base, VaultItem
from app.modules.vault.sealing import seal_item as _seal
from app.modules.vault.search import search_documents
from app.modules.vault.share import VaultShareProvider
from app.modules.vault.spaces import list_spaces


class AsyncSessionAdapter:
    """Search, share and spaces are async; this drives a real sync session through them."""

    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    def add(self, instance):
        self.session.add(instance)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()


class _Context:
    def __init__(self, session):
        self.session = session


def sealed_and_plain(session, public_key):
    # The sealed card belongs to a collection: a blind write is bound to the
    # collection whose public key sealed it, so there is no such thing as a
    # sealed card without one.
    sealed = VaultItem(
        id=1,
        entry_type="bookmark",
        title="Личное название",
        content="Личный текст",
        tags=["secret"],
        collection_id=1,
    )
    plain = VaultItem(id=2, entry_type="bookmark", title="Обычная закладка", content="публичный текст")
    session.add_all([sealed, plain])
    session.flush()
    _seal(sealed, public_key)
    session.commit()
    return sealed, plain


class SealedVisibilityTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()
        self.db = AsyncSessionAdapter(self.session)
        self.private_key, self.public_key = generate_inbox_keypair()
        self.sealed, self.plain = sealed_and_plain(self.session, self.public_key)

    def test_search_never_publishes_a_sealed_item(self):
        result = asyncio.run(search_documents(SearchDocumentsRequest(offset=0, limit=50), _Context(self.db)))
        ids = {document.document_id for document in result.documents}

        self.assertEqual({"2"}, ids)
        for document in result.documents:
            self.assertNotIn("Личное название", document.title)
            self.assertNotIn("Личный текст", (document.body or ""))

    def test_the_share_catalog_never_publishes_a_sealed_item(self):
        provider = VaultShareProvider()
        catalog = asyncio.run(provider.catalog(self.db))

        self.assertEqual([2], [entry["id"] for entry in catalog])
        for entry in catalog:
            self.assertNotIn("Личное название", entry["title"])

    def test_spaces_never_publishes_a_sealed_folder(self):
        folder = VaultItem(id=3, entry_type="folder", is_folder=True, title="Личная папка", collection_id=1)
        self.session.add(folder)
        self.session.flush()
        _seal(folder, self.public_key)
        self.session.commit()

        result = asyncio.run(
            list_spaces(
                VaultSpacesRequest(include_folders=True, include_archived=False, collection_id=None),
                _Context(self.db),
            )
        )

        self.assertEqual([], [space.name for space in result.spaces if "Личная" in space.name])

    def test_a_sealed_collection_appears_in_spaces_under_its_alias(self):
        from app.modules.vault.models import VaultCollection

        collection = VaultCollection(name="Личные финансы", is_encrypted=True, public_name="Проект Б")
        self.session.add(collection)
        self.session.commit()

        result = asyncio.run(
            list_spaces(
                VaultSpacesRequest(include_folders=True, include_archived=False, collection_id=None),
                _Context(self.db),
            )
        )

        names = [space.name for space in result.spaces]
        self.assertIn("Проект Б", names)
        self.assertNotIn("Личные финансы", names)


if __name__ == "__main__":
    unittest.main()
