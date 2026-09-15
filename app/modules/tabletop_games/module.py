from app.core.module_types import MigrationSpec, ModuleSpec

MODULE = ModuleSpec(
    id="tabletop_games",
    version="0.1.0",
    title_en="Tabletop Games",
    title_ru="Настольные игры",
    dashboard_url="/tabletop",
    order=40,
    router="app.modules.tabletop_games.router:router",
    models="app.modules.tabletop_games.models",
    migrations=MigrationSpec(
        path="migrations",
        baseline_revision="tabletop_0001",
        tables=("tabletop_rooms", "tabletop_participants", "tabletop_messages"),
    ),
    templates="templates",
    dependency_extra="tabletop_games",
)
