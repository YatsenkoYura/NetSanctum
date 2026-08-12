from app.core.module_types import ModuleSpec

MODULE = ModuleSpec(
    id="vault",
    version="0.1.0",
    title_en="Vault",
    title_ru="Хранилище / Заметки",
    dashboard_url="/vault/dashboard",
    order=30,
    router="app.modules.vault.router:router",
    models="app.modules.vault.models",
    templates="templates",
    module_cleanup="app.modules.vault.cleanup:cleanup_module",
    storage_namespaces=("vault",),
    package_prefixes=("vault_",),
    package_resolver="app.modules.vault.capabilities:resolve_package_resources",
)
