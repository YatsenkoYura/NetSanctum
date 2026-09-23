from app.core.module_types import ModuleSpec

MODULE = ModuleSpec(
    id="miku",
    version="0.1.0",
    title_en="MIKU",
    title_ru="MIKU",
    dashboard_url="/miku/dashboard",
    order=5,
    router="app.modules.miku.router:router",
    templates="templates",
    uses_integration_contracts=("library.viewer.v1",),
)
