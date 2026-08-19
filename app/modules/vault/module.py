from app.core.module_types import MigrationSpec, ModuleSpec, ShareAsset, ShareRoute, ShareSpec

MODULE = ModuleSpec(
    id="vault",
    version="0.1.0",
    title_en="Vault",
    title_ru="Хранилище / Заметки",
    dashboard_url="/vault/dashboard",
    order=30,
    router="app.modules.vault.router:router",
    models="app.modules.vault.models",
    migrations=MigrationSpec(
        path="migrations",
        baseline_revision="vault_0001",
        tables=("vault_collections", "vault_items"),
        legacy_tables=("vault_collections", "vault_items"),
    ),
    templates="templates",
    share=ShareSpec(
        provider="app.modules.vault.share:PROVIDER",
        selector_key="item_ids",
        dashboard_template="vault_dashboard.html",
        api_prefix="/api/vault",
        routes=(
            ShareRoute(name="items", path="items"),
            ShareRoute(name="item_detail", path="items/{item_id}"),
            ShareRoute(name="stats", path="stats"),
            ShareRoute(name="collections", path="collections", source="relations"),
        ),
        assets=(ShareAsset(name="item_image", path="items/{item_id}/image"),),
    ),
    module_cleanup="app.modules.vault.cleanup:cleanup_module",
    storage_namespaces=("vault",),
    package_prefixes=("vault_",),
    package_resolver="app.modules.vault.capabilities:resolve_package_resources",
)
