from app.core.module_types import MigrationSpec, ModuleSpec

MODULE = ModuleSpec(
    id="miku",
    version="0.2.0",
    title_en="MIKU",
    title_ru="MIKU",
    dashboard_url="/miku/dashboard",
    order=5,
    router="app.modules.miku.router:router",
    models="app.modules.miku.models",
    migrations=MigrationSpec(
        path="migrations",
        baseline_revision="miku_0001",
        tables=("miku_turn_audit",),
    ),
    templates="templates",
    uses_integrations=("media.video.archive.v1", "vault.capture.v1"),
    uses_integration_contracts=("library.viewer.v1", "video.source.catalog.v1"),
)
