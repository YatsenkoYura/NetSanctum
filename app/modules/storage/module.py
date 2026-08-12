from app.core.module_types import ModuleSpec

MODULE = ModuleSpec(
    id="storage",
    version="0.1.0",
    title_en="Storage Manager",
    title_ru="Хранилище",
    dashboard_url="/storage/dashboard",
    order=60,
    router="app.modules.storage.router:router",
    templates="templates",
    i18n="app.modules.storage.i18n",
)
