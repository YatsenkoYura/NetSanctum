from app.core.module_types import (
    IntegrationEffect,
    IntegrationEffects,
    IntegrationSpec,
    MigrationSpec,
    ModuleSpec,
)

MODULE = ModuleSpec(
    id="search",
    version="0.1.0",
    title_en="Search",
    title_ru="Поиск",
    order=4,
    router="app.modules.search.router:router",
    models="app.modules.search.models",
    tasks="app.modules.search.tasks",
    startup="app.modules.search.lifecycle:startup",
    shutdown="app.modules.search.lifecycle:shutdown",
    migrations=MigrationSpec(
        path="migrations",
        baseline_revision="search_0001",
        tables=("search_documents", "search_sync_state", "search_refresh_outbox"),
    ),
    integrations=(
        IntegrationSpec(
            id="search.global.v1",
            contract="search.query.v1",
            handler="app.modules.search.integrations:global_search",
            request_model="app.contracts.search_query_v1:GlobalSearchRequest",
            result_model="app.contracts.search_query_v1:GlobalSearchResult",
            description=(
                "Search all indexed local NetSanctum modules by title, metadata, content, aliases, and fuzzy text. "
                "Use this for any named local content instead of searching one module at a time."
            ),
            effects=IntegrationEffects(effect=IntegrationEffect.READ, idempotent=True),
        ),
    ),
    uses_integration_contracts=("search.documents.v1",),
)
