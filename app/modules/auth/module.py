from app.core.module_types import ModuleSpec

MODULE = ModuleSpec(
    id="auth",
    version="0.1.0",
    title_en="Authentication",
    title_ru="Авторизация",
    router="app.modules.auth.router:router",
    templates="templates",
    i18n="app.modules.auth.i18n",
    required=True,
)
