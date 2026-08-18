from app.core.module_types import MigrationSpec, ModuleSpec

MODULE = ModuleSpec(
    id="sharing",
    version="0.1.0",
    title_en="Sharing",
    title_ru="Общий доступ",
    dashboard_url="/shares/dashboard",
    order=90,
    router="app.modules.sharing.router:router",
    models="app.modules.sharing.models",
    migrations=MigrationSpec(
        path="migrations",
        baseline_revision="sharing_0001",
        tables=("share_links",),
    ),
    templates="templates",
    required=True,
)
