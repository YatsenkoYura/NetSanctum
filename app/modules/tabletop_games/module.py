from app.core.module_types import MigrationSpec, ModuleSpec, ShareSelectionType, ShareSpec

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
    share=ShareSpec(
        provider="app.modules.tabletop_games.share:PROVIDER",
        selector_key="panel_ids",
        selection_types=(
            ShareSelectionType("panel_ids", "panel", "Tabletop panel", "Панель настольных игр"),
        ),
        dashboard_template="tabletop_dashboard.html",
        api_prefix="/api/tabletop",
        max_items=1,
        interactive_entry_path="/tabletop",
    ),
    dependency_extra="tabletop_games",
)
