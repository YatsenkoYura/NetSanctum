"""The sealed offline package: ciphertext out, plaintext never at rest.

Covers the vault side of `docs/sealed-offline-packages.md`: the package wrapper
lifecycle (create/unlock/rekey), the sealed resource builders, and the exact
manifest URL contract the desktop client derives its keys from. The transfer
envelopes themselves are pinned in `tests/test_sealed_transfer.py`.
"""

import asyncio
import base64
import io
import unittest
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.crypto.transfer import (
    open_chunked_range,
    open_small_resource,
    transfer_resource_key,
    unwrap_package_dek,
)
from app.core.database import Base
from app.modules.vault.models import VaultCollection, VaultItem


class SealedPackageTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)

    def sealed_collection(self, collection_id=5):
        """A sealed collection with a matching inbox keypair and wrapper."""
        from app.modules.vault.crypto import context_for, generate_inbox_keypair, wrap_data_key
        from app.modules.vault.sealing import refresh_package_wrap, store_wrapper

        private, public = generate_inbox_keypair()
        row = VaultCollection(
            id=collection_id,
            name="Приватное",
            is_encrypted=True,
            public_name="Зашифрованный Vault",
            inbox_public_key=base64.b64encode(public).decode(),
        )
        self.db.add(row)
        self.db.commit()
        store_wrapper(
            row,
            wrap_data_key(
                private,
                "test passphrase long enough",
                context=context_for("collection", collection_id),
                t_cost=1,
                m_cost=8,
                parallelism=1,
            ),
        )
        refresh_package_wrap(row, private, "test passphrase long enough")
        self.db.commit()
        return row, private


class PackageWrapTests(SealedPackageTestCase):
    def test_the_stored_wrap_opens_with_the_collection_passphrase(self):
        from app.modules.vault.sealing import package_dek_for, sealed_package_id, sealed_package_sealing

        row, private = self.sealed_collection()
        sealing = sealed_package_sealing(row)
        self.assertIsNotNone(sealing)
        assert sealing is not None
        self.assertEqual(1, sealing["version"])
        self.assertEqual("argon2id", sealing["kdf"]["algorithm"])
        recovered = unwrap_package_dek(sealing, "test passphrase long enough", sealed_package_id(row.id))
        self.assertEqual(package_dek_for(private, row.id), recovered)
        # The cost travels with the bytes: a future cost raise must describe
        # stored wraps with their own parameters, not current ones.
        self.assertEqual(
            {"algorithm": "argon2id", "m_cost": 65536, "t_cost": 3, "parallelism": 1},
            sealing["kdf"],
        )
        self.assertEqual(sealing["kdf"], row.sealed_pkg_kdf)

    def test_a_missing_wrap_reads_as_absent_never_guessed(self):
        from app.modules.vault.sealing import sealed_package_sealing

        row, _private = self.sealed_collection()
        row.sealed_pkg_salt = None
        row.sealed_pkg_wrapped = None
        self.assertIsNone(sealed_package_sealing(row))

    def test_unlock_fills_a_missing_wrap(self):
        from app.modules.vault import sealing
        from app.modules.vault.crypto import context_for, generate_inbox_keypair, wrap_data_key
        from app.modules.vault.sealing import sealed_package_sealing, store_wrapper

        private, public = generate_inbox_keypair()
        row = VaultCollection(id=11, name="Приватное", is_encrypted=True)
        row.inbox_public_key = base64.b64encode(public).decode()
        store_wrapper(
            row,
            wrap_data_key(
                private,
                "test passphrase long enough",
                context=context_for("collection", 11),
                t_cost=1,
                m_cost=8,
                parallelism=1,
            ),
        )
        self.assertIsNone(sealed_package_sealing(row))
        with patch.object(sealing, "redis_client", AsyncMock()):
            asyncio.run(
                sealing.unlock_collection(row, "test passphrase long enough", "tok", session=AsyncMock())
            )
        self.assertIsNotNone(sealed_package_sealing(row))

    def test_package_id_names_its_collection(self):
        from app.modules.vault.sealed_package import collection_id_for_package, is_sealed_package_id
        from app.modules.vault.sealing import sealed_package_id

        self.assertEqual("vault_sealed_5", sealed_package_id(5))
        self.assertTrue(is_sealed_package_id("vault_sealed_5"))
        self.assertFalse(is_sealed_package_id("vault_all"))
        self.assertEqual(5, collection_id_for_package("vault_sealed_5"))
        with self.assertRaises(ValueError):
            collection_id_for_package("vault_all")
        with self.assertRaises(ValueError):
            collection_id_for_package("vault_sealed_x")


class SealedResourcesTests(SealedPackageTestCase):
    def sealed_item(self, item_id=10, collection_id=5):
        from app.modules.vault.sealing import inbox_public_key, seal_item

        if self.db.get(VaultCollection, collection_id) is None:
            self.sealed_collection(collection_id)
        row = VaultItem(
            id=item_id,
            entry_type="bookmark",
            title="",
            content=None,
            url="https://example.com/sealed",
            tags=[],
            collection_id=collection_id,
        )
        self.db.add(row)
        self.db.commit()
        collection = self.db.get(VaultCollection, collection_id)
        public = inbox_public_key(collection)
        assert public is not None
        row.title = "Секретная заметка"
        row.content = "содержимое, которое не должно утекать"
        seal_item(row, public)
        self.db.commit()
        return row

    def test_items_snapshot_seals_every_card_and_nothing_else(self):
        from app.modules.vault.sealed_package import (
            build_sealed_items_plaintext,
            seal_items_snapshot,
            sealed_items_url,
        )
        from app.modules.vault.sealing import package_dek_for, sealed_package_id

        row, private = self.sealed_collection()
        self.sealed_item()
        plaintext = asyncio.run(build_sealed_items_plaintext(_Async(self.db), row, private))
        self.assertIn("Секретная заметка".encode(), plaintext)
        self.assertIn("не должно утекать".encode(), plaintext)

        package_id = sealed_package_id(row.id)
        dek = package_dek_for(private, row.id)
        blob = seal_items_snapshot(dek, package_id, row.id, plaintext)
        url = sealed_items_url(row.id, package_id)
        key = transfer_resource_key(dek, package_id, url)
        self.assertEqual(plaintext, open_small_resource(key, url, blob))

    def test_snapshot_building_never_persists_opened_columns(self):
        from app.modules.vault.sealed_package import build_sealed_items_plaintext

        row, private = self.sealed_collection()
        item = self.sealed_item()
        asyncio.run(build_sealed_items_plaintext(_Async(self.db), row, private))
        self.db.expire_all()
        reread = self.db.get(VaultItem, item.id)
        self.assertEqual("", reread.title)
        self.assertIsNotNone(reread.sealed_payload)

    def test_manifest_resources_name_exact_key_urls(self):
        from app.modules.vault.sealed_package import sealed_media_url, sealed_package_resources

        row, _private = self.sealed_collection()
        resources = asyncio.run(sealed_package_resources(_Async(self.db), row))
        package_id = f"vault_sealed_{row.id}"
        self.assertEqual(f"/api/vault/sealed/{row.id}/items?package_id={package_id}", resources[0]["url"])
        self.assertEqual("sealed", resources[0]["type"])
        self.assertNotIn("size", resources[0])
        self.assertNotIn("sha256", resources[0])
        media = self.sealed_item(item_id=21)
        media.image_path = "vault/images/21-abcdef0123456789.png.enc"
        self.db.commit()
        resources = asyncio.run(sealed_package_resources(_Async(self.db), row))
        self.assertIn(sealed_media_url(21, package_id), [r["url"] for r in resources])

    def test_sealed_media_round_trips_through_both_envelopes(self):
        import tempfile
        from pathlib import Path

        from app.core.storage import LocalStorage
        from app.modules.vault.sealed_package import iter_sealed_media, sealed_media_url
        from app.modules.vault.sealing import derive_file_key, package_dek_for, sealed_package_id

        row, private = self.sealed_collection()
        package_id = sealed_package_id(row.id)
        dek = package_dek_for(private, row.id)
        file_key = derive_file_key(private, row.id)
        plaintext = bytes((i * 5) % 256 for i in range(2 * 1024 * 1024 + 777))

        with tempfile.TemporaryDirectory() as directory:
            from unittest.mock import patch

            storage = LocalStorage(str(Path(directory) / "storage"))
            storage.save_file_encrypted_seekable(
                io.BytesIO(plaintext), "vault/images/30-x.png.enc", key=file_key, length=len(plaintext)
            )
            item = VaultItem(
                id=30,
                entry_type="bookmark",
                title="",
                tags=[],
                collection_id=row.id,
                image_path="vault/images/30-x.png.enc",
            )
            url = sealed_media_url(30, package_id)
            key = transfer_resource_key(dek, package_id, url)
            with patch("app.modules.vault.sealed_package.get_storage", return_value=storage):
                sealed = b"".join(iter_sealed_media(dek, package_id, item, file_key))
        self.assertEqual(plaintext, b"".join(open_chunked_range(key, url, sealed, 0, len(plaintext))))
        self.assertEqual(
            plaintext[12345 : 12345 + 999],
            b"".join(open_chunked_range(key, url, sealed, 12345, 999)),
        )

    def test_resolver_refuses_anything_but_a_sealed_collection(self):
        from app.modules.vault.capabilities import resolve_package_resources

        async def run():
            # The plaintext package still resolves; only sealed ids are new.
            existing = await resolve_package_resources("vault_all", _Async(self.db))
            self.assertIsInstance(existing, list)
            with self.assertRaises(ValueError):
                await resolve_package_resources("vault_sealed_x", _Async(self.db))
            with self.assertRaises(ValueError):
                await resolve_package_resources("vault_sealed_999", _Async(self.db))
            resources = await resolve_package_resources("vault_sealed_5", _Async(self.db))
            self.assertTrue(resources)
            self.assertTrue(all(r["type"] == "sealed" for r in resources))

        self.sealed_collection()
        asyncio.run(run())


class _Async:
    """The slice of AsyncSession the sealed producer touches, over sqlite."""

    def __init__(self, session):
        self.session = session

    async def execute(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {})

    async def get(self, model, ident):
        return self.session.get(model, ident)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()


if __name__ == "__main__":
    unittest.main()
