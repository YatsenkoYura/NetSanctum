from app.core.module_types import ModuleSpec

MODULE = ModuleSpec(
    id="settings",
    version="0.1.0",
    title_en="Settings",
    title_ru="Настройки",
    router="app.modules.settings.router:router",
    models="app.modules.settings.models",
    templates="templates",
    i18n="app.modules.settings.i18n",
    required=True,
)
