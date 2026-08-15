from app.core.module_types import MigrationSpec, ModuleSpec

MODULE = ModuleSpec(
    id="settings",
    version="0.1.0",
    title_en="Settings",
    title_ru="Настройки",
    router="app.modules.settings.router:router",
    models="app.modules.settings.models",
    migrations=MigrationSpec(
        path="migrations",
        baseline_revision="settings_0001",
        tables=("settings",),
        legacy_tables=("settings",),
    ),
    templates="templates",
    i18n="app.modules.settings.i18n",
    required=True,
)
