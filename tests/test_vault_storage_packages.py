import asyncio
import datetime
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from app.modules.storage.capabilities import resolve_package_resources as resolve_storage_resources
from app.modules.storage.module import MODULE as STORAGE_MODULE
from app.modules.storage.router import get_storage_sync_manifest, storage_dashboard
from app.modules.vault.capabilities import resolve_package_resources as resolve_vault_resources
from app.modules.vault.module import MODULE as VAULT_MODULE
from app.modules.vault.router import get_package_items, get_vault_sync_manifest
from app.modules.vault.services import list_vault_package_items

ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


def vault_item(item_id: int, og_image: str | None):
    now = datetime.datetime(2026, 1, 1)
    return SimpleNamespace(
        id=item_id,
        entry_type="bookmark",
        title=f"Item {item_id}",
        content=None,
        url=None,
        og_title=None,
        og_description=None,
        og_image=og_image,
        score=None,
        status=None,
        progress_current=0,
        progress_total=None,
        rewatch_count=0,
        category=None,
        tags=[],
        is_pinned=False,
        is_archived=False,
        collection_id=None,
        collection_name=None,
        related_entity_type=None,
        related_entity_id=None,
        parent_id=None,
        is_folder=False,
        node_type="note",
        canvas_data={},
        created_at=now,
        updated_at=now,
    )


class VaultPackageTests(unittest.TestCase):
    def test_vault_provider_accepts_only_the_complete_vault_id(self):
        self.assertEqual(("vault_all",), VAULT_MODULE.package_prefixes)
        with self.assertRaises(ValueError):
            run(resolve_vault_resources("vault_all_backup", object()))
        with self.assertRaises(ValueError):
            run(resolve_vault_resources("vault_", object()))

    def test_vault_manifest_and_resolver_share_one_complete_snapshot_resource_set(self):
        manifest = run(get_vault_sync_manifest(db=object(), user=None, hybrid=False))
        resolved = run(resolve_vault_resources("vault_all", object()))
        urls = [resource["url"] for resource in manifest["resources"]]

        self.assertEqual(manifest["resources"], resolved)
        self.assertEqual(1, urls.count("/api/vault/package-items?package_id=vault_all"))
        self.assertFalse(any("offset=" in url or "limit=" in url for url in urls))
        self.assertFalse(any("/preview" in url for url in urls))

    def test_vault_snapshot_nulls_remote_images_and_keeps_embedded_images(self):
        items = [
            vault_item(2, "https://example.com/live.jpg"),
            vault_item(1, "data:image/png;base64,AAAA"),
        ]
        db = object()
        with patch(
            "app.modules.vault.router.list_vault_package_items",
            AsyncMock(return_value=items),
        ) as list_items:
            payload = run(get_package_items("vault_all", db=db, user=None))

        list_items.assert_awaited_once_with(db)
        self.assertIsNone(payload[0]["og_image"])
        self.assertEqual("data:image/png;base64,AAAA", payload[1]["og_image"])

    def test_vault_snapshot_rejects_alias_before_reading_items(self):
        with patch(
            "app.modules.vault.router.list_vault_package_items",
            AsyncMock(),
        ) as list_items:
            with self.assertRaises(HTTPException):
                run(get_package_items("vault_all_backup", db=object(), user=None))

        list_items.assert_not_awaited()

    def test_vault_snapshot_order_has_id_tie_breaker(self):
        class Result:
            def scalars(self):
                return self

            def all(self):
                return []

        class Database:
            statement = None

            async def execute(self, statement):
                self.statement = statement
                return Result()

        db = Database()
        run(list_vault_package_items(db))
        order_clause = str(db.statement).split("ORDER BY ", 1)[1]

        self.assertEqual(
            "vault_items.is_pinned DESC, vault_items.created_at DESC, vault_items.id DESC",
            order_clause,
        )

    def test_vault_template_uses_only_the_packaged_snapshot_offline(self):
        template = (ROOT / "app/modules/vault/templates/vault_dashboard.html").read_text()

        self.assertIn("packageUrl('/api/vault/package-items')", template)
        self.assertNotIn("packageUrl(`/api/vault/items?limit=500&offset=${offset}", template)


class StoragePackageTests(unittest.TestCase):
    def test_storage_declares_strict_package_provider(self):
        self.assertEqual(("storage_manager",), STORAGE_MODULE.package_prefixes)
        self.assertEqual(
            "app.modules.storage.capabilities:resolve_package_resources",
            STORAGE_MODULE.package_resolver,
        )
        with self.assertRaises(ValueError):
            run(resolve_storage_resources("storage_manager_backup", object()))

    def test_storage_resolver_matches_manifest_and_manifest_supports_hybrid(self):
        manifest = run(get_storage_sync_manifest(user=None, hybrid=False))
        resolved = run(resolve_storage_resources("storage_manager", object()))
        hybrid = run(get_storage_sync_manifest(user=None, hybrid=True))

        self.assertEqual(manifest["resources"], resolved)
        self.assertEqual(
            [{"url": "/api/packages/storage_manager/nsp", "type": "container"}],
            hybrid["resources"],
        )

    def test_storage_dashboard_sets_package_read_only_context(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/storage/dashboard",
                "query_string": b"package_id=storage_manager",
                "headers": [],
            }
        )
        with (
            patch(
                "app.modules.storage.router.asyncio.to_thread",
                AsyncMock(return_value={"modules": [], "large_files": []}),
            ),
            patch("app.modules.storage.router.templates.TemplateResponse") as render,
        ):
            run(storage_dashboard(request, package_id="storage_manager", user=object()))

        context = render.call_args.args[2]
        self.assertTrue(context["package_mode"])
        self.assertTrue(context["is_readonly"])

        with self.assertRaises(HTTPException):
            run(storage_dashboard(request, package_id="storage_manager_backup", user=object()))

    def test_storage_read_only_template_hides_all_mutation_controls(self):
        environment = Environment(
            loader=FileSystemLoader(ROOT / "app/modules/storage/templates"),
            autoescape=True,
        )
        template = environment.get_template("storage_content.html")
        stats = {
            "is_s3": False,
            "bucket_name": None,
            "used_human": "1 KB",
            "used_percent": 1,
            "total_human": "1 MB",
            "free_human": "999 KB",
            "modules": [{"name": "vault", "size_human": "1 KB", "file_count": 1}],
            "large_files": [
                {
                    "module": "vault",
                    "name": "item.bin",
                    "path": "vault/item.bin",
                    "size_human": "1 KB",
                }
            ],
        }

        def translate(_module, key, **kwargs):
            return key.format(**kwargs)

        read_only = template.render(stats=stats, is_readonly=True, _=translate)
        online = template.render(stats=stats, is_readonly=False, _=translate)

        for mutation in (
            'hx-post="/storage/api/recalculate"',
            'hx-post="/storage/api/clean-module',
            'hx-delete="/storage/api/file',
        ):
            self.assertNotIn(mutation, read_only)
            self.assertIn(mutation, online)


if __name__ == "__main__":
    unittest.main()
